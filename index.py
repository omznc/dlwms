# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["fastembed==0.9.1", "sqlite-vec==0.1.9", "numpy", "nh3==0.3.2"]
# ///
"""Build index.db (chunks, FTS5 and vectors) from fit.db and files/."""
import argparse
import hashlib
import re
import sqlite3
import time

from urllib.parse import urljoin

import nh3

from common import DIM, connect, load_model

SIZE, OVERLAP = 1000, 150
POST_BASE = "https://www.fit.ba/student/obavijesti/"
BATCH_CHUNKS = 256

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
  title TEXT, subject TEXT, date TEXT, year INTEGER, author TEXT,
  body TEXT, html TEXT, meta TEXT, hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS items_filter ON items(kind, subject, year);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL, n INTEGER NOT NULL, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_item ON chunks(item_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[{DIM}]);
"""


def chunk(text):
    """Split text into pieces of about SIZE chars with OVERLAP, cut at spaces."""
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    out, i = [], 0
    while i < len(text):
        end = min(i + SIZE, len(text))
        if end < len(text):
            cut = max(text.rfind(s, i + SIZE * 6 // 10, end) for s in ("\n", ". ", " "))
            end = cut + 1 if cut > i else end
        out.append(text[i:end].strip())
        if end >= len(text):
            break
        i = end - OVERLAP
    return out or [""]


def parse_date(*values):
    """Return (iso date or None, year or None) from the first parsable value."""
    for v in values:
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", v or "")
        if m:
            return f"{m[3]}-{m[2]}-{m[1]}", int(m[3])
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", v or "")
        if m:
            return m[0], int(m[1])
        m = re.search(r"\b((?:19|20)\d{2})\b", v or "")
        if m:
            return None, int(m[1])
    return None, None


def digest(*parts):
    return hashlib.sha1("\x00".join(str(p) for p in parts).encode()).hexdigest()


def absolute_url(tag, attr, value):
    if attr not in ("href", "src"):
        return value
    if value.startswith("data:"):
        # Posts embed pasted screenshots as data URLs; allow only images, only in <img>.
        return value if tag == "img" and value.startswith("data:image/") else None
    # Posts link relative to the DLWMS page, which would resolve against this site.
    return urljoin(POST_BASE, value)


def clean_html(html):
    schemes = nh3.ALLOWED_URL_SCHEMES | {"data"}
    return nh3.clean(html or "", tags=nh3.ALLOWED_TAGS | {"tfoot"}, attribute_filter=absolute_url,
                     url_schemes=schemes, link_rel="noopener noreferrer") or None


def read_posts(fit):
    rows = fit.execute("SELECT sqs,title,listed_at,date,subject,author,content,html FROM posts "
                       "WHERE content IS NOT NULL AND content != ''")
    for r in rows:
        iso, year = parse_date(r["listed_at"], r["date"])
        html = clean_html(r["html"])
        yield dict(key="p:" + r["sqs"], kind="post", title=r["title"], subject=r["subject"],
                   date=iso, year=year, author=r["author"], body=r["content"], html=html,
                   meta=r["sqs"], hash=digest(r["title"], r["subject"], iso, r["content"], html))


def read_documents(fit):
    exists = fit.execute("SELECT 1 FROM sqlite_master WHERE name='documents'").fetchone()
    if not exists:
        return
    for r in fit.execute("SELECT * FROM documents WHERE error IS NULL OR error = ''"):
        iso, year = parse_date(r["uploaded_at"], r["school_year"])
        meta = "\x1f".join(str(r[k] or "") for k in ("filename", "path", "size", "uploaded_at", "kind", "school_year"))
        title = r["title"] or r["filename"]
        text = r["text"] or ""
        yield dict(key="d:" + r["id"], kind="document", title=title, subject=r["subject"],
                   date=iso, year=year, author=r["author"], body=text, html=None, meta=meta,
                   hash=digest(title, r["subject"], iso, meta, text))


def header(it):
    return " | ".join(str(x).replace("\n", " ") for x in (it["title"], it["subject"], it["date"]) if x)


def drop_item(db, item_id):
    ids = [r[0] for r in db.execute("SELECT id FROM chunks WHERE item_id=?", (item_id,))]
    for cid in ids:
        db.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
        db.execute("DELETE FROM vec_chunks WHERE rowid=?", (cid,))
    db.execute("DELETE FROM chunks WHERE item_id=?", (item_id,))


def store(db, batch, vectors):
    """Write one batch: items, chunks, FTS rows and vectors."""
    k = 0
    for it, pieces in batch:
        old = db.execute("SELECT id FROM items WHERE key=?", (it["key"],)).fetchone()
        if old:
            drop_item(db, old[0])
        cols = ("key", "kind", "title", "subject", "date", "year", "author", "body", "html", "meta", "hash")
        db.execute(f"INSERT INTO items({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
                   f"ON CONFLICT(key) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols[1:])}",
                   [it[c] for c in cols])
        item_id = db.execute("SELECT id FROM items WHERE key=?", (it["key"],)).fetchone()[0]
        for n, text in enumerate(pieces):
            cid = db.execute("INSERT INTO chunks(item_id,n,text) VALUES(?,?,?)", (item_id, n, text)).lastrowid
            db.execute("INSERT INTO chunks_fts(rowid,text) VALUES(?,?)", (cid, text))
            db.execute("INSERT INTO vec_chunks(rowid,embedding) VALUES(?,?)", (cid, vectors[k].astype("float32").tobytes()))
            k += 1
    db.commit()


def flush(db, model, batch, stats):
    texts = ["passage: " + t for _, pieces in batch for t in pieces]
    t0 = time.time()
    vectors = list(model.embed(texts, batch_size=32))
    stats["secs"] += time.time() - t0
    stats["chunks"] += len(texts)
    store(db, batch, vectors)
    print(f"  {stats['items']} items, {stats['chunks']} chunks, "
          f"{stats['chunks'] / max(stats['secs'], 1e-9):.1f} chunks/s", flush=True)


def changed_items(db, items):
    known = dict(db.execute("SELECT key, hash FROM items").fetchall())
    seen = set()
    for it in items:
        seen.add(it["key"])
        if known.get(it["key"]) != it["hash"]:
            yield it
    for key in set(known) - seen:
        row = db.execute("SELECT id FROM items WHERE key=?", (key,)).fetchone()
        drop_item(db, row[0])
        db.execute("DELETE FROM items WHERE id=?", (row[0],))
    db.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit-db", default="fit.db")
    ap.add_argument("--index-db", default="index.db")
    ap.add_argument("--limit", type=int, help="index at most N new items (for tests)")
    ap.add_argument("--text-only", action="store_true",
                    help="store the item text without chunks or vectors (enough for the static site)")
    args = ap.parse_args()

    fit = connect(args.fit_db, readonly=True)
    db = connect(args.index_db)
    db.executescript(SCHEMA)
    if "html" not in {row[1] for row in db.execute("PRAGMA table_info(items)")}:
        db.execute("ALTER TABLE items ADD COLUMN html TEXT")
    sources = [*read_posts(fit), *read_documents(fit)]
    todo = list(changed_items(db, sources))[: args.limit]
    print(f"{len(todo)} items to embed", flush=True)
    if not todo:
        return
    if args.text_only:
        # An empty hash marks the item as not embedded, so a later full run embeds it.
        store(db, [({**it, "hash": ""}, []) for it in todo], [])
        print(f"done: {len(todo)} items stored without vectors")
        return
    model = load_model()
    stats = {"items": 0, "chunks": 0, "secs": 0.0}
    batch, size = [], 0
    for it in todo:
        pieces = [f"{header(it)}\n{p}" for p in chunk(it["body"])]
        batch.append((it, pieces))
        size += len(pieces)
        stats["items"] += 1
        if size >= BATCH_CHUNKS:
            flush(db, model, batch, stats)
            batch, size = [], 0
    if batch:
        flush(db, model, batch, stats)
    print(f"done: {stats['chunks']} chunks in {stats['secs']:.0f}s of embedding time")


if __name__ == "__main__":
    main()
