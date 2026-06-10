#!/usr/bin/env bash
# Fast local verification: model-free bridge helper tests + extension protocol tests.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="${LCC_TEST_PYTHON:-${LCC_PYTHON:-$ROOT/.venv/bin/python}}"

if [ ! -x "$PY" ]; then
  echo "Python venv를 못 찾음: $PY" >&2
  echo "먼저 ./setup.sh 를 실행하거나, LCC_TEST_PYTHON=/path/to/python 으로 지정하세요." >&2
  exit 1
fi

if ! "$PY" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
then
  echo "검증 Python은 3.10 이상이어야 합니다: $PY" >&2
  "$PY" --version >&2 || true
  exit 1
fi

if ! command -v node >/dev/null 2>&1; then
  echo "node를 찾지 못했습니다. extension/test_protocol.js 검증에 필요합니다." >&2
  exit 1
fi

cd "$ROOT/bridge"
for test_file in \
  test_import_stubs.py \
  test_assembler_decisions.py \
  test_backend_cuda.py \
  test_evs_controller.py \
  test_glossary_repair.py \
  test_latency_profile.py \
  test_model_select.py \
  test_number_guard.py \
  test_policy.py \
  test_scheduler_staleness.py \
  test_text_helpers.py
do
  "$PY" "$test_file"
done

cd "$ROOT"
"$PY" tools/quality_gate.py
"$PY" extension/native-host/test_lcc_bridge_host.py
for js_file in extension/*.js; do
  node --check "$js_file"
done
node extension/test_protocol.js
node extension/test_extension_actions.js
node extension/test_delay_runtime.js
node extension/test_offscreen_runtime.js
