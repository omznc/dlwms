"""Write the public part of fit.db to a new SQLite file for the "dataset" release.

Usage: python3 ci/dataset.py fit.db dlwms.db

The site already shows these columns. The script drops the email column and the scraper columns,
and adds the release tag and the asset name of each document file.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pages"))
from assets import asset_name, release_tag  # noqa: E402

src, out = sys.argv[1:]
Path(out).unlink(missing_ok=True)
db = sqlite3.connect(out)
db.create_function("release_tag", 1, release_tag)
db.create_function("asset_name", 1, lambda path: asset_name(path) if path else None)
db.execute("attach ? as src", (src,))
db.executescript("""
create table posts as
	select sqs, title, listed_at, date, subject, author, content, html from src.posts;
create table documents as
	select id, study_year, subject, subject_id, school_year, kind, title, filename, path, size, uploaded_at, author, text,
		release_tag(school_year) as release, asset_name(path) as asset
	from src.documents;
""")
db.commit()
db.execute("detach src")
db.execute("vacuum")
print(f"{db.execute('select count(*) from posts').fetchone()[0]} posts, "
      f"{db.execute('select count(*) from documents').fetchone()[0]} documents")
