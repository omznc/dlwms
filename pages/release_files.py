# /// script
# requires-python = ">=3.11"
# ///
"""Upload the course files that are not in a GitHub release yet.

Usage: uv run pages/release_files.py <fit.db> <files dir> <owner/repo>
Each school year has one release (for example files-2023-2024). A restart skips the uploaded assets.
A file of 0 bytes with a larger size in fit.db is a CI placeholder: the file is in a release already.
"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from assets import MAX_ASSETS, asset_name, release_tag

BATCH = 20


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def existing_assets(repo, tag):
    try:
        release = json.loads(gh("api", f"repos/{repo}/releases/tags/{tag}"))
    except subprocess.CalledProcessError:
        gh("release", "create", tag, "-R", repo, "--latest=false", "--title", tag,
           "--notes", "The course files of one school year. The asset name is a hash of the file path.")
        return set()
    return set(gh("api", "--paginate", f"repos/{repo}/releases/{release['id']}/assets?per_page=100", "-q", ".[].name").split())


def upload(repo, tag, todo):
    with tempfile.TemporaryDirectory() as tmp:
        # gh uses the base name of the path as the asset name, so link each file under its asset name.
        links = []
        for name, src in todo:
            link = Path(tmp, name)
            link.symlink_to(src.resolve())
            links.append(str(link))
        for i in range(0, len(links), BATCH):
            for attempt in range(4):
                try:
                    gh("release", "upload", tag, "-R", repo, "--clobber", *links[i:i + BATCH])
                    break
                except subprocess.CalledProcessError as error:
                    if attempt == 3:
                        raise
                    print(f"retry {tag}: {error.stderr.strip()[:200]}", flush=True)
                    time.sleep(60 * (attempt + 1))
            print(f"{tag}: {min(i + BATCH, len(links))}/{len(links)}", flush=True)


def missing(rows, have, files_dir, lost):
    """Return (asset name, file) for each row without an asset. Add the rows without a real file to lost."""
    todo = []
    for path, size in rows:
        name, src = asset_name(path), Path(files_dir, path)
        if name in have:
            continue
        if not src.is_file() or (src.stat().st_size == 0 and size > 0):
            lost.append(path)
            continue
        todo.append((name, src))
    return todo


def main(fit_db, files_dir, repo):
    db = sqlite3.connect(f"file:{fit_db}?mode=ro", uri=True)
    by_tag = defaultdict(list)
    for path, year, size in db.execute("SELECT path, school_year, size FROM documents WHERE path IS NOT NULL"):
        by_tag[release_tag(year)].append((path, size or 0))
    lost = []
    for tag, rows in sorted(by_tag.items()):
        names = {asset_name(path) for path, _ in rows}
        assert len(names) == len(rows), f"{tag}: two paths give one asset name"
        assert len(rows) < MAX_ASSETS, f"{tag}: {len(rows)} files, split the release"
        have = existing_assets(repo, tag)
        todo = missing(rows, have, files_dir, lost)
        print(f"{tag}: {len(rows)} files, {len(have)} in the release, {len(todo)} to upload", flush=True)
        upload(repo, tag, todo)
    if lost:
        sys.exit(f"{len(lost)} files are in no release and not on disk, for example {lost[:3]}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
