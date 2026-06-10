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

# --- group_lines: line fragments -> reading blocks ---
L = lambda text, x, y, w, h: {"text": text, "box": [x, y, w, h]}
# two tight lines of one paragraph merge; a far-away caption stays separate
g = o.group_lines([
    L("first line of the tweet", 0.1, 0.10, 0.6, 0.03),
    L("second line right under", 0.1, 0.135, 0.55, 0.03),
    L("footer far below", 0.1, 0.5, 0.3, 0.03),
])
check("group.count", len(g), 2)
check("group.merged_text", g[0]["text"], "first line of the tweet second line right under")
check("group.line_h", g[0]["line_h"], 0.03)
ok2 = abs(g[0]["box"][3] - 0.065) < 1e-6
check("group.union_h", ok2, True)
# side-by-side columns never merge (no x-overlap)
g2 = o.group_lines([
    L("left col", 0.05, 0.1, 0.3, 0.03),
    L("right col", 0.6, 0.1, 0.3, 0.03),
])
check("group.columns", len(g2), 2)
# block char cap splits a runaway merge (150+150 joins under 400; the third would exceed -> new block)
big = [L("x" * 150, 0.1, 0.1 + i * 0.035, 0.5, 0.03) for i in range(3)]
g3 = o.group_lines(big)
check("group.char_cap", len(g3), 2)
check("group.empty", o.group_lines([]), [])

if fails:
    print("test_ocr_geometry: FAIL")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("test_ocr_geometry: OK (vision box origin conversion passes)")
