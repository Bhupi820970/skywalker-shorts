#!/usr/bin/env bash
# Shared box settings for scripts/box_service.sh and scripts/box_watchdog.sh (Linux or macOS).
# Settings come from a KEY=value file that is PARSED, never sourced:
#   $SKY_BOX_ENV, else <box home>/box.env, else $HOME/.config/skywalker/box.env
# Box home (SKY_BOX_HOME) defaults to the folder that holds the repo clone.
# Only the keys below are accepted; values must be plain paths / names.
SKY_REPO_DIR="${SKY_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
_sky_keys=" SKY_BOX_HOME SKY_GH_CONFIG_DIR SKY_GH_REPO SKY_YTDLP_BROWSER SKY_YTDLP_JS_RUNTIME SKY_BOX_PYTHON SKY_BOX_TICK SKY_BOX_WATCHDOG_INTERVAL SKY_MUSIC_CACHE SKY_BOX_DISPATCH SKY_BOX_PUBLISH SKY_BROWSER_PROFILE_ROOT "
_sky_load() {
  local f="$1" k v line
  [ -f "$f" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; esac
    k="${line%%=*}"; v="${line#*=}"
    v="${v%\"}"; v="${v#\"}"
    case "$_sky_keys" in *" $k "*) ;; *) continue ;; esac
    [[ "$v" =~ ^[A-Za-z0-9:/._+@~-]{1,300}$ ]] || continue
    v="${v/#\~/$HOME}"
    [ -n "${!k:-}" ] || export "$k=$v"     # values already in the environment win
  done < "$f"
  return 0
}
_sky_parent="$(cd "$SKY_REPO_DIR/.." && pwd)"
if [ -n "${SKY_BOX_ENV:-}" ]; then _sky_load "$SKY_BOX_ENV"
else _sky_load "${SKY_BOX_HOME:-$_sky_parent}/box.env" || _sky_load "$HOME/.config/skywalker/box.env"; fi
export SKY_BOX_HOME="${SKY_BOX_HOME:-$_sky_parent}"
export PATH="$HOME/.local/bin:$HOME/.deno/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}"
[ -n "${SKY_GH_CONFIG_DIR:-}" ] && export GH_CONFIG_DIR="$SKY_GH_CONFIG_DIR"
export GH_PROMPT_DISABLED=1
# timeout on Linux, gtimeout on macOS (brew install coreutils)
sky_timeout_bin() { command -v timeout 2>/dev/null || command -v gtimeout 2>/dev/null || echo ""; }
# start a detached background loop (setsid on Linux; plain nohup where setsid is missing, e.g. macOS)
sky_detach() { if command -v setsid >/dev/null 2>&1; then nohup setsid "$@"; else nohup "$@"; fi; }
