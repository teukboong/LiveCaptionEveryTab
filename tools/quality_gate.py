#!/usr/bin/env python3
"""Repository-local quality gate for dependency boundaries and core file size.

The project intentionally avoids a Node dependency tree for the extension tests.
This script keeps the same zero-install shape while making the most important
architecture rules executable in check.sh.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension"
LIVE_BRIDGE_TESTS = {
    "bridge/test_stream_wav.py",
    "bridge/test_target.py",
}


@dataclass(frozen=True)
class FileRule:
    path: Path
    max_lines: int | None = None
    max_function_lines: int | None = None
    max_brace_depth: int | None = None
    forbidden: tuple[tuple[str, str], ...] = ()


RULES = (
    FileRule(
        EXT / "protocol.js",
        max_lines=260,
        max_function_lines=70,
        max_brace_depth=5,
        forbidden=(
            (r"\bchrome\.", "protocol.js must stay browser-API free"),
            (r"\bdocument\.", "protocol.js must stay DOM free"),
            (r"\bwindow\.", "protocol.js must stay DOM free"),
            (r"\bWebSocket\b", "protocol.js must not own transport runtime"),
            (r"\bimportScripts\s*\(", "protocol.js must not import layers"),
            (r"\bfetch\s*\(", "protocol.js must stay IO free"),
        ),
    ),
    FileRule(
        EXT / "background.js",
        max_lines=430,
        max_function_lines=70,
        max_brace_depth=5,
        forbidden=(
            (r"\bdocument\.", "background.js must stay DOM free"),
            (r"\bwindow\.", "background.js must stay DOM free"),
            (r"\bWebSocket\b", "background.js must route through offscreen"),
        ),
    ),
    FileRule(
        EXT / "offscreen.js",
        max_lines=450,
        max_function_lines=70,
        max_brace_depth=5,
        forbidden=(
            (r"\bchrome\.storage\.", "offscreen.js must receive settings from background"),
            (r"\bdocument\.", "offscreen.js must not touch page DOM"),
            (r"\bwindow\.", "offscreen.js must not own page globals"),
        ),
    ),
)


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        fail(f"missing required file: {rel(path)}")


def fail(message: str) -> None:
    print(f"quality_gate: {message}", file=sys.stderr)
    raise SystemExit(1)


def assert_file_rules() -> None:
    for rule in RULES:
        text = read(rule.path)
        lines = text.splitlines()
        if rule.max_lines is not None and len(lines) > rule.max_lines:
            fail(f"{rel(rule.path)} has {len(lines)} lines; max is {rule.max_lines}")
        if rule.max_function_lines is not None:
            assert_named_function_sizes(rule.path, lines, rule.max_function_lines)
        if rule.max_brace_depth is not None:
            assert_brace_depth(rule.path, lines, rule.max_brace_depth)
        for pattern, reason in rule.forbidden:
            match = re.search(pattern, text)
            if match:
                line = text[: match.start()].count("\n") + 1
                fail(f"{rel(rule.path)}:{line}: {reason}")


def assert_named_function_sizes(path: Path, lines: list[str], max_lines: int) -> None:
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        if re.search(r"\b(async\s+)?function\s+\w+\s*\(", line):
            starts.append((i, line.strip()))
        elif re.search(r"\bglobalThis\.\w+\s*=\s*function\s+\w*\s*\(", line):
            starts.append((i, line.strip()))

    for start, label in starts:
        depth = 0
        seen_body = False
        for end in range(start, len(lines)):
            for ch in lines[end]:
                if ch == "{":
                    depth += 1
                    seen_body = True
                elif ch == "}":
                    depth -= 1
            if seen_body and depth <= 0:
                size = end - start + 1
                if size > max_lines:
                    fail(f"{rel(path)}:{start + 1}: function has {size} lines; max is {max_lines}: {label}")
                break


def assert_brace_depth(path: Path, lines: list[str], max_depth: int) -> None:
    depth = 0
    for line_no, line in enumerate(lines, start=1):
        for ch in line:
            if ch == "{":
                depth += 1
                if depth > max_depth:
                    fail(f"{rel(path)}:{line_no}: brace depth {depth} exceeds max {max_depth}")
            elif ch == "}":
                depth = max(0, depth - 1)


def assert_injected_content_order() -> None:
    manifest = json.loads(read(EXT / "manifest.json"))
    scripts = manifest["content_scripts"][0]["js"]
    expected = ["protocol.js", "pcm.js", "page-seed.js", "content.js", "delay.js"]
    if scripts != expected:
        fail(f"manifest content script order must be {expected!r}; got {scripts!r}")

    background = read(EXT / "background.js")
    m = re.search(r"const\s+LCC_CONTENT_FILES\s*=\s*(\[[^\]]+\])", background)
    if not m:
        fail("background.js must declare LCC_CONTENT_FILES")
    background_scripts = json.loads(m.group(1))
    if background_scripts != expected:
        fail(f"background injection order must be {expected!r}; got {background_scripts!r}")


def assert_protocol_loads_first() -> None:
    popup = read(EXT / "popup.html")
    offscreen = read(EXT / "offscreen.html")
    if popup.find('src="protocol.js"') < 0:
        fail("popup.html must load protocol.js")
    if popup.find('src="protocol.js"') > popup.find('src="popup.js"'):
        fail("popup.html must load protocol.js before popup.js")
    if offscreen.find('src="protocol.js"') < 0:
        fail("offscreen.html must load protocol.js")
    if offscreen.find('src="protocol.js"') > offscreen.find('src="offscreen.js"'):
        fail("offscreen.html must load protocol.js before offscreen.js")


def assert_check_sh_covers_default_tests() -> None:
    check_sh = read(ROOT / "check.sh")
    tests = sorted(
        rel(path)
        for base in (ROOT / "bridge", ROOT / "extension", ROOT / "extension" / "native-host")
        for path in base.glob("test_*")
        if path.suffix in {".py", ".js"}
    )

    def check_sh_mentions(path: str) -> bool:
        return path in check_sh or (path.startswith("bridge/") and Path(path).name in check_sh)

    missing = [path for path in tests if path not in LIVE_BRIDGE_TESTS and not check_sh_mentions(path)]
    if missing:
        fail(f"check.sh must run model-free tests: {missing!r}")

    live_in_default = [path for path in LIVE_BRIDGE_TESTS if check_sh_mentions(path)]
    if live_in_default:
        fail(f"check.sh must keep live websocket/model tests out of the fast default gate: {live_in_default!r}")


def main() -> None:
    assert_file_rules()
    assert_injected_content_order()
    assert_protocol_loads_first()
    assert_check_sh_covers_default_tests()
    print("quality_gate: OK (extension boundaries and SSOT load order pass)")


if __name__ == "__main__":
    main()
