"""Model-free bridge WebSocket E2E test for ``LCC_BACKEND=fake``.

Run directly, pytest-free:

    python test_e2e_fake.py
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np
import websockets
from websockets.exceptions import ConnectionClosed

import backend_fake


HOST = "127.0.0.1"
TOKEN = "lcc-local-extension-v1"
RECV_TIMEOUT = 10
SR = 16000


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def _silero_stub_source() -> str:
    return """
def load_silero_vad(*_args, **_kwargs):
    raise RuntimeError("silero_vad stub: load_silero_vad must not be called in fake E2E")

class VADIterator:
    def __init__(self, *_args, **_kwargs):
        raise RuntimeError("silero_vad stub: VADIterator must not be instantiated in fake E2E")
""".lstrip()


class BridgeProcess:
    def __init__(self):
        self.port = _free_port()
        self.url = f"ws://{HOST}:{self.port}"
        self._tmp = tempfile.TemporaryDirectory(prefix="lcc-fake-e2e-")
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self._ready: queue.Queue[bool] = queue.Queue(maxsize=1)
        self.proc: subprocess.Popen[str] | None = None

    def start(self):
        stub = Path(self._tmp.name) / "silero_vad.py"
        stub.write_text(_silero_stub_source(), encoding="utf-8")
        env = os.environ.copy()
        env.update({
            "LCC_BACKEND": "fake",
            "LCC_HOST": HOST,
            "LCC_PORT": str(self.port),
            "PYTHONPATH": self._tmp.name + os.pathsep + env.get("PYTHONPATH", ""),
        })
        self.proc = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=Path(__file__).resolve().parent,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert self.proc.stdout is not None
        assert self.proc.stderr is not None
        threading.Thread(target=self._pump, args=(self.proc.stdout, self.stdout, True), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, self.stderr, False), daemon=True).start()
        try:
            self._ready.get(timeout=RECV_TIMEOUT)
        except queue.Empty as exc:
            raise AssertionError("bridge did not become ready\n" + self.dump()) from exc
        if self.proc.poll() is not None:
            raise AssertionError("bridge exited during startup\n" + self.dump())

    def _pump(self, stream, bucket: list[str], watch_ready: bool):
        for line in iter(stream.readline, ""):
            bucket.append(line.rstrip())
            if watch_ready and "[bridge] ready  ws://" in line:
                try:
                    self._ready.put_nowait(True)
                except queue.Full:
                    pass

    def stop(self):
        proc = self.proc
        if proc is None:
            self._tmp.cleanup()
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self._tmp.cleanup()

    def assert_stopped(self):
        assert self.proc is not None
        if self.proc.poll() is None:
            raise AssertionError("bridge process still running after stop")

    def dump(self) -> str:
        return (
            f"port={self.port}\n"
            "STDOUT:\n" + "\n".join(self.stdout[-200:]) + "\n"
            "STDERR:\n" + "\n".join(self.stderr[-200:])
        )


async def _recv_json(ws, timeout=RECV_TIMEOUT):
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    assert isinstance(raw, str), f"expected text frame, got {type(raw).__name__}"
    return json.loads(raw)


async def _hello(ws, token=TOKEN):
    await ws.send(json.dumps({"type": "hello", "token": token}))
    msg = await _recv_json(ws)
    assert msg == {"type": "hello", "ok": True}, msg


async def _expect_close(ws, code: int, reason_part: str):
    try:
        await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
    except ConnectionClosed as exc:
        assert exc.code == code, (exc.code, exc.reason)
        assert reason_part in (exc.reason or ""), exc.reason
        return
    raise AssertionError(f"connection did not close with {code}:{reason_part!r}")


async def _expect_reject(url: str, *, token=TOKEN, origin=None, code=1008, reason=""):
    async with websockets.connect(url, origin=origin, max_size=None) as ws:
        try:
            await ws.send(json.dumps({"type": "hello", "token": token}))
        except ConnectionClosed as exc:
            assert exc.code == code, (exc.code, exc.reason)
            assert reason in (exc.reason or ""), exc.reason
            return
        await _expect_close(ws, code, reason)


def _speech_pcm(speech_ms=768, silence_ms=384) -> bytes:
    speech_n = int(SR * speech_ms / 1000)
    silence_n = int(SR * silence_ms / 1000)
    speech = np.empty(speech_n, dtype=np.int16)
    speech[0::2] = 24000
    speech[1::2] = -24000
    silence = np.zeros(silence_n, dtype=np.int16)
    return b"".join([speech.tobytes(), silence.tobytes()])


def _assert_caption_contract(msg):
    for key in ("unit_id", "rev", "start_ms", "end_ms"):
        assert key in msg, msg
        assert isinstance(msg[key], int), (key, msg)


async def _caption_flow(ws, *, label: str):
    await ws.send(json.dumps({
        "type": "config",
        "targetLang": "Korean",
        "register": "lecture",
        "vadLevel": 2,
        "latencyMode": "aggressive",
    }))
    await ws.send(_speech_pcm())
    await ws.send(json.dumps({"type": "eos"}))
    messages = []
    while True:
        msg = await _recv_json(ws)
        if msg.get("type") in ("source", "caption_partial", "caption"):
            messages.append(msg)
            _assert_caption_contract(msg)
        if msg.get("type") == "caption":
            break
    sources = [m for m in messages if m.get("type") == "source"]
    finals = [m for m in messages if m.get("type") == "caption"]
    assert sources, (label, messages)
    assert finals, (label, messages)
    units = [m["unit_id"] for m in messages if "unit_id" in m]
    assert units == sorted(units), (label, units)
    final = finals[-1]
    expected = backend_fake.fake_translate_text(final["source"], target="Korean", profile="caption")
    assert final["ko"] == expected, (label, final, expected)
    return messages


async def _dom_translate_flow(ws):
    items = [
        {"id": "short", "text": "Short page item."},
        {"id": "marked", "text": "@@1@@\nAlpha handle @user\n@@2@@\nBeta 42"},
    ]
    await ws.send(json.dumps({
        "type": "dom_translate_batch",
        "request_id": "dom-1",
        "items": items,
        "partial": True,
    }))
    results = {}
    partial_seen = False
    while True:
        msg = await _recv_json(ws)
        if msg.get("type") == "dom_translate_partial":
            partial_seen = True
            continue
        if msg.get("type") == "dom_translate_result":
            assert msg["request_id"] == "dom-1", msg
            results[msg["item_id"]] = msg
            continue
        if msg.get("type") == "dom_translate_done":
            assert msg["request_id"] == "dom-1", msg
            assert msg["count"] == len(items), msg
            break
    assert partial_seen, "expected at least one deterministic DOM partial"
    assert set(results) == {it["id"] for it in items}, results
    by_id = {it["id"]: it["text"] for it in items}
    for item_id, msg in results.items():
        expected = backend_fake.fake_translate_text(by_id[item_id], target="Korean", profile="page")
        assert msg["source"] == by_id[item_id], msg
        assert msg["target"] == expected, (msg, expected)


async def _ask_flow(ws):
    transcript = "fake utterance 768ms."
    question = "What happened?"
    await ws.send(json.dumps({
        "type": "ask",
        "mode": "qa",
        "transcript": transcript,
        "question": question,
    }))
    saw_partial = False
    while True:
        msg = await _recv_json(ws)
        if msg.get("type") == "answer_partial":
            saw_partial = True
            continue
        if msg.get("type") == "answer":
            expected = backend_fake.fake_ask_text("qa", transcript, question, "Korean")
            assert msg["text"] == expected, (msg, expected)
            assert saw_partial, "expected deterministic answer_partial"
            return


async def _writeback_flow(ws):
    source = "hello from the input box"
    await ws.send(json.dumps({
        "type": "input_translate",
        "request_id": "wb-1",
        "text": source,
        "target_lang": "English",
    }))
    while True:
        msg = await _recv_json(ws)
        if msg.get("type") != "input_translate_result":
            continue
        expected = backend_fake.fake_translate_text(source, target="English", profile="write")
        assert msg["request_id"] == "wb-1", msg
        assert msg["source"] == source, msg
        assert msg["text"] == expected, (msg, expected)
        return


async def _run_scenarios(url: str):
    async with websockets.connect(url, max_size=None) as ws:
        await _hello(ws)
    await _expect_reject(url, origin="chrome-extension://not-the-live-caption-extension",
                         reason="origin not allowed")
    await _expect_reject(url, token="wrong-token", reason="bad token")

    ws_a = await websockets.connect(url, max_size=None)
    await _hello(ws_a)
    await _caption_flow(ws_a, label="initial")
    await _dom_translate_flow(ws_a)

    ws_b = await websockets.connect(url, max_size=None)
    await _hello(ws_b)
    await _expect_close(ws_a, 1001, "superseded")
    await ws_b.close()

    ws_c = await websockets.connect(url, max_size=None)
    try:
        await _hello(ws_c)
        await _caption_flow(ws_c, label="reconnect")
        await _ask_flow(ws_c)
        await _writeback_flow(ws_c)
    finally:
        await ws_c.close()


def main():
    bridge = BridgeProcess()
    try:
        bridge.start()
        asyncio.run(_run_scenarios(bridge.url))
    except Exception:
        print(bridge.dump(), file=sys.stderr)
        raise
    finally:
        bridge.stop()
        bridge.assert_stopped()
    print(f"test_e2e_fake: OK (port={bridge.port})")


if __name__ == "__main__":
    main()
