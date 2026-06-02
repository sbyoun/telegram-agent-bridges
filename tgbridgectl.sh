#!/usr/bin/env bash
# tgbridgectl.sh — manage unified Telegram bridge instances via systemd --user.
# Each instance = one bot/provider (instances/<name>/.env), one templated unit.
#
# Usage:
#   ./tgbridgectl.sh install                 # install template unit + enable linger
#   ./tgbridgectl.sh start   <instance>      # start+enable tgbridge@<instance>
#   ./tgbridgectl.sh stop    <instance>
#   ./tgbridgectl.sh restart <instance>
#   ./tgbridgectl.sh status  [instance]
#   ./tgbridgectl.sh logs    <instance> [n]
#   ./tgbridgectl.sh list                    # known instances
#   ./tgbridgectl.sh run     <instance>      # foreground (debug)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_TMPL="tgbridge@.service"
UNIT_SRC="$SCRIPT_DIR/systemd/$UNIT_TMPL"
USER_UNIT_DIR="$HOME/.config/systemd/user"

cmd="${1:-status}"; shift || true
case "$cmd" in
  install)
    mkdir -p "$USER_UNIT_DIR"
    cp "$UNIT_SRC" "$USER_UNIT_DIR/$UNIT_TMPL"
    systemctl --user daemon-reload
    loginctl enable-linger "$(whoami)" 2>/dev/null || true
    echo "installed $UNIT_TMPL (linger on)"
    ;;
  start)
    inst="${1:?usage: start <instance>}"
    systemctl --user enable --now "tgbridge@$inst.service"
    echo "started+enabled tgbridge@$inst"
    ;;
  stop)
    inst="${1:?usage: stop <instance>}"
    systemctl --user disable --now "tgbridge@$inst.service" 2>/dev/null || systemctl --user stop "tgbridge@$inst.service"
    echo "stopped tgbridge@$inst"
    ;;
  restart)
    inst="${1:?usage: restart <instance>}"
    systemctl --user restart "tgbridge@$inst.service"; echo "restarted tgbridge@$inst"
    ;;
  status)
    if [[ $# -ge 1 ]]; then
      systemctl --user --no-pager status "tgbridge@$1.service" || true
    else
      systemctl --user list-units --type=service 2>/dev/null | grep -E "tgbridge@" || echo "(no tgbridge instances running)"
    fi
    ;;
  logs)
    inst="${1:?usage: logs <instance> [n]}"; n="${2:-60}"
    tail -n "$n" "$SCRIPT_DIR/instances/$inst/bridge.log" 2>/dev/null || echo "(no log for $inst yet)"
    ;;
  list)
    for d in "$SCRIPT_DIR"/instances/*/; do
      name="$(basename "$d")"
      prov="$(grep -E '^BRIDGE_PROVIDER=' "$d/.env" 2>/dev/null | cut -d= -f2)"
      echo "  $name  (provider=${prov:-?})"
    done
    ;;
  run)
    inst="${1:?usage: run <instance>}"; exec "$SCRIPT_DIR/run-tgbridge.sh" "$inst";;
  *) echo "usage: $0 {install|start|stop|restart|status|logs|list|run} [instance]"; exit 1;;
esac
