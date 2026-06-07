#!/usr/bin/env python3
"""Download the model set for a tier (full/mid/lite) and wire it as the active translation tier.

Spawned DETACHED by the native-messaging host (the popup's full/mid/lite install buttons). Writes JSON
progress to ~/.lcc-install.json so the popup can poll, and on success sets LCC_LM_TIER in the repo .env so
the next bridge start loads that tier. Single source of truth for tier->models is bridge/server.py
(_LM_TIERS + the ASR repo constants) — this file does not hardcode model ids.

Usage:
    install_models.py <full|mid|lite> [--dry-run]
"""
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))          # .../bridge
ROOT = os.path.dirname(HERE)                                # repo root
STATUS = os.path.join(os.path.expanduser("~"), ".lcc-install.json")
ENV_FILE = os.path.join(ROOT, ".env")
TIERS = ("full", "mid", "lite")


def write_status(**kw):
    kw.setdefault("ts", int(time.time()))
    tmp = STATUS + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(kw, f)
        os.replace(tmp, STATUS)   # atomic so the poller never reads a half-written file
    except Exception:
        pass


def tier_models(tier):
    """[(label, repo_id), ...] for this tier on the MLX backend, resolved from server.py."""
    sys.path.insert(0, HERE)
    import server as s
    lm = s._LM_TIERS["mlx"][tier]
    qwen = s._ASR_QWEN3_LITE if tier == "lite" else s._ASR_QWEN3_FULL
    if tier == "lite":
        # lite stays lean: small multilingual ASR + E2B translator (granite auto-downloads on demand if picked)
        return [("ASR · Qwen3 0.6B", qwen), ("Translate · " + tier, lm)]
    granite = s.MLXA_REPOS["granite"]
    return [("ASR · Granite", granite), ("ASR · Qwen3 1.7B", qwen), ("Translate · " + tier, lm)]


def set_env_tier(tier):
    """Persist LCC_LM_TIER=<tier> in the repo .env (preserving other lines), so run_bridge.sh picks it up."""
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            lines = [ln for ln in f.read().splitlines() if not ln.strip().startswith("LCC_LM_TIER=")]
    lines.append(f"LCC_LM_TIER={tier}")
    with open(ENV_FILE, "w") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")


def main():
    tier = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    dry = "--dry-run" in sys.argv
    if tier not in TIERS:
        write_status(tier=tier, done=True, ok=False, error=f"unknown tier: {tier!r}")
        print(f"unknown tier: {tier!r}", file=sys.stderr)
        return 2
    try:
        models = tier_models(tier)
    except Exception as e:
        write_status(tier=tier, done=True, ok=False, error=f"resolve failed: {e}")
        traceback.print_exc()
        return 1
    total = len(models)
    write_status(tier=tier, done=False, ok=True, total=total, index=0,
                 current=models[0][0], pid=os.getpid())
    if dry:
        set_env_tier(tier)
        write_status(tier=tier, done=True, ok=True, total=total, index=total,
                     current="(dry-run)", models=[r for _, r in models])
        print(json.dumps({"tier": tier, "models": [r for _, r in models], "env": ENV_FILE}))
        return 0
    try:
        from huggingface_hub import snapshot_download
        for i, (label, repo) in enumerate(models):
            write_status(tier=tier, done=False, ok=True, total=total, index=i,
                         current=f"{label}  ({repo})", pid=os.getpid())
            snapshot_download(repo)
        set_env_tier(tier)
    except Exception as e:
        write_status(tier=tier, done=True, ok=False, total=total, error=str(e))
        traceback.print_exc()
        return 1
    write_status(tier=tier, done=True, ok=True, total=total, index=total,
                 current="완료", model=models[-1][1])
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
