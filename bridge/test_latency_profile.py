"""Model-free unit tests for the latency-mode tuning profile (_lat_* in server.py).

These pin the shared LatencyProfile behavior used by the live scheduler.
Importing server does NOT load any model — load_models()/warm/serve run only under `__main__` — so this is
fast and safe to run anytime:

    cd bridge && python test_latency_profile.py

server.py's top-level imports (silero_vad, mlx, …) live in the bridge venv, so use that interpreter
(or set LCC_PYTHON). Exit 0 + "OK" on success; exit 1 listing failures otherwise.
"""
import test_import_stubs
test_import_stubs.install()

import server as s

MODES = ["aggressive", "balanced", "stable"]
fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")


# pending_cap: aggressive / balanced / everything-else -> base
check("pending_cap.agg", s._lat_pending_cap("aggressive"), s.AGG_PENDING_CAP)
check("pending_cap.bal", s._lat_pending_cap("balanced"), s.BAL_PENDING_CAP)
check("pending_cap.stable", s._lat_pending_cap("stable"), s.PENDING_CAP)
check("pending_cap.default", s._lat_pending_cap("???"), s.PENDING_CAP)

# pending_max_age_ms
check("pending_age.agg", s._lat_pending_max_age_ms("aggressive"), s.AGG_PENDING_MAX_AGE_MS)
check("pending_age.bal", s._lat_pending_max_age_ms("balanced"), s.BAL_PENDING_MAX_AGE_MS)
check("pending_age.stable", s._lat_pending_max_age_ms("stable"), s.PENDING_MAX_AGE_MS)

# preview_debounce_ms
check("debounce.agg", s._lat_preview_debounce_ms("aggressive"), s.AGG_PREVIEW_DEBOUNCE_MS)
check("debounce.bal", s._lat_preview_debounce_ms("balanced"), s.BAL_PREVIEW_DEBOUNCE_MS)
check("debounce.stable", s._lat_preview_debounce_ms("stable"), s.PREVIEW_DEBOUNCE_MS)

# profile object: one source for preview gating + final stream cadence
profile = s._lat_profile("aggressive")
check("profile.mode", profile.mode, "aggressive")
check("profile.preview_min_chars", profile.preview_min_chars, s.SPEC_PREVIEW_MIN_CHARS)
check("profile.preview_cooldown", profile.preview_cooldown_ms, s.SPEC_PREVIEW_COOLDOWN_MS)
check("profile.stream.final", profile.stream_every(True), s.TX_FINAL_STREAM_EVERY)
check("profile.stream.preview", profile.stream_every(False), 4)

# engine taxonomy SSOT: Parakeet is the CPU/sherpa family; granite/qwen3 are the mlx-audio family (not sherpa).
check("sherpa.parakeet", s._is_sherpa_engine("parakeet"), True)
check("sherpa.granite", s._is_sherpa_engine("granite"), False)
check("mlxa.granite", s._is_mlxa_engine("granite"), True)
check("mlxa.qwen3", s._is_mlxa_engine("qwen3"), True)
check("normalize.unknown", s._normalize_asr_engine("nemotron"), "granite")   # dropped engine -> granite fallback

# soft_max_sec: the sherpa engine (parakeet) honors mode; any non-sherpa engine falls back to SOFT_MAX_SEC
check("soft.parakeet.agg", s._lat_soft_max_sec("parakeet", "aggressive"), s.AGG_SOFT_MAX_SEC)
check("soft.parakeet.bal", s._lat_soft_max_sec("parakeet", "balanced"), s.BAL_SOFT_MAX_SEC)
check("soft.parakeet.stable", s._lat_soft_max_sec("parakeet", "stable"), s.SOFT_MAX_SEC)
for m in MODES:
    check(f"soft.granite.{m}", s._lat_soft_max_sec("granite", m), s.SOFT_MAX_SEC)

# effective_sent_silence_ms: agg/bal clamp to their cap, stable passes through; int() coercion
agg_cap, bal_cap = s.AGG_SENT_SILENCE_MS, s.BAL_SENT_SILENCE_MS
check("ess.agg.atcap", s._lat_effective_sent_silence_ms("aggressive", agg_cap + 500), agg_cap)
check("ess.agg.below", s._lat_effective_sent_silence_ms("aggressive", agg_cap - 50), agg_cap - 50)
check("ess.bal.atcap", s._lat_effective_sent_silence_ms("balanced", bal_cap + 500), bal_cap)
check("ess.stable.pass", s._lat_effective_sent_silence_ms("stable", 99999), 99999)
check("ess.int_coerce", s._lat_effective_sent_silence_ms("stable", 1234.7), 1234)

# sent_windows_for: always >= 1 and non-decreasing in raw_ms (stable = no cap so the window keeps growing)
check("sw.floor", s._lat_sent_windows_for("stable", 0), max(1, (0 - s.SEG_SILENCE_MS) // s.WINDOW_MS))
prev, mono_ok = 0, True
for raw in range(0, 6000, 200):
    w = s._lat_sent_windows_for("stable", raw)
    if w < 1 or w < prev:
        mono_ok = False
    prev = w
if not mono_ok:
    fails.append("sw.monotonic: expected >=1 and non-decreasing in stable mode")

# translation token caps
check("txmax.final", s._lat_tx_max_tokens_for(True), s._TX_GEN_MAX)
check("txmax.preview", s._lat_tx_max_tokens_for(False), min(s._TX_GEN_MAX, s.TX_PREVIEW_MAX_TOKENS))

# stream cadence: only (final AND aggressive) uses the tight cadence; everything else is 4
check("stream.final.agg", s._lat_tx_stream_every_for(True, "aggressive"), s.TX_FINAL_STREAM_EVERY)
check("stream.final.bal", s._lat_tx_stream_every_for(True, "balanced"), 4)
check("stream.preview.agg", s._lat_tx_stream_every_for(False, "aggressive"), 4)
check("stream.preview.stable", s._lat_tx_stream_every_for(False, "stable"), 4)

if fails:
    print(f"FAIL ({len(fails)} case(s)):")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_latency_profile: OK (all latency-profile cases pass)")
