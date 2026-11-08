# DLWMS arhiva

This is a public archive of the FIT (fit.ba) DLWMS student portal: the news posts (2005-2026) and the course documents.
The site is static. It is at https://dlwms.omarzunic.com. The UI is in Bosnian.

## How it works

- A GitHub Action (`.github/workflows/sync.yml`) runs each day:
  1. It gets the encrypted state (`fit.db`, `index.db`) from the `state` release.
  2. It scrapes the new posts and documents from DLWMS.
  3. It uploads the new course files to one release for each school year (`files-2025-2026` and so on).
  4. It saves the state, builds the static site with Pagefind search and deploys it to GitHub Pages.
- A Cloudflare Worker (`pages/src/index.js`) serves the domain. It gets the pages from GitHub Pages and the files from the releases, and keeps them in the edge cache.

## Download the data

The daily sync updates all the data in the releases:

- `dataset`: `dlwms.db.zst`, a SQLite file with two tables. `posts` holds the news posts. `documents` holds the course document metadata and text.
- `files-<school year>`: the course files. The `release` and `asset` columns of `documents` give the release and the asset name of each file.

To get all the data:

    gh release download dataset -R omznc/dlwms && zstd -d dlwms.db.zst
    for t in $(gh release list -R omznc/dlwms --json tagName -q '.[].tagName' | grep '^files-'); do
      gh release download "$t" -R omznc/dlwms -D "files/$t"
    done

## Files

- `fit_archive.py`: scrape the posts into `fit.db`.
- `documents.py`: download the course documents into `files/` and write the `documents` table in `fit.db`.
- `index.py`: build `index.db` from `fit.db`. The Action uses `--text-only`. A local run without it also adds vectors for `app.py`.
- `pages/build.py`: render `index.db` as the static site.
- `pages/release_files.py`, `pages/assets.py`: upload the course files to the releases, and name the assets.
- `ci/`: the state and placeholder scripts for the Action.
- `app.py`, `common.py`, `templates/`, `static/`, `Dockerfile`, `compose.yaml`: the old server app with vector search. The static site does not use it.

## Secrets

Set these repository secrets:

- `FIT_USERNAME`, `FIT_PASSWORD`: a DLWMS login.
- `STATE_KEY`: the passphrase for the state asset.

## Local run

    FIT_USERNAME=... FIT_PASSWORD=... uv run fit_archive.py
    FIT_USERNAME=... FIT_PASSWORD=... uv run documents.py
    uv run index.py --text-only
    uv run pages/build.py index.db pages/.build
    bunx pagefind@1.6.0 --site pages/.build/site

Deploy the Worker with `cf deploy` in `pages/`.
