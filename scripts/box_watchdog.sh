#!/usr/bin/env bash
# Keeps scripts/box_service.sh alive and starts it after a box reset (no systemd / cron on this box).
#   bash scripts/box_watchdog.sh start|stop|status|install
set -u
export PATH="/home/box/.local/bin:/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE="$DIR/scripts/box_service.sh"
HOME_DIR="${SKY_BOX_HOME:-/workspace/skywalker-box}"; mkdir -p "$HOME_DIR"
PID="$HOME_DIR/box_watchdog.pid"; LOG="$HOME_DIR/box_watchdog.log"
INTERVAL="${SKY_BOX_WATCHDOG_INTERVAL:-600}"
MARK="skywalker-box-autostart"
LINE="[ -x $DIR/scripts/box_watchdog.sh ] && $DIR/scripts/box_watchdog.sh start >/dev/null 2>&1"
running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }
install_hooks() {
  local f
  for f in /home/box/.profile /home/box/.bashrc; do
    [ -f "$f" ] || continue
    # retire the old skywalker-timer hook (it pointed at the suspended chatgpt820970 repo)
    sed -i 's|^\[ -x /home/box/skywalker-timer/ensure.sh \]|# retired: &|' "$f"
    grep -q "$MARK" "$f" 2>/dev/null && continue
    printf '\n# %s\n%s\n# %s-end\n' "$MARK" "$LINE" "$MARK" >> "$f"
  done
  mkdir -p /home/box/.config/autostart
  printf '[Desktop Entry]\nType=Application\nName=Skywalker box downloader\nExec=%s start\nX-GNOME-Autostart-enabled=true\nNoDisplay=true\n' \
    "$DIR/scripts/box_watchdog.sh" > /home/box/.config/autostart/skywalker-box.desktop
}
case "${1:-status}" in
  start)
    install_hooks
    if running; then echo "watchdog already running (pid $(cat "$PID"))"; bash "$SERVICE" start >/dev/null; exit 0; fi
    nohup setsid bash -c "while true; do bash '$SERVICE' start >> '$LOG' 2>&1; sleep $INTERVAL; done" > /dev/null 2>&1 < /dev/null &
    echo $! > "$PID"; echo "watchdog started (pid $!), every ${INTERVAL}s";;
  stop) if running; then kill "$(cat "$PID")"; rm -f "$PID"; echo "watchdog stopped"; else echo "watchdog not running"; fi;;
  status) if running; then echo "watchdog running (pid $(cat "$PID"))"; else echo "watchdog not running"; fi; bash "$SERVICE" status;;
  install) install_hooks; echo "hooks installed";;
  *) echo "usage: $0 start|stop|status|install"; exit 1;;
esac
