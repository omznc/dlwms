# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "beautifulsoup4"]
# ///
import base64
import os
import queue
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup, Comment

BASE = 'https://www.fit.ba/student/'
ARCHIVE = BASE + 'obavijesti/arhiva.aspx'
P = 'ctl00$ContentPlaceHolder1$'
DELAY = float(os.getenv('DELAY', '0.5'))
WORKERS = int(os.getenv('WORKERS', '4'))
DB = os.getenv('DB', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fit.db'))

SCHEMA = '''
create table if not exists posts (
	sqs text primary key,
	title text not null,
	listed_at text,
	date text,
	subject text,
	author text,
	email text,
	content text,
	fetched integer not null default 0
);
create virtual table if not exists posts_fts using fts5(title, subject, author, content, content='posts', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2');
create trigger if not exists posts_ai after insert on posts begin
	insert into posts_fts(rowid, title, subject, author, content) values (new.rowid, new.title, new.subject, new.author, new.content);
end;
create trigger if not exists posts_au after update on posts begin
	insert into posts_fts(posts_fts, rowid, title, subject, author, content) values ('delete', old.rowid, old.title, old.subject, old.author, old.content);
	insert into posts_fts(rowid, title, subject, author, content) values (new.rowid, new.title, new.subject, new.author, new.content);
end;
'''


class Client:
	def __init__(self):
		self.s = requests.Session()
		self.s.headers['User-Agent'] = 'Mozilla/5.0 (personal archive)'
		self.login()

	def login(self):
		soup = self.soup(self.s.get(BASE + 'login.aspx', timeout=60))
		form = hidden(soup)
		form.update(txtBrojDosijea=os.environ['FIT_USERNAME'], txtLozinka=os.environ['FIT_PASSWORD'], btnPrijava='Prijava')
		r = self.s.post(BASE + 'login.aspx', data=form, timeout=60)
		if 'login.aspx' in r.url:
			sys.exit('Login failed: check FIT_USERNAME and FIT_PASSWORD.')

	def soup(self, response):
		response.raise_for_status()
		time.sleep(DELAY)
		return BeautifulSoup(response.text, 'html.parser')

	def get(self, url):
		r = self.s.get(url, timeout=60)
		if 'login.aspx' in r.url:
			self.login()
			r = self.s.get(url, timeout=60)
		return self.soup(r)

	def post(self, url, form):
		return self.soup(self.s.post(url, data=form, timeout=60))


def hidden(soup):
	return {i['name']: i.get('value', '') for i in soup.select('input[type=hidden]') if i.get('name')}


def sqs_of(href):
	match = re.search(r'sqs=([^&]+)', href)
	return match.group(1) if match else None


def text(node):
	return ' '.join(node.get_text(' ').split()) if node else ''


def read_rows(soup):
	grid = soup.find('table', id=lambda x: x and x.endswith('dgArhiva'))
	if grid is None:
		raise RuntimeError('No archive grid on the page.')
	for row in grid.find_all('tr', class_='grid'):
		link = row.find('a', id='lnkNaslov')
		cells = row.find_all('td')
		if link and sqs_of(link['href']):
			yield sqs_of(link['href']), text(link), text(cells[0])


def pager_links(soup):
	return [(text(a), a['href']) for a in soup.find_all('a', href=True) if 'dgArhiva' in a['href']]


def next_page_target(soup, current):
	pager = pager_links(soup)
	target = next((href for label, href in pager if label == str(current + 1)), None)
	if target is None and current % 10 == 0:
		target = next((href for label, href in reversed(pager) if label == '...'), None)
	match = re.search(r"__doPostBack\('([^']+)'", target or '')
	return match.group(1) if match else None


def crawl_list(client, db, full=True):
	# An empty search lists every post, newest first; the pager postbacks do not keep a date filter.
	# Without full, stop after the first page that adds no post.
	soup = client.get(ARCHIVE)
	form = hidden(soup)
	form.update({P + 'listPredmet': '', P + 'txtRijeci': '', P + 'listOperator': 'AND',
		P + 'txtDatumOd': '', P + 'txtDatumDo': '', P + 'btnTrazi': 'Traži'})
	soup = client.post(ARCHIVE, form)
	page = 1
	seen = set()
	while True:
		rows = [row for row in read_rows(soup) if row[0] not in seen]
		seen.update(row[0] for row in rows)
		before = db.total_changes
		db.executemany('insert or ignore into posts (sqs, title, listed_at) values (?, ?, ?)', rows)
		db.commit()
		added = db.total_changes > before
		if page % 25 == 0:
			print(f'page {page}: total {len(seen)}', flush=True)
		target = next_page_target(soup, page)
		if target is None or not rows or not (full or added):
			print(f'list done at page {page}: {len(seen)} posts', flush=True)
			return
		form = hidden(soup)
		form.update(__EVENTTARGET=target, __EVENTARGUMENT='')
		soup = client.post(ARCHIVE, form)
		page += 1


def read_content(soup):
	panel = soup.find('div', id='Panel1')
	if panel is None:
		return '', ''
	for node in panel.find_all(string=lambda value: isinstance(value, Comment)):
		node.extract()
	html = panel.decode_contents()
	return panel_text(BeautifulSoup(html, 'html.parser')), html


def panel_text(panel):
	# One line per table row keeps the cells of a row together for search and snippets.
	for row in panel.find_all('tr'):
		cells = [text(cell) for cell in row.find_all(['td', 'th'])]
		row.replace_with('\n' + ' | '.join(cells) + '\n')
	lines = (' '.join(line.split()) for line in panel.get_text('\n').splitlines())
	return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


def read_details(clients, sqs):
	# ASP.NET locks each session, so one thread per login gives the parallelism.
	client = clients.get()
	try:
		soup = client.get(f'{BASE}obavijesti/opsirnije.aspx?sqs={sqs}')
	finally:
		clients.put(client)
	author = soup.find('a', id='HyperLink9')
	return (
		text(soup.find('span', id='lblDatum')),
		text(soup.find('span', id='lblPredmet')),
		text(author),
		(author.get('href') or '').replace('mailto:', '') if author else '',
		*read_content(soup),
		sqs,
	)


def fetch_details(db):
	pending = [row[0] for row in db.execute('select sqs from posts where fetched = 0')]
	clients = queue.Queue()
	for _ in range(WORKERS):
		clients.put(Client())
	with ThreadPoolExecutor(WORKERS) as pool:
		futures = {pool.submit(read_details, clients, sqs): sqs for sqs in pending}
		for index, future in enumerate(as_completed(futures), 1):
			try:
				row = future.result()
			except requests.RequestException as error:
				print(f'skip {futures[future]}: {error}', flush=True)
				continue
			db.execute('update posts set date = ?, subject = ?, author = ?, email = ?, content = ?, html = ?, fetched = 1 where sqs = ?', row)
			if index % 100 == 0 or index == len(pending):
				db.commit()
				print(f'details {index}/{len(pending)}', flush=True)
	db.commit()


IMAGE_MAGIC = {b'\xff\xd8\xff': 'image/jpeg', b'\x89PNG': 'image/png', b'GIF8': 'image/gif', b'BM': 'image/bmp'}


def image_bytes(body):
	# The page writes the image and then renders an empty WebForms page after it, as text/html.
	kind = next((kind for magic, kind in IMAGE_MAGIC.items() if body.startswith(magic)), None)
	if kind is None:
		return None
	end = body.find(b'<!DOCTYPE')
	return kind, (body[:end] if end > 0 else body).rstrip(b'\r\n')


def inline_images(db):
	# DLWMS serves post images only to a signed-in user, so store them in the post as data URLs.
	client = Client()
	rows = db.execute("select sqs, html from posts where html like '%obavijest_slika.aspx%'").fetchall()
	for sqs, html in rows:
		soup = BeautifulSoup(html, 'html.parser')
		for img in soup.find_all('img', src=re.compile('obavijest_slika.aspx')):
			response = client.s.get(BASE + 'obavijesti/' + img['src'], timeout=60)
			image = image_bytes(response.content)
			if response.ok and image:
				img['src'] = f'data:{image[0]};base64,{base64.b64encode(image[1]).decode()}'
			else:
				print(f'image failed in {sqs}: {response.status_code} {response.content[:8]!r}', flush=True)
		db.execute('update posts set html = ? where sqs = ?', (soup.decode(), sqs))
		db.commit()
	print(f'images: {len(rows)} posts', flush=True)


def search(db, query):
	sql = '''select p.listed_at, p.subject, p.title, snippet(posts_fts, 3, '[', ']', ' … ', 12)
		from posts_fts join posts p on p.rowid = posts_fts.rowid
		where posts_fts match ? order by rank limit 30'''
	for listed_at, subject, title, snippet in db.execute(sql, (query,)):
		print(f'{listed_at}  {subject}\n  {title}\n  {snippet}\n')


def main():
	db = sqlite3.connect(DB, timeout=60)
	db.executescript(SCHEMA)
	if 'html' not in {row[1] for row in db.execute('pragma table_info(posts)')}:
		db.execute('alter table posts add column html text')
	command = sys.argv[1] if len(sys.argv) > 1 else 'sync'
	if command == 'search':
		search(db, ' '.join(sys.argv[2:]))
		return
	if command in ('sync', 'list'):
		crawl_list(Client(), db, full=command == 'list' or '--full' in sys.argv)
	if command in ('sync', 'details'):
		fetch_details(db)
	if command in ('sync', 'details', 'images'):
		inline_images(db)


if __name__ == '__main__':
	main()
