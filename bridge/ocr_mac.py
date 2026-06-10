"""Image OCR via Apple Vision (macOS) — the third modality after tab audio and page DOM.

The extension captures the rendered pixels of a hovered image (captureVisibleTab crop — works on any
image the user can SEE, CORS/auth notwithstanding) and ships a small JPEG here; Vision's
VNRecognizeTextRequest runs on the Apple Neural Engine (no model download, no GPU contention with the
translators), and the recognized lines go through the page-translation path. macOS only by design —
on other platforms recognize() raises and the bridge replies with a clear error.

Pure-ish: import stays stdlib-only (pyobjc loads lazily inside recognize) so model-free tests can
import the geometry helper. Install: pip install '.[ocr]' (pyobjc-framework-Vision/-Quartz).
"""
OCR_MAX_LINES = 60
OCR_BLOCK_MAX_CHARS = 400        # split a runaway merged block (keeps each translation call clean)
OCR_BLOCK_GAP_RATIO = 0.8        # join lines when the vertical gap < this x the taller line's height


def _x_overlap(a, b):
    """Horizontal overlap (normalized units) between two [x, y, w, h] boxes; <= 0 means disjoint."""
    return min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])


def group_lines(lines):
    """Merge Vision's LINE observations into reading blocks. A tweet screenshot yields dozens of tiny
    lines ('@handle · 4h', each body line separately); translating those one marker per line both
    overflows the marker batch format and loses sentence context. Lines (already sorted top-to-bottom)
    join the block whose bottom edge is vertically close (gap < OCR_BLOCK_GAP_RATIO x line height) and
    horizontally overlapping — multi-column layouts stay separate via the x-overlap test. Each block:
    {"text", "box" (union), "line_h" (tallest member line, for overlay font sizing)}. Pure; tested in
    test_ocr_geometry.py."""
    blocks = []
    for ln in lines or []:
        x, y, w, h = ln["box"]
        best = None
        best_overlap = 0.0
        for b in blocks:
            bx, by, bw, bh = b["box"]
            gap = y - (by + bh)
            if gap > OCR_BLOCK_GAP_RATIO * max(h, b["line_h"]) or gap < -0.6 * max(h, b["line_h"]):
                continue
            ov = _x_overlap((x, y, w, h), b["box"])
            if ov > 0 and ov > best_overlap and len(b["text"]) + len(ln["text"]) + 1 <= OCR_BLOCK_MAX_CHARS:
                best, best_overlap = b, ov
        if best is None:
            blocks.append({"text": ln["text"], "box": [x, y, w, h], "line_h": h})
            continue
        bx, by, bw, bh = best["box"]
        nx, ny = min(bx, x), min(by, y)
        best["box"] = [round(nx, 4), round(ny, 4),
                       round(max(bx + bw, x + w) - nx, 4), round(max(by + bh, y + h) - ny, 4)]
        best["text"] += " " + ln["text"]
        best["line_h"] = max(best["line_h"], h)
    return blocks


def vision_box_to_top_left(box):
    """Vision bounding boxes are normalized with a BOTTOM-left origin; the overlay wants top-left.
    box = (x, y, w, h) -> (x, y_top, w, h), everything stays normalized 0..1."""
    x, y, w, h = box
    return (x, max(0.0, 1.0 - y - h), w, h)


def recognize(image_bytes: bytes):
    """OCR one image (PNG/JPEG bytes). Returns [{"text": str, "box": [x, y, w, h]}] with normalized
    top-left boxes, reading order roughly top-to-bottom. Raises on non-macOS / missing pyobjc."""
    try:
        import Vision
        from Foundation import NSData
    except Exception as e:                            # pragma: no cover - platform dependent
        raise RuntimeError("이미지 OCR은 macOS + pyobjc가 필요해요 (pip install '.[ocr]')") from e
    data = NSData.dataWithBytes_length_(image_bytes, len(image_bytes))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    try:                                              # macOS 13+: let Vision pick the script per image
        request.setAutomaticallyDetectsLanguage_(True)
    except Exception:
        pass
    ok = handler.performRequests_error_([request], None)
    if isinstance(ok, tuple):                         # pyobjc returns (bool, error) for in/out error args
        ok = ok[0]
    if not ok:
        raise RuntimeError("Vision OCR 요청 실패")
    out = []
    for obs in (request.results() or []):
        cands = obs.topCandidates_(1)
        if not cands or not len(cands):
            continue
        text = str(cands[0].string()).strip()
        if not text:
            continue
        bb = obs.boundingBox()
        box = vision_box_to_top_left((float(bb.origin.x), float(bb.origin.y),
                                      float(bb.size.width), float(bb.size.height)))
        out.append({"text": text, "box": [round(v, 4) for v in box]})
        if len(out) >= OCR_MAX_LINES:
            break
    out.sort(key=lambda b: (b["box"][1], b["box"][0]))   # reading order: top-to-bottom, then left-to-right
    return out
