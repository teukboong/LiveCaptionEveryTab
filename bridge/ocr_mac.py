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
