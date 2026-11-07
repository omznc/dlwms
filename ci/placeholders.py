"""Write an empty file for each document that fit.db has, before a CI sync.

The real files are in the GitHub releases. documents.py must see the old names, or a new file
can get the path of an old file. The extract step reads only files without text, so no placeholder.
"""
import os
import sqlite3
import sys

fit_db, files_dir = sys.argv[1:3]
db = sqlite3.connect(f"file:{fit_db}?mode=ro", uri=True)
pending = db.execute("SELECT count(*) FROM documents WHERE fetched = 1 AND path IS NOT NULL AND text IS NULL").fetchone()[0]
assert pending == 0, f"{pending} files have no extracted text; extract them where the real files are"
count = 0
for (path,) in db.execute("SELECT path FROM documents WHERE path IS NOT NULL"):
    target = os.path.join(files_dir, path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if not os.path.exists(target):
        open(target, "x").close()
        count += 1
print(f"{count} placeholders")
