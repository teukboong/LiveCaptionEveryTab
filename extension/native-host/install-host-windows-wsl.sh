#!/usr/bin/env bash
# Install/uninstall the Windows Chrome native-messaging host for a WSL2 live-caption setup.
# Run inside WSL. It writes a Windows .cmd host + manifest under %LOCALAPPDATA% and registers
# the manifest in HKCU for Chromium-family browsers.
set -euo pipefail

HOST_NAME="io.github.teukboong.livecaption"
DIR="$(cd "$(dirname "$0")" && pwd)"
HOST_PY="$DIR/lcc_bridge_host.py"

REG_EXE="${REG_EXE:-/mnt/c/Windows/System32/reg.exe}"
CMD_EXE="${CMD_EXE:-/mnt/c/Windows/System32/cmd.exe}"

if [ "${1:-}" = "uninstall" ]; then
  for key in \
    "HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\$HOST_NAME" \
    "HKCU\\Software\\Chromium\\NativeMessagingHosts\\$HOST_NAME" \
    "HKCU\\Software\\BraveSoftware\\Brave-Browser\\NativeMessagingHosts\\$HOST_NAME" \
    "HKCU\\Software\\Microsoft\\Edge\\NativeMessagingHosts\\$HOST_NAME"
  do
    "$REG_EXE" DELETE "$key" /f >/dev/null 2>&1 || true
  done
  echo "Windows native host registry 제거 완료."
  exit 0
fi

[ -x "$REG_EXE" ] || { echo "reg.exe 없음: $REG_EXE" >&2; exit 1; }
[ -x "$CMD_EXE" ] || { echo "cmd.exe 없음: $CMD_EXE" >&2; exit 1; }
[ -f "$HOST_PY" ] || { echo "호스트 스크립트 없음: $HOST_PY" >&2; exit 1; }

LOCALAPPDATA_WIN="$("$CMD_EXE" /c "echo %LOCALAPPDATA%" 2>/dev/null | tr -d '\r' | tail -1)"
[ -n "$LOCALAPPDATA_WIN" ] || { echo "%LOCALAPPDATA% 확인 실패" >&2; exit 1; }
LOCALAPPDATA_WSL="$(wslpath -u "$LOCALAPPDATA_WIN")"
INSTALL_DIR="$LOCALAPPDATA_WSL/LiveCaptionEveryTab"
mkdir -p "$INSTALL_DIR"

DISTRO="${LCC_WSL_DISTRO:-}"
WSL_USER="${LCC_WSL_USER:-}"
ROOT="${LCC_ROOT:-$(cd "$DIR/../.." && pwd)}"
STACK_CMD="${LCC_CUDA_STACK_CMD:-$ROOT/bridge/cuda/lcc_cuda_stack.sh}"
PY="${LCC_NATIVE_PYTHON:-/usr/bin/python3}"

[ -x "$STACK_CMD" ] || { echo "CUDA stack script 없음: $STACK_CMD" >&2; exit 1; }

HOST_CMD="$INSTALL_DIR/$HOST_NAME.cmd"
MANIFEST="$INSTALL_DIR/$HOST_NAME.json"
DISTRO_ARG=""
USER_ARG=""
[ -n "$DISTRO" ] && DISTRO_ARG="-d \"$DISTRO\" "
[ -n "$WSL_USER" ] && USER_ARG="-u \"$WSL_USER\" "

cat > "$HOST_CMD" <<EOF
@echo off
"C:\\Windows\\System32\\wsl.exe" ${DISTRO_ARG}${USER_ARG}--exec /usr/bin/env LCC_CUDA_STACK_CMD="$STACK_CMD" LCC_ROOT="$ROOT" "$PY" "$HOST_PY"
EOF
chmod +x "$HOST_CMD"

HOST_CMD_WIN_ESC="$(wslpath -w "$HOST_CMD" | sed 's/\\/\\\\/g')"
cat > "$MANIFEST" <<EOF
{
  "name": "$HOST_NAME",
  "description": "Live Caption CUDA bridge launcher via WSL",
  "path": "$HOST_CMD_WIN_ESC",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://ddcflpihicaobncgpmadoipiofpllgnl/"
  ]
}
EOF

MANIFEST_WIN="$(wslpath -w "$MANIFEST")"
for key in \
  "HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\$HOST_NAME" \
  "HKCU\\Software\\Chromium\\NativeMessagingHosts\\$HOST_NAME" \
  "HKCU\\Software\\BraveSoftware\\Brave-Browser\\NativeMessagingHosts\\$HOST_NAME" \
  "HKCU\\Software\\Microsoft\\Edge\\NativeMessagingHosts\\$HOST_NAME"
do
  "$REG_EXE" ADD "$key" /ve /t REG_SZ /d "$MANIFEST_WIN" /f >/dev/null
done

echo "Windows native host 설치 완료."
echo "manifest: $MANIFEST_WIN"
echo "host cmd: $(wslpath -w "$HOST_CMD")"
echo "distro: $DISTRO"
echo "user: $WSL_USER"
echo "stack: $STACK_CMD"
echo "Chrome 확장을 chrome://extensions 에서 새로고침하면 팝업의 브릿지 버튼이 WSL CUDA stack을 제어합니다."
