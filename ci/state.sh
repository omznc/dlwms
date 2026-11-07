#!/usr/bin/env bash
# Get or save fit.db and index.db as an asset of the "state" release.
# Usage: ci/state.sh pull | push   (in the directory that holds the two files)
# The asset is encrypted with STATE_KEY: a release of a public repo is public, and fit.db holds emails and grades.
set -euo pipefail
: "${STATE_KEY:?set STATE_KEY}"
repo=${GITHUB_REPOSITORY:-omznc/dlwms}
gpg_key=(gpg --batch --quiet --pinentry-mode loopback --passphrase-fd 3)

case ${1:-} in
pull)
	# The asset names have a UTC time stamp, so the last name in sort order is the newest.
	name=$(gh release view state -R "$repo" --json assets -q '.assets[].name' | sort | tail -n 1)
	[[ -n $name ]] || { echo "no state asset" >&2; exit 1; }
	gh release download state -R "$repo" -p "$name" -O - | "${gpg_key[@]}" --decrypt 3<<<"$STATE_KEY" | zstd -dq | tar -x
	echo "pulled $name"
	;;
push)
	name=state-$(date -u +%Y%m%d-%H%M%S).tar.zst.gpg
	tmp=$(mktemp -d)
	trap 'rm -rf "$tmp"' EXIT
	tar -c fit.db index.db | zstd -q -T0 -10 | "${gpg_key[@]}" --symmetric --cipher-algo AES256 -o "$tmp/$name" 3<<<"$STATE_KEY"
	gh release view state -R "$repo" >/dev/null 2>&1 ||
		gh release create state -R "$repo" --latest=false --title state --notes "Encrypted scraper state for the daily sync."
	gh release upload state -R "$repo" "$tmp/$name"
	# Keep the two newest, so a bad run does not remove the last good state.
	gh release view state -R "$repo" --json assets -q '.assets[].name' | sort | head -n -2 |
		xargs -r -I{} gh release delete-asset state {} -R "$repo" -y
	echo "pushed $name ($(du -h "$tmp/$name" | cut -f1))"
	;;
*)
	echo "usage: $0 pull | push" >&2
	exit 2
	;;
esac
