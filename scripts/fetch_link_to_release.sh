#!/usr/bin/env bash
# Download an official YouTube or X scene clip on the box (yt-dlp only, no render),
# upload it to the sources release as <name>_scene1_h264.mp4, queue the next free
# IST slot, and trigger GitHub Actions render.yml so the pipeline can edit, QC,
# and schedule it through Buffer.
#
# Usage: scripts/fetch_link_to_release.sh <url> <name> [--title "..."] [--hook "..."] [--slot HH:MM] [--no-publish]
#
# Never pass cookies. Trailers/teasers are rejected. Do not use this to download
# copyrighted material you do not have the right to use; official scene clips only.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v yt-dlp >/dev/null 2>&1; then
  echo "yt-dlp is not installed. Install it first (e.g. pip install -U yt-dlp)." >&2
  exit 1
fi
if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI is required to upload the clip and trigger the render." >&2
  exit 1
fi

URL="${1:-}"
NAME="${2:-}"
if [ -z "${1:-}" ] || [ -z "${2:-}" ]; then
  echo "Usage: scripts/fetch_link_to_release.sh <url> <name> [--title T] [--hook H] [--slot HH:MM] [--no-publish]" >&2
  exit 1
fi
shift 2
TITLE=""
HOOK=""
SLOT=""
NO_PUBLISH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --title) TITLE="${2:-}"; shift 2 ;;
    --hook) HOOK="${2:-}"; shift 2 ;;
    --slot) SLOT="${2:-}"; shift 2 ;;
    --no-publish) NO_PUBLISH=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

# Reject obvious trailer names/urls before any download.
low="$(printf '%s %s %s' "$URL" "$NAME" "$TITLE" | tr '[:upper:]' '[:lower:]')"
if echo "$low" | grep -Eq '\b(trailer|teaser|tv[ _-]?spot)\b'; then
  echo "Refusing trailer/teaser link. Give an official scene clip." >&2
  exit 1
fi

SLUG="$(printf '%s' "$NAME" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9' | cut -c1-32)"
if [ -z "$SLUG" ]; then
  echo "name must contain letters or digits" >&2
  exit 1
fi
ASSET="${SLUG}_scene1_h264.mp4"
WORKDIR="$(mktemp -d /tmp/sky-link-XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT
OUT="$WORKDIR/src.mp4"

echo "downloading official clip (no render) to a temp file..."
# YouTube/X only. No cookies. Prefer mp4 <=4K.
if ! yt-dlp --no-progress --no-playlist --retries 3 \
    -f "bv*[height>=1080][height<=2160][ext=mp4]+ba[ext=m4a]/bv*[height>=1080][height<=2160]+ba/b[height>=1080][ext=mp4]/b" \
    --merge-output-format mp4 \
    -o "$OUT" \
    "$URL"; then
  echo "yt-dlp failed. If YouTube asked you to sign in, this script is already running on the box;" >&2
  echo "update yt-dlp and retry, but do not pass account cookies." >&2
  exit 1
fi

if [ ! -s "$OUT" ]; then
  echo "download produced no file" >&2
  exit 1
fi

# Remux to a predictable mp4 name if yt-dlp added an extension variant.
if [ ! -f "$OUT" ]; then
  found="$(ls -1 "$WORKDIR"/src.* 2>/dev/null | head -n1 || true)"
  [ -n "$found" ] && OUT="$found"
fi
UPLOAD="$WORKDIR/$ASSET"
if command -v ffmpeg >/dev/null 2>&1; then
  ffmpeg -y -i "$OUT" -c copy -movflags +faststart "$UPLOAD" >/dev/null 2>&1 || cp "$OUT" "$UPLOAD"
else
  cp "$OUT" "$UPLOAD"
fi

REPO_SLUG="${GITHUB_REPOSITORY:-Bhupi820970/skywalker-shorts}"
echo "uploading $ASSET to the sources release..."
gh release upload sources "$UPLOAD" -R "$REPO_SLUG" --clobber

ARGS=(--url "$URL" --name "$SLUG")
[ -n "$TITLE" ] && ARGS+=(--title "$TITLE")
[ -n "$HOOK" ] && ARGS+=(--hook "$HOOK")
[ -n "$SLOT" ] && ARGS+=(--slot "$SLOT")
[ "$NO_PUBLISH" = 1 ] && ARGS+=(--no-publish)

echo "queueing the next free official slot..."
IDS="$(python3 "$ROOT/scripts/submit_link.py" "${ARGS[@]}" | awk -F= '/^ids=/{print $2}')"
if [ -z "$IDS" ]; then
  echo "submit_link.py did not return an id" >&2
  exit 1
fi

# Commit only the queue + links files this script just wrote, then trigger render.
git add "queue/"*.json "links/requests.json" 2>/dev/null || true
if ! git diff --cached --quiet; then
  git -c user.name=bibi-bot -c user.email=bot@users.noreply.github.com \
    commit -m "queue: official link $IDS"
  git pull --rebase -q || true
  git push
fi

PUB_FLAGS=( -f "ids=$IDS" -f "max=1" )
if [ "$NO_PUBLISH" = 1 ]; then
  PUB_FLAGS+=( -f "no_publish=true" )
fi
echo "triggering render for $IDS ..."
gh workflow run render.yml -R "$REPO_SLUG" "${PUB_FLAGS[@]}"
echo "done. render will edit, QC, and (unless --no-publish) schedule via Buffer."
echo "id=$IDS"
