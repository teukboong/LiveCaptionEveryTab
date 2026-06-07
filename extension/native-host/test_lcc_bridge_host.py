#!/usr/bin/env python3
"""Model-free tests for the native-messaging host helper layer.

These pin the startup/install guardrails that the popup relies on, without launching the bridge or
touching Chrome's native-host registry.
"""
import importlib.util
import io
import json
import os
import stat
import struct
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HOST_PATH = ROOT / "extension" / "native-host" / "lcc_bridge_host.py"


def load_host():
    spec = importlib.util.spec_from_file_location("lcc_bridge_host_under_test", HOST_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_python(path, exit_code):
    path.write_text(f"#!/bin/sh\nexit {int(exit_code)}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")


def ok(name, cond):
    if not cond:
        fails.append(name)


host = load_host()


class FakeStdin:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)


class FakeStdout:
    def __init__(self):
        self.buffer = io.BytesIO()


old_stdin = sys.stdin
old_stdout = sys.stdout
try:
    payload = json.dumps({"cmd": "status"}).encode()
    sys.stdin = FakeStdin(struct.pack("<I", len(payload)) + payload)
    check("read_message.valid", host.read_message(), {"cmd": "status"})

    sys.stdin = FakeStdin(struct.pack("<I", 5) + b"{}")
    try:
        host.read_message()
    except ValueError as e:
        ok("read_message.truncated", "truncated native message" in str(e))
    else:
        ok("read_message.truncated", False)

    sys.stdin = FakeStdin(struct.pack("<I", host.MAX_MESSAGE_BYTES + 1))
    try:
        host.read_message()
    except ValueError as e:
        ok("read_message.too_large", "too large" in str(e))
    else:
        ok("read_message.too_large", False)

    payload = json.dumps(["status"]).encode()
    sys.stdin = FakeStdin(struct.pack("<I", len(payload)) + payload)
    try:
        host.read_message()
    except ValueError as e:
        ok("read_message.non_object", "JSON object" in str(e))
    else:
        ok("read_message.non_object", False)

    fake_out = FakeStdout()
    sys.stdout = fake_out
    host.send_message({"ok": False, "error": "메시지 파싱 실패"})
    out = fake_out.buffer.getvalue()
    (n,) = struct.unpack("<I", out[:4])
    body = out[4:4+n]
    ok("send_message.utf8_korean", "메시지 파싱 실패".encode("utf-8") in body)
    check("send_message.round_trip", json.loads(body.decode("utf-8")).get("ok"), False)
finally:
    sys.stdin = old_stdin
    sys.stdout = old_stdout

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    good = fake_python(tmp_path / "py-good", 0)
    bad = fake_python(tmp_path / "py-bad", 1)

    check("python_ok.good", host._python_ok(good), True)
    check("python_ok.bad", host._python_ok(bad), False)
    check("python_ok.missing", host._python_ok(str(tmp_path / "missing-python")), False)

    old_env = dict(os.environ)
    old_root = host.ROOT
    try:
        os.environ["LCC_PYTHON"] = good
        check("venv_python.explicit_path", host._venv_python(), good)

        os.environ["PATH"] = f"{tmp_path}{os.pathsep}{old_env.get('PATH', '')}"
        os.environ["LCC_PYTHON"] = "py-good"
        check("venv_python.command_name", host._venv_python(), good)

        os.environ.pop("LCC_PYTHON", None)
        venv_py = tmp_path / ".venv" / "bin" / "python"
        venv_py.parent.mkdir(parents=True)
        fake_python(venv_py, 0)
        host.ROOT = str(tmp_path)
        check("venv_python.repo_venv", host._venv_python(), str(venv_py))
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        host.ROOT = old_root

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    repo = tmp_path / "repo"
    server = repo / "bridge" / "server.py"
    server.parent.mkdir(parents=True)
    server.write_text("# fake bridge\n")

    old_root = host.ROOT
    old_port_listener_pids = host.port_listener_pids
    old_pid_command = host._pid_command
    old_bridge_pids = host.bridge_pids
    old_do_stop = host.do_stop
    old_do_start = host.do_start
    try:
        host.ROOT = str(repo)
        own_cmd = f"/venv/bin/python -u {server}"
        foreign_cmd = "/venv/bin/python -u /tmp/other-checkout/bridge/server.py"
        prefix_cmd = f"/venv/bin/python -u {server}.bak"

        check("cmd_match.own", host._cmd_matches_this_bridge(own_cmd), True)
        check("cmd_match.foreign", host._cmd_matches_this_bridge(foreign_cmd), False)
        check("cmd_match.prefix_collision", host._cmd_matches_this_bridge(prefix_cmd), False)

        host.port_listener_pids = lambda: [101, 202]
        host._pid_command = lambda pid: own_cmd if int(pid) == 101 else foreign_cmd
        check("bridge_pids.filters_foreign", host.bridge_pids(), [101])
        check("foreign_listener.filters_own", host._foreign_listener_pids(), [202])

        ready = host.do_status()
        check("status.ready_running", ready.get("running"), True)
        check("status.ready_starting", ready.get("starting"), False)
        check("status.ready_pid", ready.get("pid"), 101)

        host.port_listener_pids = lambda: []
        host.bridge_pids = lambda: [101]
        starting = host.do_status()
        check("status.starting_running", starting.get("running"), False)
        check("status.starting_flag", starting.get("starting"), True)
        check("status.starting_pid", starting.get("pid"), 101)

        host.bridge_pids = old_bridge_pids
        host.port_listener_pids = lambda: [202]
        status = host.do_status()
        check("status.foreign_running", status.get("running"), False)
        check("status.foreign_starting", status.get("starting"), False)
        check("status.foreign_blocked", status.get("blocked"), True)
        check("start.foreign_blocked", host.do_start({}).get("blocked"), True)
        check("stop.foreign_blocked", host.do_stop().get("blocked"), True)

        start_calls = []
        host.do_stop = lambda: {"ok": False, "running": True, "error": "still running"}
        host.do_start = lambda msg=None: start_calls.append(msg) or {"ok": True}
        restart = host.do_restart({})
        check("restart.stop_failure_ok", restart.get("ok"), False)
        check("restart.stop_failure_not_started", len(start_calls), 0)

        host.do_stop = lambda: {"ok": True, "running": False}
        restart = host.do_restart({"asrEngine": "parakeet"})
        check("restart.starts_after_stop", restart.get("ok"), True)
        check("restart.start_msg_forwarded", start_calls, [{"asrEngine": "parakeet"}])
    finally:
        host.ROOT = old_root
        host.port_listener_pids = old_port_listener_pids
        host._pid_command = old_pid_command
        host.bridge_pids = old_bridge_pids
        host.do_stop = old_do_stop
        host.do_start = old_do_start

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    status_path = tmp_path / "install-status.json"
    log_path = tmp_path / "install.log"
    installer = tmp_path / "install_models.py"
    installer.write_text("# fake installer\n")

    old_status = host.INSTALL_STATUS
    old_log = host.INSTALL_LOG
    old_installer = host.INSTALLER
    old_venv_python = host._venv_python
    old_popen = host.subprocess.Popen
    old_pid_command = host._pid_command
    old_kill = host.os.kill
    try:
        host.INSTALL_STATUS = str(status_path)
        host.INSTALL_LOG = str(log_path)
        host.INSTALLER = str(installer)

        host._write_install_status({"done": False, "ok": True, "tier": "lite"})
        check("install_status.atomic_write_tmp_absent", status_path.with_suffix(status_path.suffix + ".tmp").exists(), False)
        check("install_status.atomic_write_json", json.loads(status_path.read_text()).get("tier"), "lite")

        status_path.write_text("{")
        broken = host.do_install_status()
        check("install_status.broken_json_ok", broken.get("ok"), False)
        check("install_status.broken_json_done", broken.get("done"), True)
        check("install_running.broken_json", host._install_running(), None)

        status_path.write_text(json.dumps({"tier": "lite", "done": False, "ok": True, "ts": int(time.time())}))
        fresh_seed = host.do_install_status()
        check("install_status.fresh_seed_done", fresh_seed.get("done"), False)
        check("install_status.fresh_seed_ok", fresh_seed.get("ok"), True)

        status_path.write_text(json.dumps({"tier": "lite", "done": False, "ok": True, "ts": int(time.time()) - 30}))
        stale_seed = host.do_install_status()
        check("install_status.stale_seed_done", stale_seed.get("done"), True)
        check("install_status.stale_seed_ok", stale_seed.get("ok"), False)

        status_path.write_text(json.dumps({"tier": "mid", "done": False, "ok": True, "pid": 99999999, "ts": int(time.time())}))
        dead_pid = host.do_install_status()
        check("install_status.dead_pid_done", dead_pid.get("done"), True)
        check("install_status.dead_pid_ok", dead_pid.get("ok"), False)
        ok("install_status.dead_pid_log_hint", "로그:" in dead_pid.get("error", ""))

        host.os.kill = lambda _pid, _sig: None
        host._pid_command = lambda pid: f"/usr/bin/python3 {installer}" if int(pid) == 303 else "/usr/bin/python3 /tmp/other/install_models.py"
        status_path.write_text(json.dumps({"tier": "full", "done": False, "ok": True, "pid": 303, "ts": int(time.time())}))
        active_install = host.do_install_status()
        check("install_status.active_installer_done", active_install.get("done"), False)
        check("install_running.matches_installer", bool(host._install_running()), True)

        status_path.write_text(json.dumps({"tier": "full", "done": False, "ok": True, "pid": 404, "ts": int(time.time())}))
        foreign_pid = host.do_install_status()
        check("install_status.foreign_pid_done", foreign_pid.get("done"), True)
        check("install_status.foreign_pid_ok", foreign_pid.get("ok"), False)

        host._venv_python = lambda: "/usr/bin/python3"
        def raise_popen(*_args, **_kwargs):
            raise OSError("boom")
        host.subprocess.Popen = raise_popen
        spawn_fail = host.do_install({"tier": "lite"})
        check("install.spawn_fail_ok", spawn_fail.get("ok"), False)
        failed_status = json.loads(status_path.read_text())
        check("install.spawn_fail_status_done", failed_status.get("done"), True)
        check("install.spawn_fail_status_ok", failed_status.get("ok"), False)
    finally:
        host.INSTALL_STATUS = old_status
        host.INSTALL_LOG = old_log
        host.INSTALLER = old_installer
        host._venv_python = old_venv_python
        host.subprocess.Popen = old_popen
        host._pid_command = old_pid_command
        host.os.kill = old_kill

check("asr.granite", host._asr_engine({"asrEngine": "granite"}), "granite")
check("asr.qwen3", host._asr_engine({"asrEngine": "qwen3"}), "qwen3")
check("asr.parakeet", host._asr_engine({"asrEngine": "parakeet"}), "parakeet")
check("asr.invalid", host._asr_engine({"asrEngine": "whisper"}), "granite")

check("cli.status", host._cli_msg(["status"]), {"cmd": "status"})
check("cli.stop", host._cli_msg(["stop"]), {"cmd": "stop"})
check("cli.start_asr", host._cli_msg(["start", "--asr", "parakeet"]), {"cmd": "start", "asrEngine": "parakeet"})
old_do_status = host.do_status
try:
    host.do_status = lambda: {"ok": True, "marker": "status-default"}
    check("handler.missing_cmd_defaults_status", host.handle_message({}).get("marker"), "status-default")
    check("handler.unknown_cmd_fails", host.handle_message({"cmd": "wat"}).get("ok"), False)
except Exception as e:
    fails.append(f"handler checks raised: {e!r}")
finally:
    host.do_status = old_do_status
try:
    host._cli_msg(["install", "--tier", "full"])
except ValueError:
    check("cli.install_not_exposed", True, True)
else:
    check("cli.install_not_exposed", False, True)
try:
    host._cli_msg(["status", "--tier"])
except ValueError:
    check("cli.reject_unknown_arg", True, True)
else:
    check("cli.reject_unknown_arg", False, True)

env = host._start_env({"asrEngine": "parakeet"})
check("start_env.parakeet", env.get("LCC_ASR_ENGINE"), "parakeet")
ok("start_env.path_has_common_bins", "/opt/homebrew/bin" in env.get("PATH", ""))
ok("start_env.parakeet_model", bool(env.get("LCC_PARAKEET_MODEL_DIR")))
check("start_env.invalid_asr", host._start_env({"asrEngine": "whisper"}).get("LCC_ASR_ENGINE"), None)

if fails:
    print(f"FAIL ({len(fails)} case(s)):")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)

print("test_lcc_bridge_host: OK (native-host python selection + config guards pass)")
