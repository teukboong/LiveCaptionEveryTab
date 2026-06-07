"""Simulate the browser extension: stream a 16kHz mono wav to the bridge in real time
and print the captions it returns. Usage: python test_stream_wav.py /tmp/en.wav
"""
import asyncio, json, os, sys, wave
import websockets

URL = "ws://127.0.0.1:8765"
TOKEN = os.environ.get("LCC_WS_TOKEN", "lcc-local-extension-v1")


async def main(path):
    w = wave.open(path)
    assert w.getframerate() == 16000 and w.getsampwidth() == 2 and w.getnchannels() == 1, "need 16k mono pcm16"
    pcm = w.readframes(w.getnframes())
    pcm += b"\x00\x00" * int(1.6 * 16000)          # trailing silence -> clause-boundary flush
    async with websockets.connect(URL, max_size=None) as ws:
        await ws.send(json.dumps({"type": "hello", "token": TOKEN}))
        async def recv():
            async for m in ws:
                d = json.loads(m)
                if d.get("type") == "source":
                    print(f"  [src] {d['text']}", flush=True)
                elif d.get("type") == "caption":
                    print(f"\n[KO] {d['source']}\n  => {d['ko']}", flush=True)
        rt = asyncio.create_task(recv())
        step = int(0.1 * 16000) * 2                  # 100ms PCM16
        for i in range(0, len(pcm), step):
            await ws.send(pcm[i:i + step])
            await asyncio.sleep(0.1)                  # real-time pace
        await ws.send(json.dumps({"type": "eos"}))
        await asyncio.sleep(10)                       # let final transcribe+translate finish
        rt.cancel()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/en.wav"))
