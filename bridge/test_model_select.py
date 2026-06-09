"""Model-free tests for model SELECTION (replaces the old tier tests): the curated registry, memory-fit
auto (_auto_lm_model), _finalize_model_config precedence (explicit LCC_LM_MODEL > auto), the Whisper ASR
family taxonomy, and the custom translation prompt (replace-descriptive / keep-guards / signature).

Run under the bridge venv:
    cd bridge && python test_model_select.py
"""
import server as s

fails = []


def ok(name, cond):
    if not cond:
        fails.append(name)


# --- lazy: import must NOT resolve or probe hardware (INV-1) --------------------------------------------
ok("lazy: LM_MODEL unresolved at import", s.LM_MODEL == "")
ok("lazy: not resolved at import", s._LM_RESOLVED is False)

# --- curated registry present (single source of truth) -------------------------------------------------
lm_ids = [m["id"] for m in s.lm_models("mlx")]
asr_ids = [m["id"] for m in s.asr_models("mlx")]
ok("registry: lm has gemma-26b", "gemma-26b" in lm_ids)
ok("registry: lm has nano e4b/e2b (loadable via mlx_vlm)", "gemma-e4b" in lm_ids and "gemma-e2b" in lm_ids)
ok("registry: asr has whisper", "whisper-large-v3" in asr_ids)
ok("registry: asr has qwen3 variants", "qwen3-1.7b" in asr_ids and "qwen3-0.6b" in asr_ids)
_footprints = [m["footprint_gb"] for m in s.lm_models("mlx")]
ok("registry: lm largest-first", _footprints == sorted(_footprints, reverse=True))

# --- whisper ASR family taxonomy (INV-3) ---------------------------------------------------------------
ok("whisper: in ASR_ENGINES", "whisper" in s._ASR_ENGINES)
ok("whisper: _is_whisper_engine", s._is_whisper_engine("whisper") and not s._is_whisper_engine("granite"))
ok("whisper: normalize keeps whisper", s._normalize_asr_engine("whisper") == "whisper")
ok("whisper: separate family", not s._is_mlxa_engine("whisper") and not s._is_sherpa_engine("whisper"))

# --- _auto_lm_model: largest model that fits free memory (need 26b18/e4b8/e2b6, head4) ------------------
s.BACKEND = "mlx"
_orig_probe = s._free_mem_gb_mlx
try:
    def probe(gb):
        s._free_mem_gb_mlx = lambda: gb
    for gb, want in {30: "gemma-26b", 22: "gemma-26b", 21.9: "gemma-e4b", 12: "gemma-e4b",
                     11.9: "gemma-e2b", 9: "gemma-e2b", 3: "gemma-e2b"}.items():
        probe(gb)
        ok(f"auto avail={gb}->{want}", s._auto_lm_model()["id"] == want)
    probe(None)
    ok("auto unprobable->largest", s._auto_lm_model()["id"] == "gemma-26b")
finally:
    s._free_mem_gb_mlx = _orig_probe

# --- _finalize_model_config: explicit LCC_LM_MODEL > auto; qwen3 default; idempotent --------------------
s._LM_RESOLVED = False
s.LM_MODEL = ""
s.MLXA_REPOS["qwen3"] = ""
s.BACKEND = "mlx"
try:
    s._free_mem_gb_mlx = lambda: 30   # fits the 26B
    s._finalize_model_config()
    ok("finalize auto -> 26b repo", s.LM_MODEL == "mlx-community/gemma-4-26b-a4b-it-4bit")
    ok("finalize qwen3 default 1.7B", s.MLXA_REPOS["qwen3"] == "Qwen/Qwen3-ASR-1.7B")
    s.LM_MODEL = "TOUCHED"
    s._finalize_model_config()   # idempotent: must not re-resolve once done
    ok("finalize idempotent", s.LM_MODEL == "TOUCHED")
finally:
    s._free_mem_gb_mlx = _orig_probe

# explicit LCC_LM_MODEL wins over auto
s._LM_RESOLVED = False
s.LM_MODEL = "my/custom-model"
s.MLXA_REPOS["qwen3"] = ""
s._finalize_model_config()
ok("explicit LM_MODEL (custom repo) preserved", s.LM_MODEL == "my/custom-model")

# a curated registry id resolves to its repo (the popup may send a stable id as LCC_LM_MODEL)
s._LM_RESOLVED = False
s.LM_MODEL = "gemma-e4b"
s.MLXA_REPOS["qwen3"] = ""
s._finalize_model_config()
ok("LCC_LM_MODEL id resolves to repo", s.LM_MODEL == "mlx-community/gemma-4-e4b-it-4bit")

# --- custom translation prompt: replace descriptive, keep guards (INV-9 / INV-10 / INV-11) -------------
base = s._tx_system("Korean", "casual", "", ())
ok("custom empty == byte-identical", s._tx_system("Korean", "casual", "", (), "caption", "") == base)
cust = s._tx_system("Korean", "casual", "", (), "caption", "Translate like a pirate")
ok("custom replaces descriptive", "Translate like a pirate" in cust and "expert live interpreter" not in cust)
ok("custom keeps output guard", "Output ONLY the Korean translation" in cust)
pg = s._page_tx_system("Korean", "", (), "pirate")
ok("page keeps DOM rules + layers custom", "pirate" in pg and "unchanged" in pg)
sig_a = s._translation_context_signature("Korean", "casual", "", ())
sig_b = s._translation_context_signature("Korean", "casual", "", (), "X")
ok("signature includes custom (cache invalidation)", sig_a != sig_b and len(sig_b) == 5)

if fails:
    print(f"FAIL ({len(fails)} case(s)):")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_model_select: OK (registry + auto memory-fit + finalize precedence + whisper taxonomy + custom prompt)")
