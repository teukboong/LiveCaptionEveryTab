#!/bin/bash
# Install (or uninstall) the Live Caption native-messaging host so the extension popup can
# start/stop the local bridge. One-time setup; re-run any time. macOS browsers.
#   install-host.sh            # install for all installed Chromium browsers
#   install-host.sh uninstall  # remove the host manifest
set -u
HOST_NAMES=("io.github.teukboong.livecaption")
LEGACY_HOST_NAMES=("com.hesperides.livecaption")        # pre-release alias: cleaned up on uninstall, never installed
PRIMARY_HOST_NAME="${HOST_NAMES[0]}"
DIR="$(cd "$(dirname "$0")" && pwd)"
HOST_PY="$DIR/lcc_bridge_host.py"
SUPPORT="$HOME/Library/Application Support"

# Per-browser NativeMessagingHosts directories (parent must exist = browser installed).
BROWSER_DIRS=(
  "$SUPPORT/Google/Chrome/NativeMessagingHosts"
  "$SUPPORT/Google/Chrome Beta/NativeMessagingHosts"
  "$SUPPORT/Google/Chrome Canary/NativeMessagingHosts"
  "$SUPPORT/Google/Chrome for Testing/NativeMessagingHosts"
  "$SUPPORT/Google/ChromeForTesting/NativeMessagingHosts"
  "$SUPPORT/Chromium/NativeMessagingHosts"
  "$SUPPORT/Arc/User Data/NativeMessagingHosts"
  "$SUPPORT/BraveSoftware/Brave-Browser/NativeMessagingHosts"
  "$SUPPORT/Microsoft Edge/NativeMessagingHosts"
  "$SUPPORT/OpenAI/ChatGPT Atlas/NativeMessagingHosts"
  "$SUPPORT/com.openai.atlas/browser-data/host/NativeMessagingHosts"
  "$SUPPORT/Vivaldi/NativeMessagingHosts"
  "$SUPPORT/com.operasoftware.Opera/NativeMessagingHosts"
)

# Chromium instances launched with --user-data-dir may look for native hosts inside that
# profile root. Detect currently running custom profiles so one-off browser profiles work too.
while IFS= read -r user_data_dir; do
  [ -n "$user_data_dir" ] || continue
  BROWSER_DIRS+=("$user_data_dir/NativeMessagingHosts")
done < <(
  ps -axo args= 2>/dev/null |
    grep -E 'Google Chrome|ChromeForTesting|Chrome for Testing|Chromium|Brave|Microsoft Edge|Arc|Vivaldi|Opera|ChatGPT Atlas' |
    sed -n 's/.*--user-data-dir=\([^ ]*\).*/\1/p' |
    sort -u
)

if [ "${1:-}" = "uninstall" ]; then
  n=0
  for d in "${BROWSER_DIRS[@]}"; do
    for host_name in "${HOST_NAMES[@]}" "${LEGACY_HOST_NAMES[@]}"; do
      f="$d/$host_name.json"
      [ -f "$f" ] && rm -f "$f" && echo "삭제: $f" && n=$((n+1))
    done
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
  for host_name in "${HOST_NAMES[@]}"; do
    # write the manifest with the absolute host path and host alias substituted in
    sed \
      -e "s#__HOST_PATH__#$HOST_PY#" \
      -e "s#\"name\": \"$PRIMARY_HOST_NAME\"#\"name\": \"$host_name\"#" \
      "$DIR/$PRIMARY_HOST_NAME.json" > "$d/$host_name.json"
    echo "설치: $d/$host_name.json"
    installed=$((installed+1))
  done
done

if [ "$installed" -eq 0 ]; then
  echo "⚠ 설치된 Chromium 계열 브라우저를 못 찾음. Chrome/Brave/Edge 설치 후 다시 실행." >&2
  exit 1
fi
echo
echo "✅ 네이티브 호스트 설치 완료 (${installed}곳). 확장을 chrome://extensions 에서 '↻ 새로고침' 하면"
echo "   팝업의 '브릿지 켜기' 버튼이 동작합니다. (확장 ID는 ddcflpihicaobncgpmadoipiofpllgnl 로 고정)"
