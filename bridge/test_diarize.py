"""Tests for the diarize-lite pure clustering (OnlineSpeakerClusters): hysteresis, turn-continuity
bonus, two-strike new speakers, and periodic merge. The sherpa-onnx extractor needs a model file and
stays out of this model-free gate.

    cd bridge && python test_diarize.py
"""
import math

import diarize as dz

fails = []


def ok(name, cond):
    if not cond:
        fails.append(f"{name}: condition failed")


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")


def vec(cos):
    """2-D unit vector with the given cosine to A=[1,0]."""
    return [cos, math.sqrt(max(0.0, 1.0 - cos * cos))]


def unit(xs):
    s = math.sqrt(sum(x * x for x in xs))
    return [x / s for x in xs]


def mk(**kw):
    base = dict(hi=0.55, lo=0.35, max_speakers=3, prev_bonus=0.0, topk=10, merge_every=1000, merge_at=0.95)
    base.update(kw)
    return dz.OnlineSpeakerClusters(**base)


A = [1.0, 0.0]
B = [0.0, 1.0]

# --- basics: first speaker immediate, same voice rejoins ---
c = mk()
check("first", c.add(A), 1)
check("scaled", c.add([10.0, 0.0]), 1)        # magnitude irrelevant (unit-norm)
check("rejoin", c.add(vec(0.95)), 1)
ok("zero", c.add([0.0, 0.0]) is None)
ok("empty", c.add([]) is None)
ok("none", c.add(None) is None)

# --- centered cosine: large shared components no longer collapse distinct speakers ---
# These synthetic embeddings model D2/D3: raw cosine is ~0.99 across two distinct voices because all
# vectors carry a large common component. The escape path (center=False / LCC_SPK_CENTER=0) keeps the
# previous raw-cosine behavior; centered scoring separates the voices with the same hi/lo semantics.
A1 = unit([10.0, 1.0, 0.0])
A2 = unit([10.0, 1.2, 0.05])
B1 = unit([10.0, 0.0, 1.0])
B2 = unit([10.0, 0.05, 1.2])
c = mk(center=False)
check("raw_common_first", c.add(A1), 1)
check("raw_common_same", c.add(A2), 1)
check("raw_common_swallows_other", c.add(B1), 1)
check("raw_common_count", len(c.speakers), 1)
c = mk(center=True)
check("center_common_first", c.add(A1), 1)
check("center_common_same", c.add(A2), 1)
ok("center_common_other_pending", c.add(B1) is None)
check("center_common_other_confirmed", c.add(B2), 2)
check("center_common_count", len(c.speakers), 2)

# --- cold-start / epsilon guard: before enough history, or when x-mu degenerates, raw cosine is used ---
c = mk(center=True)
check("cold_start_first", c.add(A), 1)
check("cold_start_second_uses_raw", c.add(vec(0.45)), 1)  # lo <= raw < hi: labeled, no centroid pollution
c = mk(center=True)
check("epsilon_first", c.add(A), 1)
check("epsilon_second_identical", c.add([10.0, 0.0]), 1)
check("epsilon_third_identical", c.add([1.0, 0.0]), 1)

# --- hysteresis: between lo and hi -> labeled WITHOUT centroid update ---
c = mk()
c.add(A)
before = list(c.speakers[1]["centroid"])
check("assign_band", c.add(vec(0.45)), 1)     # lo(0.35) <= 0.45 < hi(0.55)
check("centroid_clean", c.speakers[1]["centroid"], before)
check("join_band_updates", c.add(vec(0.9)), 1)
ok("centroid_moved", c.speakers[1]["centroid"] != before)

# --- two-strike new speaker: one unknown clip never opens a speaker ---
c = mk()
c.add(A)
ok("strike_one", c.add(B) is None)            # cos 0 < lo -> pending, untagged
check("strike_two", c.add([0.05, 0.999]), 2)  # similar unknown again -> speaker 2 opens
check("speaker_count", len(c.speakers), 2)
# a lone outlier between two A clips never spawns a cluster
c = mk()
c.add(A)
ok("outlier_pending", c.add(B) is None)
check("back_to_A", c.add(vec(0.95)), 1)       # A again -> pending cleared by the join
ok("outlier_gone", c.pending is None)
check("still_two_total", len(c.speakers), 1)

# --- turn continuity: the previous speaker gets a bonus across the hi threshold ---
c = mk(prev_bonus=0.07)
c.add(A)
before = list(c.speakers[1]["centroid"])
check("bonus_join", c.add(vec(0.50)), 1)      # 0.50 + 0.07 >= hi -> joins AND updates
ok("bonus_updated_centroid", c.speakers[1]["centroid"] != before)

# --- capacity: at max speakers an unknown voice stays untagged (no spawn, no theft) ---
c = mk(max_speakers=2)
c.add(A)
c.add(B)                                       # pending
c.add([0.05, 0.999])                           # speaker 2
mid = [math.sqrt(0.5) * -1.0, math.sqrt(0.5) * -1.0]   # far from both
ok("capacity_strike_one", c.add(mid) is None)
ok("capacity_strike_two", c.add(mid) is None)  # confirmable, but capacity blocks creation
check("capacity_count", len(c.speakers), 2)

# --- merge: converged clusters collapse into the earlier label ---
c = mk(merge_at=0.9)
c.add(A)
c.add(B)
c.add([0.05, 0.999])                           # speaker 2 ~ B
c.speakers[2]["embs"] = [vec(0.99)]            # drift speaker 2 onto A's voice
c.speakers[2]["centroid"] = c._centroid(c.speakers[2]["embs"])
c.last_label = 2
c.merge_overlapping()
check("merged_count", len(c.speakers), 1)
ok("merged_into_earlier", 1 in c.speakers and 2 not in c.speakers)
check("last_label_remap", c.last_label, 1)
check("future_uses_merged", c.add(vec(0.95)), 1)

# --- label stability: merging never renumbers the surviving labels ---
c = mk()
c.add(A)
c.add(B); c.add([0.05, 0.999])                 # speaker 2
ok("third_pending", c.add([-1.0, 0.0]) is None)
check("third_label", c.add([-0.999, 0.05]), 3)
check("labels_stable", sorted(c.speakers), [1, 2, 3])

# --- server fallback-audio preservation predicate (F-07) ---
def should_preserve_spk_pcm(diarize_enabled, diarize_loading, audio_len, current_len):
    return (diarize_enabled or diarize_loading) and audio_len > current_len


ok("preserve_when_enabled", should_preserve_spk_pcm(True, False, 200, 100))
ok("preserve_when_loading", should_preserve_spk_pcm(False, True, 200, 100))
ok("skip_when_disabled_and_not_loading", not should_preserve_spk_pcm(False, False, 200, 100))
ok("skip_when_not_longer", not should_preserve_spk_pcm(True, False, 100, 200))

if fails:
    print("test_diarize: FAIL")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_diarize: OK (hysteresis + continuity + two-strike + merge pass)")
