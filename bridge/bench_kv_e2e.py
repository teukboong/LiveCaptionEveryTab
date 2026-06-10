"""End-to-end WS smoke: feed a real wav to the live bridge, confirm ASR->translate (KV reuse on the
_mlx_pool thread) actually produces Korean captions. Proves runtime, not just the standalone bench."""
import asyncio, json, wave, sys
import websockets

WAV = sys.argv[1] if len(sys.argv) > 1 else "/tmp/cx_en1.wav"
URL = "ws://127.0.0.1:8765"
TOKEN = "lcc-local-extension-v1"

async def main():
    w = wave.open(WAV); pcm = w.readframes(w.getnframes()); SR = w.getframerate()
    src = parts = caps = 0; last_ko = ""; got_ko = False; errs = []
    async with websockets.connect(URL, max_size=None) as ws:
        await ws.send(json.dumps({"type": "hello", "token": TOKEN}))
        await ws.recv()  # hello ok
        await ws.send(json.dumps({"type": "config", "targetLang": "Korean", "register": "lecture", "vadLevel": 2}))

        async def reader():
            nonlocal src, parts, caps, last_ko, got_ko
            try:
                while True:
                    m = json.loads(await ws.recv())
                    t = m.get("type")
                    if t == "source": src += 1
                    elif t == "caption_partial":
                        parts += 1
                        if m.get("ko"): last_ko = m["ko"]; got_ko = True
                    elif t == "caption":
                        caps += 1
                        ko = m.get("ko", "")
                        if ko: last_ko = ko; got_ko = True
                        print(f"  [caption] {m.get('source','')[:50]}  ->  {ko}", flush=True)
                    elif t == "err":
                        errs.append(m.get("text", "")); print("  [err]", m.get("text"), flush=True)
            except Exception:
                pass

        rt = asyncio.create_task(reader())
        chunk = int(SR * 0.32) * 2          # 320ms PCM16 frames
        for i in range(0, len(pcm), chunk):
            await ws.send(pcm[i:i + chunk])
            await asyncio.sleep(0.02)        # gentle pacing
        await ws.send(json.dumps({"type": "eos"}))
        try:
            await asyncio.wait_for(rt, timeout=18)
        except asyncio.TimeoutError:
            rt.cancel()

    print(f"\nsource={src} caption_partial={parts} caption={caps} errs={len(errs)}")
    print(f"last_ko: {last_ko}")
    ok = got_ko and not errs
    print(f"\nPASS (got Korean + no err): {ok}")
    return ok

sys.exit(0 if asyncio.run(main()) else 1)
