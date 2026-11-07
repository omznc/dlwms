"""Download every course file from the old R2 Worker, for the first upload to the releases.

Usage: python3 ci/seed_files.py <fit.db> <files dir> <old site URL>
The old Worker stores ".." in a key as ".". Each file must have the size that fit.db has.
"""
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

fit_db, files_dir, site = sys.argv[1:4]
rows = sqlite3.connect(f"file:{fit_db}?mode=ro", uri=True).execute(
    "SELECT path, size FROM documents WHERE path IS NOT NULL").fetchall()


def fetch(row):
    path, size = row
    # Cloudflare refuses the default Python-urllib User-Agent with a 403.
    url = urllib.request.Request(f"{site}/files/{urllib.parse.quote(path.replace('..', '.'))}",
                                 headers={"User-Agent": "dlwms-seed"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=300) as response:
                data = response.read()
            break
        except OSError as error:
            if attempt == 2:
                return f"{path}: {error}"
    if len(data) != size:
        return f"{path}: {len(data)} bytes, fit.db has {size}"
    target = os.path.join(files_dir, path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as f:
        f.write(data)
    return None


with ThreadPoolExecutor(16) as pool:
    errors = [e for e in pool.map(fetch, rows) if e]
print(f"{len(rows) - len(errors)} of {len(rows)} files")
if errors:
    sys.exit("\n".join(errors[:20]))
