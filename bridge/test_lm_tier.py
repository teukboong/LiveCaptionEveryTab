"""Model-free tests for translation-model tiering: size the translator to AVAILABLE memory (idle VRAM),
not total RAM. Covers _normalize_tier, _auto_tier thresholds, and _finalize_model_config precedence
(explicit LCC_LM_MODEL > LCC_LM_TIER > auto), plus the lite-tier ASR shrink and lazy/idempotent resolution.

Run under the bridge venv:
    cd bridge && python test_lm_tier.py
"""
import server as s

fails = []


def ok(name, cond):
    if not cond:
        fails.append(name)


# --- lazy: resolution must NOT happen at import (tests `import server` -> no hardware probe) -------------
ok("lazy: LM_MODEL unresolved at import", s.LM_MODEL == "")
ok("lazy: not resolved at import", s._LM_RESOLVED is False)

# --- _normalize_tier (+ aliases) -----------------------------------------------------------------------
ok("normtier full", s._normalize_tier("FULL") == "full")
ok("normtier mid (whitespace)", s._normalize_tier(" Mid ") == "mid")
ok("normtier alias small->lite", s._normalize_tier("small") == "lite")
ok("normtier alias max->full", s._normalize_tier("max") == "full")
ok("normtier junk->''", s._normalize_tier("garbage") == "")
ok("normtier None->''", s._normalize_tier(None) == "")

# --- _auto_tier: largest tier whose need+headroom fits avail (need full18/mid8/lite5, head4) ------------
s.BACKEND = "mlx"
_orig_probe = s._free_mem_gb_mlx
try:
    def probe(gb):
        s._free_mem_gb_mlx = lambda: gb
    for gb, want in {30: "full", 22: "full", 21.9: "mid", 12: "mid",
                     11.9: "lite", 9: "lite", 3: "lite"}.items():
        probe(gb)
        ok(f"auto_tier avail={gb}->{want}", s._auto_tier() == want)
    probe(None)
    ok("auto_tier unprobable->full", s._auto_tier() == "full")
finally:
    s._free_mem_gb_mlx = _orig_probe

# --- _finalize_model_config: explicit tier resolves model + lite shrinks ASR, idempotent ----------------
s._LM_RESOLVED = False
s.LM_TIER = "lite"
s.LM_MODEL = ""
s.MLXA_REPOS["qwen3"] = ""
s.BACKEND = "mlx"
s._finalize_model_config()
ok("finalize lite model", s.LM_MODEL == s._LM_TIERS["mlx"]["lite"])
ok("finalize lite ASR=0.6B", s.MLXA_REPOS["qwen3"] == "Qwen/Qwen3-ASR-0.6B")
s.LM_MODEL = "TOUCHED"
s._finalize_model_config()  # idempotent: must not re-resolve once done
ok("finalize idempotent", s.LM_MODEL == "TOUCHED")

# --- explicit LCC_LM_MODEL wins over tier --------------------------------------------------------------
s._LM_RESOLVED = False
s.LM_TIER = "full"
s.LM_MODEL = "my/custom-model"
s.MLXA_REPOS["qwen3"] = ""
s._finalize_model_config()
ok("explicit LM_MODEL preserved", s.LM_MODEL == "my/custom-model")

# --- non-lite tiers keep the full-size ASR -------------------------------------------------------------
s._LM_RESOLVED = False
s.LM_TIER = "mid"
s.LM_MODEL = ""
s.MLXA_REPOS["qwen3"] = ""
s._finalize_model_config()
ok("mid tier ASR=1.7B", s.MLXA_REPOS["qwen3"] == "Qwen/Qwen3-ASR-1.7B")

if fails:
    print(f"FAIL ({len(fails)} case(s)):")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_lm_tier: OK (tier normalize + auto threshold + finalize precedence + lite ASR shrink pass)")
