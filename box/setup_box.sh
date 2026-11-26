#!/usr/bin/env bash
# One-time setup of the Skywalker box downloader on a new Linux or macOS computer.
# Does NOT start the service: run `bash scripts/box_watchdog.sh start` yourself afterwards (ONE computer only).
set -eu
DIR="$(cd "$(dirname "$0")/.." && pwd)"
BOXHOME="${SKY_BOX_HOME:-$(cd "$DIR/.." && pwd)}"
miss=0
for t in git python3 ffmpeg ffprobe gh; do command -v "$t" >/dev/null 2>&1 || { echo "MISSING: $t"; miss=1; }; done
command -v deno >/dev/null 2>&1 || [ -x "$HOME/.deno/bin/deno" ] || echo "warning: deno not found (curl -fsSL https://deno.land/install.sh | sh) - yt-dlp needs it for 1080p+"
command -v timeout >/dev/null 2>&1 || command -v gtimeout >/dev/null 2>&1 || echo "warning: no timeout/gtimeout (macOS: brew install coreutils)"
[ "$miss" = 0 ] || { echo "install the missing tools first"; exit 1; }
if [ ! -x "$BOXHOME/venv/bin/python" ]; then python3 -m venv "$BOXHOME/venv"; fi
"$BOXHOME/venv/bin/python" -m pip install -q --upgrade pip
"$BOXHOME/venv/bin/python" -m pip install -q -r "$DIR/box/requirements-box.txt"
[ -f "$BOXHOME/box.env" ] || { cp "$DIR/box/box.env.example" "$BOXHOME/box.env"; chmod 600 "$BOXHOME/box.env"; echo "created $BOXHOME/box.env - edit it"; }
gh auth status >/dev/null 2>&1 && echo "gh: logged in" || echo "gh: run 'gh auth login' (account that owns the repo, repo+workflow scopes)"
echo "box home: $BOXHOME"
echo "next: bash scripts/box_watchdog.sh start   (on ONE computer only)"
