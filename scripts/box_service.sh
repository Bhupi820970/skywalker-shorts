#!/usr/bin/env bash
# Skywalker box downloader service (pro-edit box_fetch_service.sh design).
# start is idempotent and never kills a healthy loop. After a box reset the watchdog
# (scripts/box_watchdog.sh, hooked into ~/.profile, ~/.bashrc and desktop autostart) starts it again.
#   bash scripts/box_service.sh start|stop|restart|status|logs
# Env (set here, box only): GH_CONFIG_DIR (gh login for Bhupi820970), SKY_YTDLP_BROWSER (the box
# browser user-data-dir yt-dlp may read for YouTube / X download auth; session data is never copied
# or logged), SKY_YTDLP_JS_RUNTIME (deno on this box so yt-dlp can select 1080p+ formats).
set -u
export PATH="/home/box/.local/bin:/home/box/.deno/bin:/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${SKY_BOX_HOME:-/workspace/skywalker-box}"; mkdir -p "$HOME_DIR"
PID="$HOME_DIR/box_service.pid"; LOG="$HOME_DIR/box_service.log"
TICK="${SKY_BOX_TICK:-300}"
PY="${SKY_BOX_PYTHON:-$HOME_DIR/venv/bin/python}"; [ -x "$PY" ] || PY=python3
export SKY_BOX_HOME="$HOME_DIR" GH_PROMPT_DISABLED=1
export GH_CONFIG_DIR="${SKY_GH_CONFIG_DIR:-/workspace/.ghnew}"
# Prefer a real Chrome user-data-dir (Local State + Default). The box desktop browser uses
# chrome-profile/Fork-N, not chrome-profile/Default (a stale empty profile).
_sky_ytdlp_browser() {
  local cur="${SKY_YTDLP_BROWSER:-}" path="" cand="" pid="" p=""
  case "$cur" in
    chrome:/*) path="${cur#chrome:}" ;;
  esac
  if [ -n "$path" ] && [ -f "$path/Local State" ] && [ -d "$path/Default" ]; then
    printf '%s' "$cur"; return 0
  fi
  pid=$(pgrep -n -f -- '--user-data-dir=/home/box/chrome-profile' 2>/dev/null || true)
  if [ -n "$pid" ] && [ -r "/proc/$pid/cmdline" ]; then
    p=$(tr '\0' '\n' < "/proc/$pid/cmdline" | sed -n 's/^--user-data-dir=//p' | head -n 1)
    if [ -f "$p/Local State" ] && [ -d "$p/Default" ]; then
      printf 'chrome:%s' "$p"; return 0
    fi
  fi
  for cand in /home/box/chrome-profile/Fork-2 /home/box/chrome-profile/Fork-3 /home/box/chrome-profile; do
    if [ -f "$cand/Local State" ] && [ -d "$cand/Default" ]; then
      printf 'chrome:%s' "$cand"; return 0
    fi
  done
  printf '%s' "${cur:-chrome:/home/box/chrome-profile/Fork-2}"
}
export SKY_YTDLP_BROWSER="$(_sky_ytdlp_browser)"
if [ -x /home/box/.deno/bin/deno ]; then
  export SKY_YTDLP_JS_RUNTIME="${SKY_YTDLP_JS_RUNTIME:-deno:/home/box/.deno/bin/deno}"
fi
running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }
case "${1:-status}" in
  start)
    if running; then echo "already running (pid $(cat "$PID"))"; exit 0; fi
    # supervisor loop: sync code (compile-checked) -> one cycle -> sleep. A crash only ends that cycle.
    nohup setsid bash -c "cd '$DIR'; while true; do
        timeout 300 '$PY' scripts/box_service.py sync >> '$LOG' 2>&1
        timeout 6h '$PY' scripts/box_service.py cycle >> '$LOG' 2>&1
        tail -n 20000 '$LOG' > '$LOG.tmp' 2>/dev/null && mv '$LOG.tmp' '$LOG'
        sleep $TICK; done" > /dev/null 2>&1 < /dev/null &
    echo $! > "$PID"; echo "started (pid $!), log: $LOG";;
  stop)
    if running; then p=$(cat "$PID"); pkill -TERM -s "$p" 2>/dev/null || kill "$p"; rm -f "$PID"; echo stopped; else echo "not running"; fi;;
  restart) bash "$0" stop; sleep 1; bash "$0" start;;
  status)
    if running; then echo "running (pid $(cat "$PID"))"; else echo "not running"; fi
    (cd "$DIR" && timeout 120 "$PY" scripts/box_service.py status 2>/dev/null) || true;;
  logs) tail -n "${2:-60}" "$LOG";;
  *) echo "usage: $0 start|stop|restart|status|logs"; exit 1;;
esac
