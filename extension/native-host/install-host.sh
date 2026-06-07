#!/bin/bash
# Install (or uninstall) the Live Caption native-messaging host so the extension popup can
# start/stop the local bridge. One-time setup; re-run any time. macOS browsers.
#   install-host.sh            # install for all installed Chromium browsers
#   install-host.sh uninstall  # remove the host manifest
set -u
HOST_NAME="io.github.teukboong.livecaption"
DIR="$(cd "$(dirname "$0")" && pwd)"
HOST_PY="$DIR/lcc_bridge_host.py"
SUPPORT="$HOME/Library/Application Support"

# Per-browser NativeMessagingHosts directories (parent must exist = browser installed).
BROWSER_DIRS=(
  "$SUPPORT/Google/Chrome/NativeMessagingHosts"
  "$SUPPORT/Google/Chrome Beta/NativeMessagingHosts"
  "$SUPPORT/Google/Chrome Canary/NativeMessagingHosts"
  "$SUPPORT/Chromium/NativeMessagingHosts"
  "$SUPPORT/BraveSoftware/Brave-Browser/NativeMessagingHosts"
  "$SUPPORT/Microsoft Edge/NativeMessagingHosts"
)

if [ "${1:-}" = "uninstall" ]; then
  n=0
  for d in "${BROWSER_DIRS[@]}"; do
    f="$d/$HOST_NAME.json"
    [ -f "$f" ] && rm -f "$f" && echo "삭제: $f" && n=$((n+1))
  done
  echo "제거 완료 (${n}곳)."
  exit 0
fi

if [ ! -f "$HOST_PY" ]; then echo "호스트 스크립트 없음: $HOST_PY" >&2; exit 1; fi
chmod +x "$HOST_PY"

installed=0
for d in "${BROWSER_DIRS[@]}"; do
  parent="$(dirname "$d")"                 # e.g. .../Google/Chrome
  [ -d "$parent" ] || continue             # browser not installed -> skip
  mkdir -p "$d"
  # write the manifest with the absolute host path substituted in
  sed "s#__HOST_PATH__#$HOST_PY#" "$DIR/$HOST_NAME.json" > "$d/$HOST_NAME.json"
  echo "설치: $d/$HOST_NAME.json"
  installed=$((installed+1))
done

if [ "$installed" -eq 0 ]; then
  echo "⚠ 설치된 Chromium 계열 브라우저를 못 찾음. Chrome/Brave/Edge 설치 후 다시 실행." >&2
  exit 1
fi
echo
echo "✅ 네이티브 호스트 설치 완료 (${installed}곳). 확장을 chrome://extensions 에서 '↻ 새로고침' 하면"
echo "   팝업의 '브릿지 켜기' 버튼이 동작합니다. (확장 ID는 ddcflpihicaobncgpmadoipiofpllgnl 로 고정)"
