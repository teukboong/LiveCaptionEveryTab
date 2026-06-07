"""Eyeball A/B: accuracy mode's 2-pass re-transcription vs the per-chunk stitched 1-pass.

The bridge transcribes each VAD chunk independently and stitches the text with word-overlap dedup
(_append_text_dedupe). When a word straddles a chunk boundary, both halves can be mistranscribed and
dedup can't recover. Accuracy mode instead re-transcribes the whole sentence's audio in one pass.

This bench forces the failure case: it cuts a clean clip into N equal chunks (boundaries land
mid-word), stitches them the way the bridge does, then compares against one whole-clip pass.
Reuses server.transcribe_pcm / _append_text_dedupe (SSOT). Loads ONLY the ASR model.

Run with the bridge STOPPED (feedback_mlx_port_safety). Eyeball, not a metric.
"""
import os, sys, wave
import server

CLIPS = sys.argv[1:] or ["/tmp/en.wav", "/tmp/ml_ja.wav"]
N_CHUNKS = 3


def read_pcm16_mono16k(path):
    with wave.open(path, "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            print(f"  (skip {path}: need 16k mono s16, got "
                  f"{w.getframerate()}Hz {w.getnchannels()}ch {8*w.getsampwidth()}bit)")
            return None
        return w.readframes(w.getnframes())


def main():
    print("[bench] loading ASR only (no 26B/VAD)…", flush=True)
    server.load_models(lm=False, vad=False)
    server.transcribe_pcm((b"\x00\x00") * 16000)      # warm the audio graph (~1s silence)

    for path in CLIPS:
        if not os.path.exists(path):
            print(f"\n=== {path} (missing) ==="); continue
        pcm = read_pcm16_mono16k(path)
        if pcm is None:
            continue
        print("\n" + "=" * 88 + f"\n=== {os.path.basename(path)} ===")

        whole = server.transcribe_pcm(pcm) or "(no speech)"

        step = (len(pcm) // 2 // N_CHUNKS) * 2          # byte offset aligned to int16 samples
        stitched = ""
        chunks = []
        for i in range(N_CHUNKS):
            lo = i * step
            hi = len(pcm) if i == N_CHUNKS - 1 else (i + 1) * step
            piece = server.transcribe_pcm(pcm[lo:hi])
            chunks.append(piece or "·")
            if piece:
                stitched = server._append_text_dedupe(stitched, piece)

        print(f"  chunks    : {' | '.join(chunks)}")
        print(f"  1-pass    : {stitched}")
        print(f"  2-pass    : {whole}")
        print(f"  {'(identical)' if stitched.strip() == whole.strip() else '(DIFFER — eyeball which reads cleaner)'}")


if __name__ == "__main__":
    main()
