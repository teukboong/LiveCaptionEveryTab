#!/usr/bin/env python3
"""Model-free tests for the native-messaging host helper layer.

These pin the startup/install guardrails that the popup relies on, without launching the bridge or
touching Chrome's native-host registry.
"""
import importlib.util
import os
import stat
import tempfile
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

        host.port_listener_pids = lambda: [202]
        status = host.do_status()
        check("status.foreign_running", status.get("running"), False)
        check("status.foreign_blocked", status.get("blocked"), True)
        check("start.foreign_blocked", host.do_start({}).get("blocked"), True)
        check("stop.foreign_blocked", host.do_stop().get("blocked"), True)
    finally:
        host.ROOT = old_root
        host.port_listener_pids = old_port_listener_pids
        host._pid_command = old_pid_command

check("asr.granite", host._asr_engine({"asrEngine": "granite"}), "granite")
check("asr.qwen3", host._asr_engine({"asrEngine": "qwen3"}), "qwen3")
check("asr.parakeet", host._asr_engine({"asrEngine": "parakeet"}), "parakeet")
check("asr.invalid", host._asr_engine({"asrEngine": "whisper"}), "granite")

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
