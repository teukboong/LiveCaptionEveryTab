"""Tests for the diarize-lite pure clustering (OnlineSpeakerClusters). The sherpa-onnx extractor
needs a model file and stays out of this model-free gate.

    cd bridge && python test_diarize.py
"""
import diarize as dz

fails = []


def ok(name, cond):
    if not cond:
        fails.append(f"{name}: condition failed")


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")


A = [1.0, 0.0, 0.0, 0.0]
B = [0.0, 1.0, 0.0, 0.0]
A_NEAR = [0.95, 0.05, 0.0, 0.0]          # cos ~0.998 with A
C = [0.0, 0.0, 1.0, 0.0]

c = dz.OnlineSpeakerClusters(threshold=0.55, max_speakers=3, ema=0.1)
check("first_speaker", c.add(A), 1)
check("same_again", c.add(A), 1)
check("near_same", c.add(A_NEAR), 1)
check("second_speaker", c.add(B), 2)
check("first_back", c.add(A), 1)         # returning speaker keeps the label
check("third_speaker", c.add(C), 3)

# at capacity: a brand-new voice maps to the CLOSEST existing label, no new cluster
D = [0.0, 0.7, 0.7, 0.0]                 # between B and C
lbl = c.add(D)
ok("capacity_closest", lbl in (2, 3))
check("capacity_size", len(c.centroids), 3)

# magnitude doesn't matter (unit-norm), garbage does
check("scaled", c.add([10.0, 0.0, 0.0, 0.0]), 1)
ok("zero_vec", c.add([0.0, 0.0, 0.0, 0.0]) is None)
ok("empty", c.add([]) is None)
ok("none", c.add(None) is None)

# EMA drift: repeated near-A samples keep label 1 and pull the centroid, but never flip to others
c2 = dz.OnlineSpeakerClusters(threshold=0.55, max_speakers=2, ema=0.2)
c2.add(A)
c2.add(B)
for _ in range(20):
    check("ema_stable", c2.add(A_NEAR), 1)

if fails:
    print("test_diarize: FAIL")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_diarize: OK (online speaker clustering passes)")
