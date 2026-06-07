#!/bin/bash
# A/B the interpretation-policy flags (EVS controller, number guard) on the launchd bridge. Both default ON
# (in server.py); this writes an override into .env.policy (sourced by run_bridge.sh) and restarts the
# bridge (~40s model reload), so you can isolate each flag's effect on real streams.
#
#   bash policy_ab.sh on      # both ON  (LCC_EVS=1 LCC_NUMGUARD=1)
#   bash policy_ab.sh off     # both OFF (LCC_EVS=0 LCC_NUMGUARD=0)
#   bash policy_ab.sh evs     # EVS only (number guard off)
#   bash policy_ab.sh num     # number guard only (EVS off)
#   bash policy_ab.sh reset   # remove the override -> code defaults (both ON)
#   bash policy_ab.sh status
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env.policy"
LABEL="gui/$(id -u)/io.github.teukboong.livecaption.bridge.manual"

case "${1:-status}" in
  on)    printf 'LCC_EVS=1\nLCC_NUMGUARD=1\n' > "$ENV_FILE" ;;
  off)   printf 'LCC_EVS=0\nLCC_NUMGUARD=0\n' > "$ENV_FILE" ;;
  evs)   printf 'LCC_EVS=1\nLCC_NUMGUARD=0\n' > "$ENV_FILE" ;;
  num)   printf 'LCC_EVS=0\nLCC_NUMGUARD=1\n' > "$ENV_FILE" ;;
  reset) rm -f "$ENV_FILE" ;;
  status)
    if [ -f "$ENV_FILE" ]; then echo "policy (override): $(tr '\n' ' ' < "$ENV_FILE")"; else echo "policy: code defaults (both ON)"; fi
    echo "bridge PID: $(lsof -ti tcp:8765 -sTCP:LISTEN 2>/dev/null || echo none)"
    exit 0 ;;
  *) echo "usage: $0 {on|off|evs|num|reset|status}"; exit 1 ;;
esac

echo "policy -> $([ -f "$ENV_FILE" ] && tr '\n' ' ' < "$ENV_FILE" || echo 'code defaults (both ON)')"
launchctl kickstart -k "$LABEL" && echo "bridge reloading (~40s). revert: bash policy_ab.sh reset"
