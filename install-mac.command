#!/bin/bash
# Double-click on macOS to set up Live Caption Every Tab: creates .venv, installs deps, and registers the
# popup's native-messaging host. After this, everything (bridge on/off, model tiers) is done from the popup.
# First time: if double-click is blocked by Gatekeeper, right-click this file -> Open -> Open.
#
#   double-click            # backend auto, deps only (models fetched later from the popup)
#   ./install-mac.command --models            # also fetch the auto tier now (disk-frugal)
#   ./install-mac.command --models --tier lite
cd "$(dirname "$0")" || exit 1
clear
echo "Live Caption Every Tab — macOS 설치"
echo "===================================="
echo
if ! command -v python3 >/dev/null 2>&1; then
  echo "⚠ Python 3가 필요합니다."
  echo "  https://www.python.org/downloads/ 에서 설치하거나, 터미널에서:  brew install python@3.13"
  echo
  read -r -p "엔터를 누르면 닫힙니다…" _
  exit 1
fi
bash ./setup.sh "$@"
rc=$?
echo
if [ "$rc" = 0 ]; then
  echo "✅ 설치 완료."
  echo "   1) Chrome 주소창에 chrome://extensions"
  echo "   2) 우측 상단 '개발자 모드' 켜기"
  echo "   3) '압축해제된 확장 프로그램을 로드' → 이 폴더의 extension/ 선택"
  echo "   이후엔 확장 팝업에서 브릿지 켜기 · 모델(Full/Mid/Lite)까지 전부 됩니다."
else
  echo "❌ 설치 중 문제 (코드 $rc). 위 메시지를 확인하세요."
fi
echo
read -r -p "엔터를 누르면 이 창이 닫힙니다…" _
exit "$rc"
