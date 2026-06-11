#!/usr/bin/env python3
"""Native messaging host for the Live Caption extension — lets the popup start/stop/status the
local bridge (server.py) without a terminal.

Protocol (Chrome native messaging): each message is a 4-byte little-endian uint32 length followed
by that many bytes of UTF-8 JSON, on stdin/stdout. The extension sends one {cmd} message and reads
one reply (chrome.runtime.sendNativeMessage), so this host handles a single message and exits.

Commands:
  {"cmd":"status"}  -> {ok, running, starting, pid}
  {"cmd":"start"}   -> launches the bridge DETACHED if the port is free; never double-launches
                       (a second 26B load would blow RAM ~52GB). Returns immediately; the bridge
                       takes ~40s to load models before the port opens, so the popup polls status.
  {"cmd":"stop"}    -> SIGTERM then SIGKILL the process holding the port
  {"cmd":"restart"} -> stop (if running) then start
"""
import sys, os, json, struct, subprocess, time, signal, shutil, shlex, fcntl

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_BRIDGE = os.path.normpath(os.path.join(HERE, "..", "..", "bridge", "run_bridge.sh"))
CUDA_STACK_CMD = os.environ.get("LCC_CUDA_STACK_CMD", "").strip()
PORT = 8765
LOG = os.path.join(os.path.expanduser("~"), ".lcc-bridge.log")
START_LOCK = os.path.join(os.path.expanduser("~"), ".lcc-bridge-start.lock")   # start mutex (port is global anyway)
START_PID = os.path.join(os.path.expanduser("~"), ".lcc-bridge-start.json")    # last spawned launcher pid + ts
HOST_LOG = os.path.join(os.path.expanduser("~"), ".lcc-bridge-host.log")
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))                     # repo root
INSTALLER = os.path.join(ROOT, "bridge", "install_models.py")
INSTALL_STATUS = os.path.join(os.path.expanduser("~"), ".lcc-install.json")
INSTALL_LOG = os.path.join(os.path.expanduser("~"), ".lcc-install.log")
MAX_MESSAGE_BYTES = 1024 * 1024


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
    if n > MAX_MESSAGE_BYTES:
        raise ValueError(f"native message too large: {n} bytes")
    data = sys.stdin.buffer.read(n)
    if len(data) != n:
        raise ValueError(f"truncated native message: expected {n} bytes, got {len(data)}")
    msg = json.loads(data.decode("utf-8"))
    if not isinstance(msg, dict):
        raise ValueError("native message must be a JSON object")
    return msg


def send_message(obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
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


def _server_py():
    return os.path.realpath(os.path.join(ROOT, "bridge", "server.py"))


def _pid_command(pid):
    try:
        out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                             capture_output=True, text=True, timeout=4).stdout
        return out.strip()
    except Exception as e:
        hlog(f"ps cmd err pid={pid}: {e}")
        return ""


def _cmd_has_path(cmd, path):
    if not cmd:
        return False
    real_path = os.path.realpath(path)
    raw_path = os.path.normpath(path)
    try:
        tokens = shlex.split(cmd)
    except Exception:
        tokens = cmd.split()
    for token in tokens:
        if os.path.realpath(token) == real_path or os.path.normpath(token) == raw_path:
            return True
    return False


def _cmd_matches_this_bridge(cmd):
    if not _cmd_has_path(cmd, os.path.join(ROOT, "bridge", "server.py")):
        return False
    # only an interpreter actually RUNNING server.py counts — `vim .../server.py`, `tail -f`, `less`
    # etc. also carry the path in argv and must never be reported as the bridge (or SIGKILLed by stop)
    try:
        head = os.path.basename(shlex.split(cmd)[0]).lower()
    except Exception:
        head = os.path.basename((cmd.split() or [""])[0]).lower()
    return head.startswith("python") or head in ("bash", "sh", "zsh", "dash")


def _this_bridge_pids(pids):
    return sorted(int(pid) for pid in pids if _cmd_matches_this_bridge(_pid_command(pid)))


def _own_listener_pids():
    return _this_bridge_pids(port_listener_pids())


def _foreign_listener_pids():
    listeners = port_listener_pids()
    ours = set(_this_bridge_pids(listeners))
    return [pid for pid in listeners if pid not in ours]


def bridge_pids():
    """PIDs for this checkout's bridge. Prefer the port listener; fallback catches a still-loading server."""
    pids = set(_this_bridge_pids(port_listener_pids()))
    if not pids:
        try:
            out = subprocess.run(["ps", "-axo", "pid=,command="],
                                 capture_output=True, text=True, timeout=4).stdout
            for line in out.splitlines():
                pid_s, _sep, cmd = line.strip().partition(" ")
                if pid_s.isdigit() and _cmd_matches_this_bridge(cmd):
                    pids.add(int(pid_s))
        except Exception as e:
            hlog(f"ps scan err: {e}")
    return sorted(pids)


def _starting_pid():
    """The launcher pid recorded by the last do_start, while it is still alive and still ours. Covers the
    run_bridge.sh bash preamble (~1s before exec server.py) during which the bridge is invisible to ps —
    the window where a second start used to pass every guard and double-load the 26B model."""
    try:
        with open(START_PID) as f:
            data = json.loads(f.read())
        pid, ts = int(data["pid"]), float(data["ts"])
    except Exception:
        return None
    if time.time() - ts > 180:                  # stale file: don't trust a recycled pid
        return None
    try:
        os.kill(pid, 0)
    except Exception:
        return None
    cmd = _pid_command(pid)
    if _cmd_matches_this_bridge(cmd) or _cmd_has_path(cmd, RUN_BRIDGE):
        return pid
    return None


def do_status():
    listener_pids = _own_listener_pids()
    pids = bridge_pids() or ([p for p in (_starting_pid(),) if p])   # preamble: only the pidfile sees it
    foreign = _foreign_listener_pids()
    reply = {
        "ok": True,
        "running": bool(listener_pids),
        "starting": bool(pids and not listener_pids),
        "pid": ((listener_pids or pids)[0] if (listener_pids or pids) else None),
    }
    if foreign:
        reply.update({"blocked": True, "error": f"포트 {PORT}가 다른 프로세스에 사용 중: pid {foreign[0]}"})
    return reply


def _start_env(msg):
    env = dict(os.environ)
    env.setdefault("HOME", os.path.expanduser("~"))
    # Chrome gives the host a minimal PATH; make sure the venv-resolver + common bins are reachable.
    env["PATH"] = ":".join([env.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]).strip(":")
    asr = str((msg or {}).get("asrEngine") or "").strip().lower()
    if asr in ("granite", "qwen3", "parakeet", "whisper"):
        env["LCC_ASR_ENGINE"] = asr
    if asr == "parakeet":
        env.setdefault(
            "LCC_PARAKEET_MODEL_DIR",
            os.path.join(os.path.expanduser("~"), ".local/share/models/live-caption/parakeet-tdt-0.6b-v2-int8"),
        )
        env.setdefault("LCC_PARAKEET_THREADS", "4")
        env.setdefault("LCC_PARAKEET_PROVIDER", "cpu")
    lm = str((msg or {}).get("lmModel") or "").strip()
    if lm:                               # "" = Auto (server memory-fit); only pin when explicitly chosen (restart-applied)
        env["LCC_LM_MODEL"] = lm
    asr_repo = str((msg or {}).get("asrRepo") or "").strip()
    if asr_repo and asr == "qwen3":      # variant repo (0.6B vs 1.7B) / custom — pins the engine's model
        env["LCC_QWEN3_MODEL"] = asr_repo
    elif asr_repo and asr == "whisper":
        env["LCC_WHISPER_MODEL"] = asr_repo
    return env


def _asr_engine(msg):
    asr = str((msg or {}).get("asrEngine") or os.environ.get("LCC_ASR_ENGINE") or "granite").strip().lower()
    return asr if asr in ("granite", "qwen3", "parakeet", "whisper") else "granite"


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
    # Every check below is check-then-spawn; concurrent hosts (popup double-click, CLI + popup, a second
    # profile) could each pass them before either spawns. Serialize the whole section with a file lock.
    lock = None
    try:
        lock = open(START_LOCK, "a+")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if lock is not None:                     # lock held by a concurrent start; open failure falls through unlocked
            lock.close()
            return {"ok": True, "running": False, "starting": True, "already": True,
                    "msg": "이미 기동 중 — 다른 시작 요청이 진행 중"}
        hlog("start lock open failed; proceeding unlocked")
    try:
        listener_pids = _own_listener_pids()
        if listener_pids:
            return {"ok": True, "running": True, "already": True, "pid": listener_pids[0], "msg": "이미 실행 중"}
        foreign = _foreign_listener_pids()
        if foreign:
            return {"ok": False, "running": False, "blocked": True,
                    "error": f"포트 {PORT}가 다른 프로세스에 사용 중입니다(pid {foreign[0]}). 브릿지를 시작하지 않았습니다."}
        # The port isn't open for ~40s while the bridge loads models. If a server.py is already starting,
        # a second launch would load a second 26B model (~52GB RAM) — treat an existing PID as "already
        # starting" and never double-launch. _starting_pid() covers the bash preamble ps can't attribute.
        pids = bridge_pids() or ([p for p in (_starting_pid(),) if p])
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
        try:
            with open(START_PID, "w") as f:
                f.write(json.dumps({"pid": p.pid, "ts": time.time()}))   # run_bridge.sh execs server.py -> same pid
        except Exception as e:
            hlog(f"start pidfile err: {e}")
        time.sleep(0.6)                              # catch an instant crash (bad venv etc.)
        if p.poll() is not None:
            return {"ok": False, "error": f"브릿지가 즉시 종료됨(코드 {p.returncode}). 로그: {LOG}"}
        return {"ok": True, "running": False, "starting": True, "pid": p.pid,
                "msg": "기동 중 — 모델 로드에 ~40초", "log": LOG}
    finally:
        if lock is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()
            except Exception:
                pass


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
    spid = _starting_pid()                       # a launcher still in its bash preamble is invisible to ps
    if spid and spid not in pids:
        pids = sorted(pids + [spid])
    if not pids:
        foreign = _foreign_listener_pids()
        if foreign:
            return {"ok": False, "running": False, "blocked": True,
                    "error": f"포트 {PORT}는 다른 프로세스가 사용 중입니다(pid {foreign[0]}). 종료하지 않았습니다."}
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
            if not bridge_pids():
                return {"ok": True, "running": False, "msg": "종료됨"}
            time.sleep(0.5)
    still_running = bool(bridge_pids())
    return {"ok": not still_running, "running": still_running, "msg": "종료 시도 완료"}


def do_restart(msg=None):
    stopped = do_stop()
    if not stopped.get("ok"):
        stopped.setdefault("restart", False)
        return stopped
    if stopped.get("running"):
        stopped.setdefault("ok", False)
        stopped.setdefault("restart", False)
        stopped.setdefault("error", stopped.get("msg") or "브릿지 종료 실패")
        return stopped
    return do_start(msg)


def _venv_python():
    """A python with the project deps (huggingface_hub): LCC_PYTHON, else the repo .venv, else this host's own
    interpreter (on CUDA/WSL the host already runs under the project venv). None if nothing usable exists."""
    for raw in (os.environ.get("LCC_PYTHON"), os.path.join(ROOT, ".venv", "bin", "python"), sys.executable):
        if not raw:
            continue
        p = raw if os.path.sep in raw else shutil.which(raw)
        if p and os.path.exists(p) and _python_ok(p):
            return p
    return None


def _python_ok(path):
    try:
        return subprocess.run(
            [path, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
        ).returncode == 0
    except Exception:
        return False


def _install_running():
    """The live install status dict if a download is in progress (pid alive, not done), else None."""
    st = _install_status_for_running_check()
    if not st:
        return None
    if st.get("done"):
        return None
    pid = st.get("pid")
    if not pid:
        return None                  # seed without a child pid yet -> not a live run
    if not _install_pid_active(pid):
        return None
    return st


def _install_pid_active(pid):
    try:
        pid = int(pid)
        os.kill(pid, 0)
    except Exception:
        return False
    return _cmd_has_path(_pid_command(pid), INSTALLER)


def _write_install_status(st):
    tmp = INSTALL_STATUS + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, INSTALL_STATUS)
    except Exception as e:
        hlog(f"install status write err: {e}")
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def _read_install_status():
    try:
        with open(INSTALL_STATUS) as f:
            st = json.load(f)
        if not isinstance(st, dict):
            raise ValueError(f"install status must be a JSON object, got {type(st).__name__}")
        return st, None
    except FileNotFoundError:
        return None, None
    except Exception as e:
        return None, e


def _install_status_read_error(e):
    return {
        "ok": False,
        "done": True,
        "error": f"설치 상태 파일을 읽지 못했습니다: {e}. 다시 설치를 시작하세요.",
    }


def _install_status_for_running_check():
    st, err = _read_install_status()
    if err:
        hlog(f"install status read err: {err}")
        return None
    return st


def _install_status_for_user():
    st, err = _read_install_status()
    if err:
        hlog(f"install status read err: {err}")
        return _install_status_read_error(err)
    if st is None:
        return {"ok": True, "idle": True}
    return st


def _install_status_failure(st, error):
    failed = {**(st or {}), "done": True, "ok": False, "error": error, "ts": int(time.time())}
    _write_install_status(failed)
    return failed


def do_install(msg=None):
    """Spawn the per-model downloader DETACHED for one (role, model). Returns immediately; popup polls
    install_status. install_models.py pins LCC_LM_MODEL / LCC_WHISPER_MODEL and (Whisper) auto-quantizes."""
    role = str((msg or {}).get("role") or "").strip().lower()
    model = str((msg or {}).get("model") or "").strip()
    if role not in ("asr", "lm"):
        return {"ok": False, "error": f"unknown role: {role} (use asr|lm)"}
    if not model:
        return {"ok": False, "error": "missing model"}
    backend = "cuda" if CUDA_STACK_CMD else "mlx"   # downloader is backend-aware (MLX repos vs CUDA GGUF)
    if _install_running():
        return {"ok": True, "started": False, "already": True, "msg": "이미 설치 중"}
    py = _venv_python()
    if not py:
        return {"ok": False, "error": "Python 3.10+ 환경을 못 찾음 — 먼저 ./setup.sh 를 실행하세요"}
    if not os.path.exists(INSTALLER):
        return {"ok": False, "error": f"install_models.py 없음: {INSTALLER}"}
    seed = {"role": role, "model": model, "backend": backend, "done": False, "ok": True, "current": "시작 중…",
            "index": 0, "total": 0, "ts": int(time.time())}
    _write_install_status(seed)                     # seed status so the poller sees progress instantly
    env = _start_env(msg or {})
    try:
        logf = open(INSTALL_LOG, "ab")
        p = subprocess.Popen([py, INSTALLER, "--role", role, "--model", model, "--backend", backend],
                             stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, env=env)
    except Exception as e:
        hlog(f"install spawn err: {e}")
        _install_status_failure(seed, f"설치 시작 실패: {e}")
        return {"ok": False, "error": f"설치 시작 실패: {e}"}
    _write_install_status({**seed, "pid": p.pid, "ts": int(time.time())})  # record child pid for live polling
    return {"ok": True, "started": True, "role": role, "model": model, "backend": backend,
            "msg": "설치 시작 — 모델 다운로드(수 GB)"}


def do_install_status():
    st = _install_status_for_user()
    if not st.get("ok", True):
        return st
    if st.get("idle"):
        return st
    if st.get("done"):
        return {"ok": True, **st}
    pid = st.get("pid")
    age = int(time.time()) - int(st.get("ts") or 0)
    if not pid:
        if age > 15:
            st = _install_status_failure(st, "설치 프로세스가 시작되지 않았습니다 — 다시 시도하세요")
        return {"ok": True, **st}
    if not _install_pid_active(pid):
        st = _install_status_failure(st, f"설치 프로세스가 종료됐지만 완료 상태가 없습니다(pid {pid}). 로그: {INSTALL_LOG}")
    return {"ok": True, **st}


def do_models_status(msg=None):
    """Per-curated-model installed flags for the popup's download buttons. Runs in the bridge venv (the
    host's own interpreter may lack the model deps) so server's registry + install_models.is_installed
    are importable. Returns {ok, backend, asr:[{id,label,installed}], lm:[...]}."""
    py = _venv_python()
    if not py:
        return {"ok": False, "error": "Python 환경을 못 찾음 — 먼저 ./setup.sh 를 실행하세요"}
    backend = "cuda" if CUDA_STACK_CMD else "mlx"
    bridge_dir = os.path.join(ROOT, "bridge")
    code = (
        "import json,sys; sys.path.insert(0, sys.argv[1]);"
        "import server as s, install_models as im; b=sys.argv[2];"
        "f=lambda role, ms:[{'id':m['id'],'label':m['label'],'engine':m.get('engine'),'repo':m.get('repo'),'installed':bool(im.is_installed(role,m['id'],b))} for m in ms];"
        "print(json.dumps({'asr':f('asr',s.asr_models(b)),'lm':f('lm',s.lm_models(b))}))"
    )
    try:
        out = subprocess.run([py, "-c", code, bridge_dir, backend], capture_output=True, text=True, timeout=30)
    except Exception as e:
        return {"ok": False, "error": f"models_status 실행 실패: {e}"}
    if out.returncode != 0:
        hlog(f"models_status rc={out.returncode} stderr={out.stderr[-500:]}")
        return {"ok": False, "error": (out.stderr.strip() or "models_status 실패")[-600:]}
    try:
        data = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as e:
        return {"ok": False, "error": f"models_status 파싱 실패: {e}"}
    return {"ok": True, "backend": backend, **data}


def handle_message(msg):
    cmd = str(msg.get("cmd") or "status").strip().lower()
    hlog(f"cmd={cmd}")
    if cmd == "start":
        return do_start(msg)
    if cmd == "stop":
        return do_stop()
    if cmd == "restart":
        return do_restart(msg)
    if cmd == "install":
        return do_install(msg)
    if cmd == "install_status":
        return do_install_status()
    if cmd == "models_status":
        return do_models_status(msg)
    if cmd == "status" and CUDA_STACK_CMD:
        data = _cuda_stack(["status", _asr_engine(msg)], timeout=20) or {}
        return {
            "ok": bool(data.get("ok", True)),
            "running": bool(data.get("running") or data.get("bridge")),
            "pid": data.get("pid"),
            "detail": data,
        }
    if cmd == "status":
        return do_status()
    return {"ok": False, "error": f"unknown command: {cmd}"}


def _cli_msg(argv):
    if not argv or argv[0] in ("-h", "--help"):
        raise ValueError("usage: lcc_bridge_host.py status|start|stop|restart|install_status [--asr granite|qwen3|parakeet]")
    cmd = argv[0].strip().lower()
    if cmd not in ("status", "start", "stop", "restart", "install_status"):
        raise ValueError(f"unknown command: {argv[0]}")
    msg = {"cmd": cmd}
    i = 1
    while i < len(argv):
        if argv[i] == "--asr" and i + 1 < len(argv):
            msg["asrEngine"] = argv[i + 1]
            i += 2
            continue
        raise ValueError(f"unknown arg: {argv[i]}")
    return msg


def run_cli(argv):
    try:
        msg = _cli_msg(argv)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        reply = handle_message(msg)
    except Exception as e:
        hlog(f"cli err: {e}")
        reply = {"ok": False, "error": str(e)}
    print(json.dumps(reply, ensure_ascii=False, sort_keys=True))
    return 0 if reply.get("ok") and not reply.get("blocked") else 1


def _native_messaging_origin_arg(argv):
    if not argv:
        return False
    first = str(argv[0])
    return first.startswith("chrome-extension://") or first.startswith("moz-extension://")


def main():
    try:
        msg = read_message()
    except Exception as e:
        hlog(f"read err: {e}")
        send_message({"ok": False, "error": f"메시지 파싱 실패: {e}"})
        return
    if not msg:
        return
    try:
        reply = handle_message(msg)
    except Exception as e:
        hlog(f"handler err: {e}")
        reply = {"ok": False, "error": str(e)}
    send_message(reply)


if __name__ == "__main__":
    if len(sys.argv) > 1 and not _native_messaging_origin_arg(sys.argv[1:]):
        raise SystemExit(run_cli(sys.argv[1:]))
    main()
