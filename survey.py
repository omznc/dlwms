# /// script
# requires-python = ">=3.11"
# dependencies = ["beautifulsoup4", "nh3==0.3.2"]
# ///
"""Count the HTML features of the stored posts and what the sanitizer removes."""
import collections
import os
import re
import sqlite3
import sys
from urllib.parse import urlparse

import nh3
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from index import clean_html  # noqa: E402

db = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else 'fit.db')
rows = db.execute("select rowid, html from posts where html is not null and html != ''").fetchall()
tags, kept, schemes, lost = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
examples = collections.defaultdict(list)
for rowid, html in rows:
	raw = BeautifulSoup(html, 'html.parser')
	clean = BeautifulSoup(clean_html(html) or '', 'html.parser')
	raw_tags = collections.Counter(tag.name for tag in raw.find_all(True))
	clean_tags = collections.Counter(tag.name for tag in clean.find_all(True))
	tags.update(set(raw_tags))
	for name in raw_tags:
		if clean_tags[name] < raw_tags[name]:
			lost[name] += 1
			if len(examples[name]) < 3:
				examples[name].append(rowid)
	for tag in raw.find_all(['a', 'img', 'iframe', 'embed', 'object']):
		url = tag.get('href') or tag.get('src') or ''
		scheme = urlparse(url).scheme or ('relative:' + url.split('/')[0][:30] if url else 'empty')
		schemes[f'{tag.name} {scheme}'] += 1
	if len(raw.get_text(strip=True)) > 20 and len(clean.get_text(strip=True)) < 0.8 * len(raw.get_text(strip=True)):
		lost['TEXT LOSS'] += 1
		examples['TEXT LOSS'].append(rowid)
print(f'{len(rows)} posts with html')
print('posts per tag:', tags.most_common(40))
print('posts where the sanitizer dropped a tag:', lost.most_common(), dict(examples))
print('link and media targets:', schemes.most_common(25))
