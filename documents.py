# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "beautifulsoup4", "pymupdf", "python-docx", "python-pptx", "openpyxl", "libarchive-c"]
# ///
"""Archive every course document of the DLWMS portal.

Usage: documents.py list | download | extract [--all] | sync | stats
  list      enumerate every year x subject x school year, insert metadata rows
  download  fetch the files of rows with fetched = 0 (4 logins in parallel)
  extract   extract text from the downloaded files (no network)
  sync      list (the two newest school years only) + download + extract

A download is a postback that needs the ViewState of the grid page that shows
the link. A worker therefore walks the grid pages of one search and downloads
the pending rows on each page.
"""
import hashlib
import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from email.message import Message
from urllib.parse import unquote

import requests

from fit_archive import BASE, DB, DELAY, P, WORKERS, Client, hidden, text

URL = BASE + 'nastava/dokumenti/pretraga.aspx'
FILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'files')
TEXT_LIMIT = 5_000_000
MEMBER_LIMIT = 100_000_000

SCHEMA = '''
create table if not exists documents (id text primary key, study_year integer, subject text, subject_id text, school_year text, kind text, title text, filename text, path text, size integer, uploaded_at text, author text, text text, fetched integer not null default 0, error text);
'''
META = re.compile(r'^(?P<kind>.*?)\s*(?P<date>\d\d\.\d\d\.\d{4})\s*/\s*Autor-\s*(?P<author>.*?)\s*\($')
PB = re.compile(r"__doPostBack\('([^']+)'")
LOCK = threading.Lock()

TEXT_EXT = {'txt', 'csv', 'md', 'json', 'xml', 'html', 'htm', 'sql', 'py', 'java', 'c', 'cpp', 'h', 'hpp', 'cs', 'js',
	'ts', 'php', 'css', 'sh', 'bat', 'r', 'm', 'tex', 'log', 'ini', 'yml', 'yaml', 'rtf', 'asm', 'pas', 'vhd', 'v',
	'ipynb', 'tsv', 'ino', 'kt', 'swift', 'go', 'rs', 'rb', 'cshtml', 'aspx', 'config', 'properties', 'srt'}


# ---------- site access ----------

def search_fields(group, **extra):
	return {P + 'listGodina': str(group['study_year']), P + 'ddlPredmeti': group['subject_id'], P + 'listVrsta': '',
		P + 'ddlSkolskeGodine': group['school_year'], P + 'txtKljucneRijeci': '', **extra}


def post_year(client, year):
	soup = client.get(URL)
	return client.post(URL, {**hidden(soup), P + 'listGodina': str(year), '__EVENTTARGET': P + 'listGodina', '__EVENTARGUMENT': ''})


def run_search(client, group):
	soup = post_year(client, group['study_year'])
	return client.post(URL, {**hidden(soup), **search_fields(group, **{P + 'btnTrazi': 'Traži'})})


def parse_row(link):
	spans = {s.get('id'): text(s) for s in link.find_parent('td').find_all('span')}
	meta = META.match(spans.get('lblVrsta', ''))
	return {
		'title': text(link),
		'target': PB.search(link['href']).group(1),
		'kind': meta['kind'] if meta else spans.get('lblVrsta', ''),
		'uploaded_at': meta['date'] if meta else '',
		'author': meta['author'] if meta else '',
		'size_kb': int(spans['lblDokumentSize']) if spans.get('lblDokumentSize', '').isdigit() else 0,
	}


def grid_rows(soup):
	grid = soup.find('table', id='gridDokumenti')
	if grid is None:
		return []
	return [parse_row(a) for a in grid.find_all('a', id='lbtnNaslov')]


def next_target(soup):
	pager = soup.find('table', id='gridDokumenti').find('tr', class_='pgr').find('td')
	items = [(text(n), n.name == 'span', PB.search(n.get('href', ''))) for n in pager.find_all(['a', 'span'])]
	current = next((i for i, item in enumerate(items) if item[1]), None)
	if current is None or current + 1 >= len(items):
		return None
	match = items[current + 1][2]
	return match.group(1) if match else None


def walk(client, group):
	"""Yield (soup, rows) for every grid page of one search."""
	soup = run_search(client, group)
	seen = set()
	while soup.find('table', id='gridDokumenti') is not None:
		rows = grid_rows(soup)
		sig = tuple((r['target'].rsplit('$', 2)[-2], r['title'], r['uploaded_at']) for r in rows)
		if not rows or sig in seen:
			return
		seen.add(sig)
		yield soup, rows
		target = next_target(soup)
		if target is None:
			return
		soup = client.post(URL, {**hidden(soup), **search_fields(group), '__EVENTTARGET': target, '__EVENTARGUMENT': ''})


def doc_id(group, row, n):
	key = '|'.join([group['subject_id'], group['school_year'], row['kind'], row['title'], row['uploaded_at'],
		row['author'], str(row['size_kb']), str(n)])
	return hashlib.sha1(key.encode()).hexdigest()[:16]


def tag_rows(group, rows, counter):
	for row in rows:
		base = (row['kind'], row['title'], row['uploaded_at'], row['author'], row['size_kb'])
		row['id'] = doc_id(group, row, counter[base])
		counter[base] += 1
	return rows


# ---------- list ----------

def read_options(client):
	soup = client.get(URL)
	years = [o['value'] for o in soup.find('select', attrs={'name': P + 'ddlSkolskeGodine'}).find_all('option') if o['value']]
	subjects = []
	for year in (1, 2, 3, 4):
		page = post_year(client, year)
		for o in page.find('select', attrs={'name': P + 'ddlPredmeti'}).find_all('option'):
			subjects.append({'study_year': year, 'subject_id': o['value'], 'subject': text(o)})
	return years, subjects


def db_insert(db, group, rows):
	with LOCK:
		db.executemany('insert or ignore into documents (id, study_year, subject, subject_id, school_year, kind, title, uploaded_at, author, size) values (?,?,?,?,?,?,?,?,?,?)',
			[(r['id'], group['study_year'], group['subject'], group['subject_id'], group['school_year'], r['kind'], r['title'],
				r['uploaded_at'], r['author'], r['size_kb'] * 1024) for r in rows])


def list_group(client, group):
	counter = Counter()
	return [row for _, rows in walk(client, group) for row in tag_rows(group, rows, counter)]


def row_key(row):
	return (row['kind'], row['title'], row['uploaded_at'], row['author'])


def orphan_rows(all_rows, by_year_rows):
	"""Blank school year lists everything; keep only rows that no school year search found."""
	known = Counter(row_key(r) for r in by_year_rows)
	fresh = []
	for row in all_rows:
		if known[row_key(row)] > 0:
			known[row_key(row)] -= 1
		else:
			fresh.append(row)
	return fresh


def crawl_list(db, full=True):
	# One write per subject: the other crawl holds the write lock between its commits.
	# Without full, search only the two newest school years and skip the search with no school year.
	client = Client()
	years, subjects = read_options(client)
	if not full:
		years = sorted(years)[-2:]
	print(f'{len(subjects)} subjects, school years {years}', flush=True)
	for subject in subjects:
		found = 0
		by_year = []
		for year in years:
			group = {**subject, 'school_year': year}
			rows = list_group(client, group)
			by_year.append((group, rows))
		group = {**subject, 'school_year': ''}
		everything = list_group(client, group) if full else []
		orphans = orphan_rows(everything, [r for _, rows in by_year for r in rows])
		for g, rows in [*by_year, (group, orphans)]:
			db_insert(db, g, rows)
			found += len(rows)
		db.commit()
		print(f"list {subject['study_year']} {subject['subject']}: all {len(everything)}, stored {found}, no school year {len(orphans)}", flush=True)


# ---------- download ----------

def safe_name(name):
	name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name).strip(' .') or 'file'
	return name[:180]


def header_filename(response):
	disposition = response.headers.get('Content-Disposition', '')
	msg = Message()
	msg['content-disposition'] = disposition
	name = msg.get_filename()
	if name is None:
		return None
	try:  # requests decodes headers as latin-1; the server sends UTF-8 bytes
		name = name.encode('latin-1').decode('utf-8')
	except (UnicodeEncodeError, UnicodeDecodeError):
		pass
	return unquote(name)


def reserve_path(subject_id, name):
	folder = os.path.join(FILES, subject_id)
	os.makedirs(folder, exist_ok=True)
	stem, ext = os.path.splitext(safe_name(name))
	with LOCK:
		for n in range(1000):
			candidate = f'{stem}{f" ({n})" if n else ""}{ext}'
			try:
				os.close(os.open(os.path.join(folder, candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
				return candidate
			except FileExistsError:
				continue
	raise RuntimeError('No free file name.')


def fetch_file(client, soup, group, row):
	form = {**hidden(soup), **search_fields(group), '__EVENTTARGET': row['target'], '__EVENTARGUMENT': ''}
	response = client.s.post(URL, data=form, timeout=300)
	time.sleep(DELAY)
	if 'login.aspx' in response.url:
		client.login()
		raise RuntimeError('session expired')
	name = header_filename(response)
	if name is None:
		raise RuntimeError(f"no attachment ({response.status_code}, {response.headers.get('Content-Type')})")
	response.raise_for_status()
	return name, response.content


def save_doc(db, group, row, name, data):
	saved = reserve_path(group['subject_id'], name)
	with open(os.path.join(FILES, group['subject_id'], saved), 'wb') as f:
		f.write(data)
	with LOCK:
		db.execute('update documents set filename = ?, path = ?, size = ?, fetched = 1, error = null where id = ?',
			(name, f"{group['subject_id']}/{saved}", len(data), row['id']))
		db.commit()


def set_error(db, doc_id_, message):
	with LOCK:
		db.execute('update documents set error = ? where id = ?', (message[:500], doc_id_))
		db.commit()


def download_page(client, db, group, soup, rows, pending):
	for row in rows:
		if row['id'] not in pending:
			continue
		try:
			name, data = fetch_file(client, soup, group, row)
			save_doc(db, group, row, name, data)
			print(f"ok {group['subject']}: {name} ({len(data)} B)", flush=True)
		except (requests.RequestException, RuntimeError, OSError) as error:
			set_error(db, row['id'], f'{type(error).__name__}: {error}')
			print(f"ERR {group['subject']}: {row['title']}: {error}", flush=True)


def download_group(clients, db, group):
	client = clients.pop()
	try:
		counter = Counter()
		for soup, rows in walk(client, group):
			tag_rows(group, rows, counter)
			with LOCK:
				pending = {r[0] for r in db.execute('select id from documents where fetched = 0 and subject_id = ? and school_year = ?',
					(group['subject_id'], group['school_year']))}
			download_page(client, db, group, soup, rows, pending)
	except (requests.RequestException, AttributeError) as error:
		print(f"group failed {group['subject']} [{group['school_year']}]: {error}", flush=True)
	finally:
		clients.append(client)


def pending_groups(db):
	cols = 'study_year, subject_id, subject, school_year'
	return [dict(zip(cols.split(', '), r)) for r in db.execute(f'select distinct {cols} from documents where fetched = 0 order by 1, 2, 4')]


def crawl_download(db):
	groups = pending_groups(db)
	print(f'{len(groups)} groups with pending documents', flush=True)
	if not groups:
		return
	clients = [Client() for _ in range(min(WORKERS, len(groups)))]
	with ThreadPoolExecutor(len(clients)) as pool:
		list(pool.map(lambda g: download_group(clients, db, g), groups))


# ---------- extract ----------

def pdf_text(data):
	import pymupdf
	with pymupdf.open(stream=data, filetype='pdf') as doc:
		return '\n'.join(page.get_text() for page in doc)


def docx_text(data):
	import docx
	doc = docx.Document(io.BytesIO(data))
	parts = [p.text for p in doc.paragraphs]
	parts += [cell.text for table in doc.tables for row in table.rows for cell in row.cells]
	return '\n'.join(parts)


def shape_text(shape):
	parts = [shape.text_frame.text] if shape.has_text_frame else []
	if getattr(shape, 'has_table', False) and shape.has_table:
		parts += [c.text for row in shape.table.rows for c in row.cells]
	return parts


def pptx_text(data):
	import pptx
	parts = []
	for slide in pptx.Presentation(io.BytesIO(data)).slides:
		parts += [t for shape in slide.shapes for t in shape_text(shape)]
		if slide.has_notes_slide:
			parts.append(slide.notes_slide.notes_text_frame.text)
	return '\n'.join(parts)


def xlsx_text(data):
	import openpyxl
	book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
	lines = []
	for sheet in book.worksheets:
		lines.append(f'# {sheet.title}')
		lines += ['\t'.join('' if v is None else str(v) for v in row) for row in sheet.iter_rows(values_only=True)]
	return '\n'.join(lines)


def plain_text(data):
	for encoding in ('utf-8-sig', 'cp1250', 'latin-1'):
		try:
			return data.decode(encoding)
		except UnicodeDecodeError:
			continue
	return ''


def zip_text(data):
	parts = []
	with zipfile.ZipFile(io.BytesIO(data)) as archive:
		for info in archive.infolist():
			if info.is_dir():
				continue
			parts.append(f'== {info.filename}')
			if info.file_size > MEMBER_LIMIT:
				continue
			try:
				parts.append(extract_bytes(info.filename, archive.read(info)))
			except Exception as error:  # a broken member must not hide the others
				parts.append(f'[extract failed: {type(error).__name__}]')
	return '\n'.join(parts)


def rar_text(data):
	"""Read rar through the system libarchive (the standard library cannot, and the Fedora 7z has no rar codec)."""
	import libarchive
	parts = []
	with libarchive.memory_reader(data) as entries:
		for entry in entries:
			if not entry.isfile:
				continue
			parts.append(f'== {entry.pathname}')
			if entry.size <= MEMBER_LIMIT:
				parts.append(extract_bytes(entry.pathname, b''.join(entry.get_blocks())))
	return '\n'.join(parts)


def legacy_office(target, extractor):
	# LibreOffice converts the binary Office formats into the XML formats that we already read.
	def convert(data):
		with tempfile.TemporaryDirectory() as tmp:
			source = os.path.join(tmp, 'in')
			with open(source, 'wb') as f:
				f.write(data)
			subprocess.run(['soffice', f'-env:UserInstallation=file://{tmp}/profile', '--headless',
				'--convert-to', target, '--outdir', tmp, source], check=True, capture_output=True, timeout=300)
			with open(f'{source}.{target}', 'rb') as f:
				return extractor(f.read())
	return convert


EXTRACTORS = {'pdf': pdf_text, 'docx': docx_text, 'pptx': pptx_text, 'xlsx': xlsx_text, 'xlsm': xlsx_text, 'zip': zip_text, 'rar': rar_text,
	'doc': legacy_office('docx', docx_text), 'ppt': legacy_office('pptx', pptx_text), 'xls': legacy_office('xlsx', xlsx_text)}


def extract_bytes(name, data):
	ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
	func = EXTRACTORS.get(ext) or (plain_text if ext in TEXT_EXT else None)
	return func(data).replace('\x00', '')[:TEXT_LIMIT] if func else ''


def extract_file(args):
	doc_id_, path = args
	try:
		with open(os.path.join(FILES, path), 'rb') as f:
			return doc_id_, extract_bytes(path, f.read()), None
	except Exception as error:  # extraction libraries raise anything
		return doc_id_, '', f'extract {type(error).__name__}: {error}'[:500]


def run_extract(db, everything):
	where = '' if everything else 'and text is null'
	todo = list(db.execute(f"select id, path from documents where fetched = 1 and path is not null {where}"))
	print(f'extracting {len(todo)} files', flush=True)
	with ProcessPoolExecutor(WORKERS) as pool:
		for index, (doc_id_, content, error) in enumerate(pool.map(extract_file, todo, chunksize=4), 1):
			db.execute('update documents set text = ?, error = case when error is null or error like \'extract%\' then ? else error end where id = ?', (content, error, doc_id_))
			if index % 50 == 0 or index == len(todo):
				db.commit()
				print(f'extract {index}/{len(todo)}', flush=True)
	db.commit()


# ---------- stats ----------

def show_stats(db):
	q = lambda sql: db.execute(sql).fetchall()
	print('documents, fetched, bytes:', q('select count(*), sum(fetched), sum(size) from documents where fetched = 1 or 1')[0])
	print('per study year:', q('select study_year, count(*), sum(fetched) from documents group by 1'))
	print('per school year:', q('select school_year, count(*) from documents group by 1'))
	print('with text:', q("select count(*) from documents where text is not null and text != ''")[0][0])
	print('errors:', q('select count(*) from documents where error is not null')[0][0])


def main():
	db = sqlite3.connect(DB, timeout=60, check_same_thread=False)
	db.execute('pragma busy_timeout = 60000')
	db.executescript(SCHEMA)
	command = sys.argv[1] if len(sys.argv) > 1 else 'sync'
	if command in ('sync', 'list'):
		crawl_list(db, full=command == 'list')
	if command in ('sync', 'download'):
		crawl_download(db)
	if command in ('sync', 'extract'):
		run_extract(db, '--all' in sys.argv)
	if command in ('sync', 'stats'):
		show_stats(db)


if __name__ == '__main__':
	main()
