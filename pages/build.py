# /// script
# requires-python = ">=3.11"
# dependencies = ["jinja2==3.1.6"]
# ///
"""Render index.db as a static site for GitHub Pages.

Usage: uv run pages/build.py <index.db> <out dir>
Writes <out>/site. Pagefind runs after this script and writes <out>/site/pagefind.
"""
import base64
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import escape

from assets import asset_name, release_tag

HERE = Path(__file__).parent
POST_URL = "https://www.fit.ba/student/obavijesti/opsirnije.aspx?sqs="
DOC_FIELDS = ("filename", "path", "size", "uploaded_at", "kind", "school_year")
KIND = {"post": "obavijest", "document": "dokument"}
RECENT = 20
# svg stays inline: as a same-origin file it could run script.
DATA_IMG = re.compile(r'src="data:image/(png|jpe?g|gif|webp);base64,([A-Za-z0-9+/=\s]+)"')
TITLE = '<h1 class="title" id="title">'
BODIES = ('<div class="prose">', '<div class="prose prose--html">', '<ol class="code">')

env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape())


def extract_images(html, img_dir):
    """Move inline base64 images to files, so Pagefind does not index the base64 text."""
    def save(m):
        ext = "jpg" if m[1].startswith("jp") else m[1]
        data = base64.b64decode(re.sub(r"\s", "", m[2]))
        name = f"{hashlib.sha256(data).hexdigest()[:20]}.{ext}"
        (img_dir / name).write_bytes(data)
        return f'src="/img/{name}"'
    return DATA_IMG.sub(save, html)


def pagefind_tags(it):
    kind = KIND[it["kind"]]
    tags = [("filter", "vrsta", kind), ("meta", "vrsta", kind)]
    if it["subject"]:
        tags += [("filter", "predmet", it["subject"]), ("meta", "predmet", it["subject"])]
    if it["year"]:
        tags.append(("filter", "godina", str(it["year"])))
    if it["date"]:
        tags += [("sort", "datum", it["date"]), ("meta", "datum", it["date"])]
    return "".join(f'<meta data-pagefind-{t}="{k}[content]" content="{escape(v)}">' for t, k, v in tags)


def mark_for_pagefind(page, it):
    """Index only the title and the content, not the navigation or the file facts."""
    assert page.count(TITLE) == 1, it["id"]
    page = page.replace(TITLE, f"{TITLE[:-1]} data-pagefind-body>{pagefind_tags(it)}")
    for tag in BODIES:
        page = page.replace(tag, f"{tag[:-1]} data-pagefind-body>", 1)
    return page


def render_item(it, img_dir):
    it = dict(it)
    if it["html"]:
        it["html"] = extract_images(it["html"], img_dir)
    extra = {}
    if it["kind"] == "post":
        extra["source_url"] = POST_URL + it["meta"]
    else:
        doc = dict(zip(DOC_FIELDS, it["meta"].split("\x1f")))
        doc["size"] = round(int(doc["size"]) / 1024) if doc["size"].isdigit() else None
        extra["doc"] = doc
        if doc["path"]:
            # The Worker gets the release asset and sends it with the file name from the last segment.
            name = doc["filename"] or doc["path"].rsplit("/", 1)[-1]
            extra["download_url"] = f"/files/{release_tag(doc['school_year'])}/{asset_name(doc['path'])}/{quote(name)}"
    return mark_for_pagefind(env.get_template("detail.html").render(it=it, **extra), it)


def render_home(db, total):
    rows = db.execute("SELECT id, kind, title, subject, date, year FROM items").fetchall()
    recent = sorted((r for r in rows if r["date"]), key=lambda r: r["date"], reverse=True)[:RECENT]
    subjects = Counter(r["subject"] for r in rows if r["subject"])
    years = Counter(r["year"] for r in rows if r["year"])
    return env.get_template("search.html").render(
        q="", p={}, results=[], recent=recent, total=total, subjects=sorted(subjects),
        facets={"kind": dict(Counter(KIND[r["kind"]] for r in rows)), "subject": dict(subjects)},
        year_hist=sorted(years.items()), years_json=json.dumps(sorted(years)), pages=0, page=1)


def main(index_db, out):
    site = Path(out) / "site"
    shutil.rmtree(site, ignore_errors=True)
    shutil.copytree(HERE / "static", site / "static")
    (site / "img").mkdir()
    db = sqlite3.connect(f"file:{index_db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    count = 0
    for it in db.execute("SELECT * FROM items"):
        page_dir = site / "item" / str(it["id"])
        page_dir.mkdir(parents=True)
        (page_dir / "index.html").write_text(render_item(it, site / "img"))
        count += 1
    (site / "index.html").write_text(render_home(db, count))
    (site / "404.html").write_text(env.get_template("error.html").render(status_code=404))
    print(f"{count} items, {len(list((site / 'img').iterdir()))} images")


if __name__ == "__main__":
    main(*sys.argv[1:3])
