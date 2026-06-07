#!/bin/bash
# Opt-in: run the caption bridge at login + auto-restart on crash, via launchd.
# ⚠ The bridge holds ~26GB RAM while resident. Only enable if you want it always on.
# Usage:  bash autostart.sh install   |   bash autostart.sh uninstall   |   bash autostart.sh status
set -e
LABEL="io.github.teukboong.livecaption"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")" && pwd)"

case "${1:-}" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$DIR/run_bridge.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/livecaption-bridge.log</string>
  <key>StandardErrorPath</key><string>/tmp/livecaption-bridge.log</string>
</dict>
</plist>
EOF
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "설치됨: $PLIST"
    echo "로그: /tmp/livecaption-bridge.log"
    echo "⚠ 로그인 시 자동 실행 + 크래시 시 자동 재시작. 브릿지가 ~26GB RAM을 상주 점유합니다."
    echo "끄려면: bash autostart.sh uninstall"
    ;;
  uninstall)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "제거됨: $PLIST (수동 run_bridge.sh 는 그대로 사용 가능)"
    ;;
  status)
    launchctl list | grep "$LABEL" || echo "미설치 (또는 미실행)"
    ;;
  *)
    echo "usage: bash autostart.sh install|uninstall|status"; exit 1
    ;;
esac
