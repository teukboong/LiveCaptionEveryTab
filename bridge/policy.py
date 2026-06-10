import os
import re
from dataclasses import dataclass

from text_helpers import _clean, _weak_tail

SR = 16000
WINDOW_SAMPLES = 512                       # Silero VAD window @16k
WINDOW_BYTES = WINDOW_SAMPLES * 2
WINDOW_MS = 1000 * WINDOW_SAMPLES // SR     # 32ms
SEG_SILENCE_MS = 250       # silence that ends one utterance (VADIterator min_silence) — lower = ASR starts sooner
SENT_SILENCE_MS = 1000     # clause/sentence boundary -> translate. KO reverses word order, so kept higher than
                           #  GPT's suggested 750 — committing mid-clause would break the translation order.
SPEECH_PAD_MS = 120        # VADIterator pre/post-roll
PREROLL_WINDOWS = SPEECH_PAD_MS // WINDOW_MS + 4
SOFT_MAX_SEC, HARD_MAX_SEC, MIN_SEC = 4.0, 12, 0.4
SOFT_OVERLAP_MS = 220
LA_ON = os.environ.get("LCC_LA", "0") == "1"   # LocalAgreement: stream the confirmed-prefix as live source (off by default)
LA_STEP_WINDOWS = max(1, round(float(os.environ.get("LCC_LA_STEP_MS", "1000")) / WINDOW_MS))
TWO_PASS_MIN_SEC, TWO_PASS_MAX_SEC = 1.2, 14.0   # accuracy mode: re-transcribe the whole sentence's audio once at commit
PENDING_CAP = 120          # chars: force-translate even without a sentence end (low-latency profile)
PENDING_MAX_AGE_MS = 3000
PREVIEW_DEBOUNCE_MS = 450
PREVIEW_MIN_CHARS = 18
PREVIEW_MIN_DELTA = 12

AGG_SOFT_MAX_SEC = max(MIN_SEC, float(os.environ.get("LCC_AGG_SOFT_MAX_SEC", "4.0")))
BAL_SOFT_MAX_SEC = max(MIN_SEC, float(os.environ.get("LCC_BAL_SOFT_MAX_SEC", "3.5")))
AGG_SENT_SILENCE_MS = max(SEG_SILENCE_MS + WINDOW_MS, int(os.environ.get("LCC_AGG_SENT_SILENCE_MS", "900")))
BAL_SENT_SILENCE_MS = max(SEG_SILENCE_MS + WINDOW_MS, int(os.environ.get("LCC_BAL_SENT_SILENCE_MS", "1100")))
AGG_PENDING_CAP = max(40, int(os.environ.get("LCC_AGG_PENDING_CAP", "120")))
BAL_PENDING_CAP = max(60, int(os.environ.get("LCC_BAL_PENDING_CAP", "100")))
AGG_PENDING_MAX_AGE_MS = max(800, int(os.environ.get("LCC_AGG_PENDING_MAX_AGE_MS", "1800")))
BAL_PENDING_MAX_AGE_MS = max(1000, int(os.environ.get("LCC_BAL_PENDING_MAX_AGE_MS", "2400")))
AGG_PREVIEW_DEBOUNCE_MS = max(80, int(os.environ.get("LCC_AGG_PREVIEW_DEBOUNCE_MS", "180")))
BAL_PREVIEW_DEBOUNCE_MS = max(120, int(os.environ.get("LCC_BAL_PREVIEW_DEBOUNCE_MS", "300")))
SPEC_PREVIEW_MIN_CHARS = max(PREVIEW_MIN_CHARS, int(os.environ.get("LCC_SPEC_PREVIEW_MIN_CHARS", "42")))
SPEC_PREVIEW_MIN_DELTA = max(8, int(os.environ.get("LCC_SPEC_PREVIEW_MIN_DELTA", "18")))
SPEC_PREVIEW_COOLDOWN_MS = max(250, int(os.environ.get("LCC_SPEC_PREVIEW_COOLDOWN_MS", "900")))
PREVIEW_PROMOTE_SIMILARITY = min(1.0, max(0.0, float(os.environ.get("LCC_PREVIEW_PROMOTE_SIMILARITY", "0.985"))))
TX_FINAL_STREAM_EVERY = max(1, int(os.environ.get("LCC_TX_FINAL_STREAM_EVERY", "2")))
TX_FINAL_STREAM_MIN_CHARS = max(1, int(os.environ.get("LCC_TX_FINAL_STREAM_MIN_CHARS", "8")))
TX_FINAL_STREAM_MIN_WORDS = max(1, int(os.environ.get("LCC_TX_FINAL_STREAM_MIN_WORDS", "2")))
TX_FINAL_STREAM_DELTA_CHARS = max(1, int(os.environ.get("LCC_TX_FINAL_STREAM_DELTA_CHARS", "4")))


# --- Latency-mode tuning profile (pure) ---------------------------------------------------------------
# One immutable profile per mode keeps the live scheduler and tests on the same knob surface.
@dataclass(frozen=True)
class LatencyProfile:
    mode: str
    pending_cap: int
    pending_max_age_ms: int
    preview_debounce_ms: int
    preview_min_chars: int
    preview_min_delta: int
    preview_cooldown_ms: int
    final_stream_every: int

    def pending_cap_for_pressure(self, pressure: int = 0) -> int:
        return max(40, self.pending_cap - EVS_CAP_DROP) if pressure >= 1 else self.pending_cap

    def pending_max_age_for_pressure(self, pressure: int = 0) -> int:
        return max(600, self.pending_max_age_ms - EVS_AGE_DROP) if pressure >= 1 else self.pending_max_age_ms

    def stream_every(self, final: bool) -> int:
        return self.final_stream_every if final else 4


def _lat_profile(mode: str) -> LatencyProfile:
    if mode == "aggressive":
        return LatencyProfile(
            mode="aggressive",
            pending_cap=AGG_PENDING_CAP,
            pending_max_age_ms=AGG_PENDING_MAX_AGE_MS,
            preview_debounce_ms=AGG_PREVIEW_DEBOUNCE_MS,
            preview_min_chars=SPEC_PREVIEW_MIN_CHARS,
            preview_min_delta=SPEC_PREVIEW_MIN_DELTA,
            preview_cooldown_ms=SPEC_PREVIEW_COOLDOWN_MS,
            final_stream_every=TX_FINAL_STREAM_EVERY,
        )
    if mode == "balanced":
        return LatencyProfile(
            mode="balanced",
            pending_cap=BAL_PENDING_CAP,
            pending_max_age_ms=BAL_PENDING_MAX_AGE_MS,
            preview_debounce_ms=BAL_PREVIEW_DEBOUNCE_MS,
            preview_min_chars=PREVIEW_MIN_CHARS,
            preview_min_delta=PREVIEW_MIN_DELTA,
            preview_cooldown_ms=2200,
            final_stream_every=4,
        )
    return LatencyProfile(
        mode="stable",
        pending_cap=PENDING_CAP,
        pending_max_age_ms=PENDING_MAX_AGE_MS,
        preview_debounce_ms=PREVIEW_DEBOUNCE_MS,
        preview_min_chars=PREVIEW_MIN_CHARS,
        preview_min_delta=PREVIEW_MIN_DELTA,
        preview_cooldown_ms=2200,
        final_stream_every=4,
    )


def _lat_tx_stream_every_for(final: bool, mode: str):
    return _lat_profile(mode).stream_every(final)


def _lat_preview_debounce_ms(mode: str):
    return _lat_profile(mode).preview_debounce_ms


# --- EVS (Ear-Voice Span) controller: a load-adaptive latency band ------------------------------------
# Like an interpreter, run a TARGET lag band instead of fixed knobs: under sustained translation backlog
# shift the force-commit thresholds DOWN (commit sooner = shorter span = smaller jobs the client merges to
# catch up), and relax to nominal when idle. Hysteresis (separate enter/exit) stops the level oscillating
# utterance-to-utterance. Default ON (LCC_EVS=0 to disable; A/B with policy_ab.sh). When off,
# _evs_step is a no-op (level 0) so behaviour is byte-identical to the static profile. Tested in
# test_evs_controller.py.
EVS_ON = os.environ.get("LCC_EVS", "1") == "1"
EVS_ENTER_MS = max(1, int(os.environ.get("LCC_EVS_ENTER_MS", "1800")))   # backlog >= this -> pressured
EVS_EXIT_MS = max(0, int(os.environ.get("LCC_EVS_EXIT_MS", "900")))      # backlog <= this -> relax (must be < ENTER)
EVS_CAP_DROP = max(0, int(os.environ.get("LCC_EVS_CAP_DROP", "40")))     # chars shaved off pending_cap when pressured
EVS_AGE_DROP = max(0, int(os.environ.get("LCC_EVS_AGE_DROP", "600")))    # ms shaved off pending_max_age_ms when pressured


def _evs_step(level: int, backlog_ms: int) -> int:
    """Pure hysteresis controller. level 0 = nominal, 1 = pressured. Enter at EVS_ENTER_MS, exit at
    EVS_EXIT_MS (< ENTER) so the level can't flap while backlog hovers near one threshold. No-op when off."""
    if not EVS_ON:
        return 0
    if level <= 0:
        return 1 if backlog_ms >= EVS_ENTER_MS else 0
    return 0 if backlog_ms <= EVS_EXIT_MS else 1


def _lat_pending_cap(mode: str, pressure: int = 0):
    return _lat_profile(mode).pending_cap_for_pressure(pressure)


def _lat_pending_max_age_ms(mode: str, pressure: int = 0):
    return _lat_profile(mode).pending_max_age_for_pressure(pressure)


def _lat_effective_sent_silence_ms(mode: str, raw_ms):
    raw = int(raw_ms)
    if mode == "aggressive":
        return min(raw, AGG_SENT_SILENCE_MS)
    if mode == "balanced":
        return min(raw, BAL_SENT_SILENCE_MS)
    return raw
# ------------------------------------------------------------------------------------------------------


def _commit_decision(text, eos_now, finalize_now, age_ms, pending_cap, pending_max_age_ms):
    """Whether the in-progress remainder should be force-committed now, and why (pure; mirrors the inline
    logic in inference_loop). reason is '' when force is False. A weak tail (conjunction/aux/trailing
    comma) defers a pause/age/cap commit but never an eos. Tested in test_assembler_decisions.py."""
    weak = _weak_tail(text)
    too_long = len(text) > pending_cap and not weak
    aged = (age_ms > pending_max_age_ms) and not weak
    force = eos_now or too_long or aged or (finalize_now and not weak)
    if not force:
        return False, ""
    return True, ("eos" if eos_now else ("cap" if too_long else ("age" if aged else "pause")))


def _two_pass_eligible(accuracy_mode, pure, clauses, pcm_len):
    """Accuracy-mode 2-pass re-transcription pays off only for a multi-clause, well-aligned unit whose
    audio is within [TWO_PASS_MIN_SEC, TWO_PASS_MAX_SEC] (pure). Tested in test_assembler_decisions.py."""
    return (accuracy_mode and pure and clauses >= 2
            and int(TWO_PASS_MIN_SEC * SR) * 2 <= pcm_len <= int(TWO_PASS_MAX_SEC * SR) * 2)


NUMGUARD_ON = os.environ.get("LCC_NUMGUARD", "1") == "1"
_SIG_NUM_RE = re.compile(r"\d[\d.,:]*\d")   # >= 2 chars; single digits are intentionally ignored — they
                                            # often become Korean counters ("2 cats" -> "고양이 두 마리").


def _sig_numbers(text: str):
    return [re.sub(r"\D", "", m) for m in _SIG_NUM_RE.findall(text or "")]


# Sino-Korean (일이삼…) and native-Korean (하나둘셋…) spellings, so a number the translator wrote out in
# words ("26" → "스물여섯" / "이십육") isn't falsely flagged as dropped. Scoped to 0–99 (the common case);
# larger numbers keep the digit-only check (rarely spelled out, and full numerals get expensive).
_SINO_ONES = ["", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구"]
_NATIVE_ONES = ["", "하나", "둘", "셋", "넷", "다섯", "여섯", "일곱", "여덟", "아홉"]
_NATIVE_TENS = ["", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔"]


def _ko_number_forms(digits: str):
    """Korean spelled-out forms (Sino + native) of an integer 0..99; () for anything outside that range."""
    if not digits or not digits.isdigit():
        return ()
    n = int(digits)
    if n > 99:
        return ()
    if n == 0:
        return ("영", "공")
    tens, ones = divmod(n, 10)
    sino = ((_SINO_ONES[tens] if tens > 1 else "") + "십" if tens else "") + _SINO_ONES[ones]
    native = _NATIVE_TENS[tens] + _NATIVE_ONES[ones]
    return tuple(f for f in {sino, native} if f)


def _missing_numbers(source: str, ko: str):
    """Significant source numbers whose value is absent from the translation — as digits OR spelled out in
    Korean (Sino/native), so a meaning-translated number ("at 20 percent" → "이십 퍼센트") isn't flagged."""
    src = _sig_numbers(source)
    if not src:
        return []
    ko = ko or ""
    kod = re.sub(r"\D", "", ko)
    return [n for n in src
            if n and n not in kod and not any(form in ko for form in _ko_number_forms(n))]


def _guard_numbers(source: str, ko: str):
    """Trust guard: if significant source numbers went missing from the translation, append them verbatim so
    the literal value survives (greedy decoding makes a re-translate pointless). Returns
    (display_ko, number_uncertain). No-op when LCC_NUMGUARD=0. Tested in test_number_guard.py."""
    if not NUMGUARD_ON:
        return ko, False
    missing = _missing_numbers(source, ko)
    if not missing:
        return ko, False
    return f"{ko} ({' '.join(missing)})", True


# --- Interpretation policy ----------------------------------------------------------------------------
# The "when to wait / commit / (later) compress / repair" surface, borrowed from simultaneous interpreting.
# The pieces feed in at different pipeline points and stay separate pure functions: _evs_step (load -> the
# latency band), _commit_decision (timing -> commit/wait), _two_pass_eligible (clean-up pass), _preview_is_stale
# (preview validity), _guard_numbers (number trust). decide_commit() unifies the commit-time call and carries a
# risk read so a future compression mode can hold the risky bits longer. See docs/caption-lifecycle.md.
_NEG_RE = re.compile(r"n't\b|\b(?:not|never|no|none|without|cannot|nor|neither)\b", re.I)


def _source_risk(text: str) -> str:
    """'high' when the line carries information costly to get wrong — numbers or negation — which viewers
    distrust most when a caption is off; else 'low'. Pure; tested in test_policy.py."""
    return "high" if (_sig_numbers(text) or _NEG_RE.search(text or "")) else "low"


@dataclass
class InterpretDecision:
    action: str   # "commit" | "wait"  (compress / repair / draft reserved for later phases)
    reason: str   # eos / cap / age / pause / "" — why
    risk: str     # "low" | "high" — information risk of the source line


def decide_commit(text, eos_now, finalize_now, age_ms, pending_cap, pending_max_age_ms) -> InterpretDecision:
    """Commit-time policy: commit now or wait, why, and how risky the content is. The commit/wait choice is
    byte-identical to _commit_decision; this adds the risk annotation. Tested in test_policy.py."""
    force, reason = _commit_decision(text, eos_now, finalize_now, age_ms, pending_cap, pending_max_age_ms)
    return InterpretDecision(action="commit" if force else "wait", reason=reason, risk=_source_risk(text))
# ------------------------------------------------------------------------------------------------------


def _preview_is_stale(job, finalized_units, cur_uid, cur_rev, latest_preview_rev):
    """A preview job is stale once its unit finalized, the active unit moved on, or its rev was superseded
    (pure; mirrors the translation scheduler's check). Final jobs are never stale. Tested in
    test_scheduler_staleness.py."""
    return (
        not job["final"] and
        (
            job["unit_id"] in finalized_units or
            cur_uid != job["unit_id"] or
            cur_rev != job["rev"] or
            latest_preview_rev.get(job["unit_id"]) != job["rev"]
        )
    )


def _stream_visible_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _stream_partial_substantial(text: str) -> bool:
    text = _clean(text)
    if not text or text.endswith(("(", "[", "{", ",", "…", "、", "，", "·")):
        return False
    visible = _stream_visible_chars(text)
    if visible >= TX_FINAL_STREAM_MIN_CHARS or len(text.split()) >= TX_FINAL_STREAM_MIN_WORDS:
        return True
    # Short complete captions such as "네." / "맞습니다." are useful, but early fragments
    # like "오늘은" are not. Terminal punctuation is the cheap completeness signal.
    return visible >= 2 and bool(re.search(r"[.!?。！？]$", text))


def _stream_partial_should_emit(text: str, last: str) -> bool:
    text = _clean(text)
    last = _clean(last)
    if not _stream_partial_substantial(text):
        return False
    if not last:
        return True
    if text == last:
        return False
    if len(text.split()) > len(last.split()):
        return True
    return _stream_visible_chars(text) - _stream_visible_chars(last) >= TX_FINAL_STREAM_DELTA_CHARS
