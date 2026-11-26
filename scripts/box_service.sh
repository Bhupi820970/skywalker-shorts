#!/usr/bin/env bash
# Skywalker box downloader service (pro-edit box_fetch_service.sh design).
# start is idempotent and never kills a healthy loop. After a box reset the watchdog
# (scripts/box_watchdog.sh, hooked into ~/.profile, ~/.bashrc and desktop autostart) starts it again.
#   bash scripts/box_service.sh start|stop|restart|status|logs
# Env (box only, from box.env via box/env.sh): SKY_GH_CONFIG_DIR (gh login), SKY_YTDLP_BROWSER (the box
# browser user-data-dir yt-dlp may read for YouTube / X download auth; session data is never copied
# or logged), SKY_YTDLP_JS_RUNTIME (deno on this box so yt-dlp can select 1080p+ formats).
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../box/env.sh
. "$DIR/box/env.sh"          # settings from box.env (parsed, not sourced); see HANDOFF.md
HOME_DIR="$SKY_BOX_HOME"; mkdir -p "$HOME_DIR"
PID="$HOME_DIR/box_service.pid"; LOG="$HOME_DIR/box_service.log"
TICK="${SKY_BOX_TICK:-300}"
PY="${SKY_BOX_PYTHON:-$HOME_DIR/venv/bin/python}"; [ -x "$PY" ] || PY=python3
TO="$(sky_timeout_bin)"; T300="${TO:+$TO 300}"; T6H="${TO:+$TO 6h}"
# Prefer a real Chrome user-data-dir (Local State + Default) under SKY_BROWSER_PROFILE_ROOT
# (default $HOME/chrome-profile). Cookies are read in place by yt-dlp, never copied or logged.
_sky_ytdlp_browser() {
  local cur="${SKY_YTDLP_BROWSER:-}" root="${SKY_BROWSER_PROFILE_ROOT:-$HOME/chrome-profile}" path="" cand="" pid="" p=""
  case "$cur" in
    chrome:/*) path="${cur#chrome:}" ;;
  esac
  if [ -n "$path" ] && [ -f "$path/Local State" ] && [ -d "$path/Default" ]; then
    printf '%s' "$cur"; return 0
  fi
  pid=$(pgrep -n -f -- "--user-data-dir=$root" 2>/dev/null || true)
  if [ -n "$pid" ] && [ -r "/proc/$pid/cmdline" ]; then
    p=$(tr '\0' '\n' < "/proc/$pid/cmdline" | sed -n 's/^--user-data-dir=//p' | head -n 1)
    if [ -f "$p/Local State" ] && [ -d "$p/Default" ]; then
      printf 'chrome:%s' "$p"; return 0
    fi
  fi
  for cand in "$root/Fork-2" "$root/Fork-3" "$root"; do
    if [ -f "$cand/Local State" ] && [ -d "$cand/Default" ]; then
      printf 'chrome:%s' "$cand"; return 0
    fi
  done
  printf '%s' "$cur"
}
export SKY_YTDLP_BROWSER="$(_sky_ytdlp_browser)"
if [ -z "${SKY_YTDLP_JS_RUNTIME:-}" ] && [ -x "$HOME/.deno/bin/deno" ]; then
  export SKY_YTDLP_JS_RUNTIME="deno:$HOME/.deno/bin/deno"
fi
running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }
case "${1:-status}" in
  start)
    if running; then echo "already running (pid $(cat "$PID"))"; exit 0; fi
    # supervisor loop: sync code (compile-checked) -> one cycle -> sleep. A crash only ends that cycle.
    sky_detach bash -c "cd '$DIR'; while true; do
        $T300 '$PY' scripts/box_service.py sync >> '$LOG' 2>&1
        $T6H '$PY' scripts/box_service.py cycle >> '$LOG' 2>&1
        tail -n 20000 '$LOG' > '$LOG.tmp' 2>/dev/null && mv '$LOG.tmp' '$LOG'
        sleep $TICK; done" > /dev/null 2>&1 < /dev/null &
    echo $! > "$PID"; echo "started (pid $!), log: $LOG";;
  stop)
    if running; then p=$(cat "$PID"); pkill -TERM -s "$p" 2>/dev/null || kill "$p"; rm -f "$PID"; echo stopped; else echo "not running"; fi;;
  restart) bash "$0" stop; sleep 1; bash "$0" start;;
  status)
    if running; then echo "running (pid $(cat "$PID"))"; else echo "not running"; fi
    (cd "$DIR" && ${TO:+$TO 120} "$PY" scripts/box_service.py status 2>/dev/null) || true;;
  logs) tail -n "${2:-60}" "$LOG";;
  *) echo "usage: $0 start|stop|restart|status|logs"; exit 1;;
esac
