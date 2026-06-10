#!/usr/bin/env python3
"""macOS-only speaker fixture bench for diarize.py threshold calibration.

Generates / reuses three `say` voices (Alex, Samantha, Fred) x two clips, converts them to
16 kHz mono PCM16 with afconvert, embeds with the active sherpa-onnx speaker model, then prints raw
and session-centered cosine distributions. This is intentionally not part of check.sh: it needs the
real local speaker model and macOS speech tools.
"""

from __future__ import annotations

import itertools
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import wave

import diarize as dz


VOICES = ("Alex", "Samantha", "Fred")
CLIPS = (
    "Today we are testing live captions with a short speaker sample for calibration.",
    "The second sentence gives the clustering code another clean example of the same voice.",
)
OUT_DIR = Path(tempfile.gettempdir()) / "lcc-spk-fixture-v1"


def fail(msg: str) -> None:
    print(f"bench_spk_fixture: FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def require_tool(name: str) -> None:
    if not shutil.which(name):
        fail(f"missing required macOS tool: {name}")


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def make_fixture(voice: str, idx: int, text: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{voice.lower()}-{idx}"
    aiff = OUT_DIR / f"{stem}.aiff"
    wav = OUT_DIR / f"{stem}.wav"
    if wav.exists() and wav.stat().st_size > 10_000:
        return wav
    run(["say", "-v", voice, "-o", str(aiff), text])
    run(["afconvert", str(aiff), str(wav), "-f", "WAVE", "-d", "LEI16@16000", "-c", "1"])
    return wav


def pcm16(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != dz.SR:
            fail(f"unexpected WAV format for {path}: ch={w.getnchannels()} width={w.getsampwidth()} sr={w.getframerate()}")
        return w.readframes(w.getnframes())


def norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    if n <= 0:
        fail("zero embedding")
    return [x / n for x in v]


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def centered(v: list[float], mean: list[float]) -> list[float]:
    return norm([x - m for x, m in zip(v, mean)])


def fmt(x: float) -> str:
    return f"{x:.3f}"


def round2(x: float) -> float:
    return round(x + 1e-12, 2)


def recommend(same_min: float, diff_max: float) -> tuple[float, float, float, float]:
    gap = same_min - diff_max
    if gap <= 0:
        fail(f"centered distributions overlap: diff_max={fmt(diff_max)} same_min={fmt(same_min)}")
    lo = round2(diff_max + gap * 0.35)
    hi = round2(diff_max + gap * 0.70)
    if not (diff_max < lo < hi < same_min):
        fail(f"rounded thresholds lost separation: diff_max={fmt(diff_max)} lo={lo:.2f} hi={hi:.2f} same_min={fmt(same_min)}")
    prev_bonus = round2(max(0.0, min(0.03, lo - diff_max - 0.02)))
    merge_at = hi
    return hi, lo, prev_bonus, merge_at


def print_matrix(title: str, labels: list[str], values: list[list[float]]) -> None:
    print(title)
    print(" " * 14 + " ".join(f"{label:>10}" for label in labels))
    for label, row in zip(labels, values):
        print(f"{label:>12}  " + " ".join(f"{fmt(v):>10}" for v in row))


def main() -> int:
    require_tool("say")
    require_tool("afconvert")
    samples: list[tuple[str, str, Path, list[float]]] = []
    print(f"fixture_dir={OUT_DIR}")
    print(f"model={dz._model_path()}")
    for voice in VOICES:
        for idx, text in enumerate(CLIPS, start=1):
            wav = make_fixture(voice, idx, text)
            emb = dz.embed(pcm16(wav))
            if emb is None:
                fail(f"embed returned None for {wav}")
            v = norm(list(emb))
            samples.append((voice, f"{voice}-{idx}", wav, v))
            print(f"sample {voice}-{idx}: wav={wav} dim={len(v)}")

    dim = len(samples[0][3])
    mean = [sum(sample[3][i] for sample in samples) / len(samples) for i in range(dim)]
    centered_vecs = [centered(sample[3], mean) for sample in samples]
    labels = [sample[1] for sample in samples]
    raw_matrix = [[dot(a[3], b[3]) for b in samples] for a in samples]
    centered_matrix = [[dot(a, b) for b in centered_vecs] for a in centered_vecs]
    print_matrix("raw_cosine_matrix", labels, raw_matrix)
    print_matrix("centered_cosine_matrix", labels, centered_matrix)

    raw_same: list[float] = []
    raw_diff: list[float] = []
    centered_same: list[float] = []
    centered_diff: list[float] = []
    for i, j in itertools.combinations(range(len(samples)), 2):
        same = samples[i][0] == samples[j][0]
        (raw_same if same else raw_diff).append(raw_matrix[i][j])
        (centered_same if same else centered_diff).append(centered_matrix[i][j])

    raw_same_min, raw_diff_max = min(raw_same), max(raw_diff)
    centered_same_min, centered_diff_max = min(centered_same), max(centered_diff)
    hi, lo, prev_bonus, merge_at = recommend(centered_same_min, centered_diff_max)
    print(
        "raw_distribution "
        f"same_min={fmt(raw_same_min)} same_max={fmt(max(raw_same))} "
        f"diff_min={fmt(min(raw_diff))} diff_max={fmt(raw_diff_max)}"
    )
    print(
        "centered_distribution "
        f"same_min={fmt(centered_same_min)} same_max={fmt(max(centered_same))} "
        f"diff_min={fmt(min(centered_diff))} diff_max={fmt(centered_diff_max)} "
        f"gap={fmt(centered_same_min - centered_diff_max)}"
    )
    print(
        "recommended "
        f"hi={hi:.2f} lo={lo:.2f} prev_bonus={prev_bonus:.2f} merge_at={merge_at:.2f} "
        f"margin_lo={fmt(lo - centered_diff_max)} margin_hi={fmt(centered_same_min - hi)} "
        f"margin_prev={fmt(lo - (centered_diff_max + prev_bonus))}"
    )
    if centered_diff_max >= centered_same_min:
        fail("centered different-speaker max is not below same-speaker min")
    if not (centered_diff_max < lo < hi < centered_same_min):
        fail("recommended hi/lo are not between centered different-speaker max and same-speaker min")
    if centered_diff_max + prev_bonus >= lo:
        fail("prev_bonus can still lift a different-speaker score into the label-only band")
    print("bench_spk_fixture: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
