"""Tests for the OCR geometry helper (ocr_mac.vision_box_to_top_left). The Vision extractor itself is
macOS + pyobjc and stays out of the model-free gate.

    cd bridge && python test_ocr_geometry.py
"""
import ocr_mac as o

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")


# Vision: normalized, BOTTOM-left origin -> overlay: top-left origin
check("origin_flip", o.vision_box_to_top_left((0.0, 0.0, 1.0, 1.0)), (0.0, 0.0, 1.0, 1.0))
check("bottom_strip", o.vision_box_to_top_left((0.0, 0.0, 1.0, 0.1)), (0.0, 0.9, 1.0, 0.1))
check("top_strip", o.vision_box_to_top_left((0.0, 0.9, 1.0, 0.1)), (0.0, 0.0, 1.0, 0.1))
check("mid_box", o.vision_box_to_top_left((0.25, 0.25, 0.5, 0.5)), (0.25, 0.25, 0.5, 0.5))
got = o.vision_box_to_top_left((0.1, 0.2, 0.3, 0.4))
check("general", (got[0], round(got[1], 6), got[2], got[3]), (0.1, 0.4, 0.3, 0.4))
# slightly-out-of-range Vision output clamps at 0 instead of going negative
check("clamp", o.vision_box_to_top_left((0.0, 0.95, 0.2, 0.1)), (0.0, 0.0, 0.2, 0.1))

if fails:
    print("test_ocr_geometry: FAIL")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_ocr_geometry: OK (vision box origin conversion passes)")
