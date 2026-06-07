"""Stream a wav with a config targetLang/contextHint to verify target-language translation."""
import asyncio, json, os, sys, wave, websockets

TOKEN = os.environ.get("LCC_WS_TOKEN", "lcc-local-extension-v1")

async def main(path, target, hint):
    w = wave.open(path)
    pcm = w.readframes(w.getnframes()) + b"\x00\x00" * int(1.6 * 16000)
    async with websockets.connect("ws://127.0.0.1:8765", max_size=None) as ws:
        await ws.send(json.dumps({"type": "hello", "token": TOKEN}))
        await ws.send(json.dumps({"type": "config", "targetLang": target, "contextHint": hint}))
        async def recv():
            async for m in ws:
                d = json.loads(m)
                if d.get("type") == "caption":
                    print(f"[{target}] {d['source']}\n   => {d['ko']}", flush=True)
        rt = asyncio.create_task(recv())
        step = int(0.1 * 16000) * 2
        for i in range(0, len(pcm), step):
            await ws.send(pcm[i:i + step]); await asyncio.sleep(0.1)
        await ws.send(json.dumps({"type": "eos"})); await asyncio.sleep(10); rt.cancel()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: test_target.py WAV_PATH [TARGET_LANG] [CONTEXT_HINT]", file=sys.stderr)
        raise SystemExit(2)
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "Korean",
                     sys.argv[3] if len(sys.argv) > 3 else ""))
