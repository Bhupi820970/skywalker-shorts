#!/usr/bin/env bash
# Keeps scripts/box_service.sh alive and starts it after a box reset (no systemd / cron on this box).
#   bash scripts/box_watchdog.sh start|stop|status|install|uninstall
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
. "$DIR/box/env.sh"
SERVICE="$DIR/scripts/box_service.sh"
HOME_DIR="$SKY_BOX_HOME"; mkdir -p "$HOME_DIR"
PID="$HOME_DIR/box_watchdog.pid"; LOG="$HOME_DIR/box_watchdog.log"
INTERVAL="${SKY_BOX_WATCHDOG_INTERVAL:-600}"
MARK="skywalker-box-autostart"
LINE="[ -x $DIR/scripts/box_watchdog.sh ] && $DIR/scripts/box_watchdog.sh start >/dev/null 2>&1"
running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }
install_hooks() {
  local f
  for f in "$HOME/.profile" "$HOME/.bashrc"; do
    [ -f "$f" ] || continue
    grep -q "$MARK" "$f" 2>/dev/null && continue
    printf '\n# %s\n%s\n# %s-end\n' "$MARK" "$LINE" "$MARK" >> "$f"
  done
  if [ "$(uname -s)" = Linux ]; then
    mkdir -p "$HOME/.config/autostart"
    printf '[Desktop Entry]\nType=Application\nName=Skywalker box downloader\nExec=%s start\nX-GNOME-Autostart-enabled=true\nNoDisplay=true\n' \
      "$DIR/scripts/box_watchdog.sh" > "$HOME/.config/autostart/skywalker-box.desktop"
  fi
}
remove_hooks() {   # delete the shell-start hook + desktop autostart (does not stop running loops)
  local f t
  for f in "$HOME/.profile" "$HOME/.bashrc"; do
    [ -f "$f" ] || continue
    t="$(mktemp)"; awk -v m="# $MARK" -v e="# $MARK-end" '$0==m{skip=1;next} $0==e{skip=0;next} !skip' "$f" > "$t" && cat "$t" > "$f"; rm -f "$t"
  done
  rm -f "$HOME/.config/autostart/skywalker-box.desktop"
}
case "${1:-status}" in
  start)
    install_hooks
    if running; then echo "watchdog already running (pid $(cat "$PID"))"; bash "$SERVICE" start >/dev/null; exit 0; fi
    sky_detach bash -c "while true; do bash '$SERVICE' start >> '$LOG' 2>&1; sleep $INTERVAL; done" > /dev/null 2>&1 < /dev/null &
    echo $! > "$PID"; echo "watchdog started (pid $!), every ${INTERVAL}s";;
  stop) if running; then kill "$(cat "$PID")"; rm -f "$PID"; echo "watchdog stopped"; else echo "watchdog not running"; fi;;
  status) if running; then echo "watchdog running (pid $(cat "$PID"))"; else echo "watchdog not running"; fi; bash "$SERVICE" status;;
  install) install_hooks; echo "hooks installed";;
  uninstall) remove_hooks; bash "$0" stop; bash "$SERVICE" stop; echo "hooks removed, loops stopped";;
  *) echo "usage: $0 start|stop|status|install|uninstall"; exit 1;;
esac
