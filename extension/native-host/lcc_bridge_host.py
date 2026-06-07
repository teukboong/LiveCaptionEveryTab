#!/usr/bin/env python3
"""Native messaging host for the Live Caption extension — lets the popup start/stop/status the
local bridge (server.py) without a terminal.

Protocol (Chrome native messaging): each message is a 4-byte little-endian uint32 length followed
by that many bytes of UTF-8 JSON, on stdin/stdout. The extension sends one {cmd} message and reads
one reply (chrome.runtime.sendNativeMessage), so this host handles a single message and exits.

Commands:
  {"cmd":"status"}  -> {ok, running, pid}
  {"cmd":"start"}   -> launches the bridge DETACHED if the port is free; never double-launches
                       (a second 26B load would blow RAM ~52GB). Returns immediately; the bridge
                       takes ~40s to load models before the port opens, so the popup polls status.
  {"cmd":"stop"}    -> SIGTERM then SIGKILL the process holding the port
  {"cmd":"restart"} -> stop (if running) then start
"""
import sys, os, json, struct, subprocess, time, signal, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_BRIDGE = os.path.normpath(os.path.join(HERE, "..", "..", "bridge", "run_bridge.sh"))
CUDA_STACK_CMD = os.environ.get("LCC_CUDA_STACK_CMD", "").strip()
PORT = 8765
LOG = os.path.join(os.path.expanduser("~"), ".lcc-bridge.log")
HOST_LOG = os.path.join(os.path.expanduser("~"), ".lcc-bridge-host.log")


def hlog(msg):
    try:
        with open(HOST_LOG, "a") as f:
            f.write(f"{int(time.time())} {msg}\n")
    except Exception:
        pass


def read_message():
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        return None
    (n,) = struct.unpack("<I", raw_len)
    data = sys.stdin.buffer.read(n)
    return json.loads(data.decode("utf-8"))


def send_message(obj):
    data = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _lsof():
    return shutil.which("lsof") or ("/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else None)


def port_listener_pids():
    """PIDs listening on PORT. Uses lsof instead of a raw TCP probe so status checks don't create
    invalid WebSocket handshake tracebacks in the bridge log."""
    lsof = _lsof()
    if not lsof:
        return []
    try:
        out = subprocess.run([lsof, "-ti", f"tcp:{PORT}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=4).stdout
        return sorted(int(p) for p in out.split() if p.strip().isdigit())
    except Exception as e:
        hlog(f"lsof err: {e}")
        return []


def port_open():
    return bool(port_listener_pids())


def bridge_pids():
    """PIDs listening on PORT (preferred) or any server.py (fallback). Best-effort, no hard dep."""
    pids = set(port_listener_pids())
    if not pids:
        pgrep = shutil.which("pgrep") or "/usr/bin/pgrep"
        try:
            out = subprocess.run([pgrep, "-f", "bridge/server.py"],
                                 capture_output=True, text=True, timeout=4).stdout
            pids.update(int(p) for p in out.split() if p.strip().isdigit())
        except Exception as e:
            hlog(f"pgrep err: {e}")
    return sorted(pids)


def do_status():
    pids = bridge_pids()
    return {"ok": True, "running": port_open() or bool(pids), "pid": (pids[0] if pids else None)}


def _start_env(msg):
    env = dict(os.environ)
    env.setdefault("HOME", os.path.expanduser("~"))
    # Chrome gives the host a minimal PATH; make sure the venv-resolver + common bins are reachable.
    env["PATH"] = ":".join([env.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]).strip(":")
    asr = str((msg or {}).get("asrEngine") or "").strip().lower()
    if asr in ("granite", "qwen3", "parakeet"):
        env["LCC_ASR_ENGINE"] = asr
    if asr == "parakeet":
        env.setdefault(
            "LCC_PARAKEET_MODEL_DIR",
            os.path.join(os.path.expanduser("~"), ".local/share/models/live-caption/parakeet-tdt-0.6b-v2-int8"),
        )
        env.setdefault("LCC_PARAKEET_THREADS", "4")
        env.setdefault("LCC_PARAKEET_PROVIDER", "cpu")
    return env


def _asr_engine(msg):
    asr = str((msg or {}).get("asrEngine") or os.environ.get("LCC_ASR_ENGINE") or "granite").strip().lower()
    return asr if asr in ("granite", "qwen3", "parakeet") else "granite"


def _cuda_stack(args, timeout=180):
    if not CUDA_STACK_CMD:
        return None
    try:
        out = subprocess.run(
            [CUDA_STACK_CMD, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_start_env({"asrEngine": args[1] if len(args) > 1 else os.environ.get("LCC_ASR_ENGINE", "granite")}),
        )
    except Exception as e:
        hlog(f"cuda stack err: {e}")
        return {"ok": False, "error": f"CUDA stack 실행 실패: {e}"}
    if out.returncode != 0:
        hlog(f"cuda stack rc={out.returncode} stdout={out.stdout[-500:]} stderr={out.stderr[-1000:]}")
        return {"ok": False, "error": (out.stderr.strip() or out.stdout.strip() or f"CUDA stack 실패 rc={out.returncode}")[-1200:]}
    body = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else "{}"
    try:
        data = json.loads(body)
    except Exception:
        data = {"ok": True, "raw": out.stdout.strip()}
    if out.stderr.strip():
        data.setdefault("log", out.stderr.strip()[-1200:])
    return data


def do_start(msg=None):
    if CUDA_STACK_CMD:
        data = _cuda_stack(["start", _asr_engine(msg)], timeout=240)
        if data is None:
            return {"ok": False, "error": "CUDA stack command unavailable"}
        return {
            "ok": bool(data.get("ok", True)),
            "running": bool(data.get("running") or data.get("bridge")),
            "starting": False,
            "pid": data.get("pid"),
            "msg": "CUDA 스택 기동 완료" if data.get("ok", True) else data.get("error", "CUDA 스택 실패"),
            "detail": data,
        }
    if port_open():
        return {"ok": True, "running": True, "already": True, "msg": "이미 실행 중"}
    # The port isn't open for ~40s while the bridge loads models. If a server.py is already starting,
    # a second launch would load a second 26B model (~52GB RAM) — treat an existing PID as "already
    # starting" and never double-launch.
    pids = bridge_pids()
    if pids:
        return {"ok": True, "running": False, "starting": True, "already": True, "pid": pids[0],
                "msg": "이미 기동 중 — 모델 로드 대기"}
    if not os.path.exists(RUN_BRIDGE):
        return {"ok": False, "error": f"run_bridge.sh 없음: {RUN_BRIDGE}"}
    env = _start_env(msg or {})
    try:
        logf = open(LOG, "ab")
        # start_new_session=True -> own process group, detached: survives this host exiting AND Chrome closing.
        p = subprocess.Popen(["/bin/bash", RUN_BRIDGE], stdin=subprocess.DEVNULL,
                             stdout=logf, stderr=logf, start_new_session=True, env=env)
    except Exception as e:
        hlog(f"spawn err: {e}")
        return {"ok": False, "error": f"기동 실패: {e}"}
    time.sleep(0.6)                              # catch an instant crash (bad venv etc.)
    if p.poll() is not None:
        return {"ok": False, "error": f"브릿지가 즉시 종료됨(코드 {p.returncode}). 로그: {LOG}"}
    return {"ok": True, "running": False, "starting": True, "pid": p.pid,
            "msg": "기동 중 — 모델 로드에 ~40초", "log": LOG}


def do_stop():
    if CUDA_STACK_CMD:
        data = _cuda_stack(["stop", _asr_engine({})], timeout=120)
        if data is None:
            return {"ok": False, "error": "CUDA stack command unavailable"}
        return {
            "ok": bool(data.get("ok", True)),
            "running": bool(data.get("running") or data.get("bridge")),
            "msg": "CUDA 스택 종료됨",
            "detail": data,
        }
    pids = bridge_pids()
    if not pids:
        return {"ok": True, "running": False, "msg": "실행 중 아님"}
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
            except Exception as e:
                hlog(f"kill {sig} {pid}: {e}")
        for _ in range(10):
            if not port_open() and not bridge_pids():
                return {"ok": True, "running": False, "msg": "종료됨"}
            time.sleep(0.5)
    return {"ok": not port_open(), "running": port_open(), "msg": "종료 시도 완료"}


def main():
    try:
        msg = read_message()
    except Exception as e:
        hlog(f"read err: {e}")
        send_message({"ok": False, "error": f"메시지 파싱 실패: {e}"})
        return
    if not msg:
        return
    cmd = (msg.get("cmd") or "status").lower()
    hlog(f"cmd={cmd}")
    try:
        if cmd == "start":
            reply = do_start(msg)
        elif cmd == "stop":
            reply = do_stop()
        elif cmd == "restart":
            do_stop()
            reply = do_start(msg)
        elif cmd == "status" and CUDA_STACK_CMD:
            data = _cuda_stack(["status", _asr_engine(msg)], timeout=20) or {}
            reply = {
                "ok": bool(data.get("ok", True)),
                "running": bool(data.get("running") or data.get("bridge")),
                "pid": data.get("pid"),
                "detail": data,
            }
        else:
            reply = do_status()
    except Exception as e:
        hlog(f"handler err: {e}")
        reply = {"ok": False, "error": str(e)}
    send_message(reply)


if __name__ == "__main__":
    main()
