"""Names of the GitHub releases and assets for the course files. build.py and release_files.py use them."""
import hashlib
import re

# GitHub allows at most 1000 assets in one release.
MAX_ASSETS = 1000


def release_tag(school_year):
    return "files-" + (school_year.replace("/", "-") if school_year else "other")


def asset_name(path):
    # GitHub changes spaces and non-ASCII letters in an asset name, so the name is a hash of the path.
    base = path.rsplit("/", 1)[-1]
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    ext = ext if re.fullmatch(r"[a-z0-9]{1,8}", ext) else "bin"
    return f"{hashlib.sha256(path.encode()).hexdigest()[:20]}.{ext}"
