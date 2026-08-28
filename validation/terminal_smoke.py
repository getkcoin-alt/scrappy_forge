"""Installed-CLI integration check with a real PTY and explicitly scripted fixtures.

Run from a virtual environment with Forge and its dev/auth extras installed.
Only generated projects and local test servers are used. No real model or account.
"""

import errno
import fcntl
import importlib.util
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

import httpx


def load_fixture(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    root = Path(__file__).resolve().parents[1]
    validation = Path(__file__).resolve().parent
    forge = Path(sys.executable).with_name("forge")
    if not forge.exists():
        raise SystemExit("Install Forge into this Python virtual environment first")
    cli_tests = load_fixture(root / "tests/test_cli.py", "forge_cli_fixture")
    auth_tests = load_fixture(root / "tests/test_auth.py", "forge_auth_fixture")
    model_fixture = cli_tests.fixture_model_server.__wrapped__()
    auth_fixture = auth_tests.oauth_server.__wrapped__()
    model_endpoint, auth_server = next(model_fixture), next(auth_fixture)
    temp = Path(tempfile.mkdtemp(prefix="forge-v3-terminal-"))
    project = temp / "project"
    (project / "tests").mkdir(parents=True)
    (project / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
    (project / "tests/test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\nclass AdditionTest(unittest.TestCase):\n"
        "    def test_add(self): self.assertEqual(add(2, 3), 5)\n"
    )
    config = temp / "config.json"
    config.write_text(
        json.dumps(
            {
                "home": str(temp / "state"),
                "provider": "local",
                "model": "SCRIPTED-HTTP-FIXTURE",
                "endpoint": model_endpoint,
                "execution": "trusted-local",
                "checks": [
                    {"name": "unit", "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests"]}
                ],
                "mcp": {
                    "authfixture": {
                        "transport": "http",
                        "url": auth_server["url"] + "/mcp",
                        "allow_loopback": True,
                        "oauth": {"callback_port": auth_tests.free_port(), "scopes": "read"},
                    }
                },
            }
        )
    )
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 180, 0, 0))
    env = os.environ.copy()
    env.update(TERM="dumb", PROMPT_TOOLKIT_NO_CPR="1")
    proc = subprocess.Popen(
        [str(forge), "--repo", str(project), "--config", str(config), "--allow-local-execution"],
        cwd=temp,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        start_new_session=True,
    )
    os.close(slave)
    transcript = bytearray()

    def until(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError("Terminal response timed out; inspect terminal-smoke.txt")
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b""
                if not chunk:
                    raise AssertionError("Terminal exited unexpectedly")
                transcript.extend(chunk)

    def expect(needle, start=0):
        until(lambda: needle.encode() in transcript[start:])

    def send(line, expected):
        start = len(transcript)
        os.write(master, (line + "\n").encode())
        expect(expected, start)
        return start

    try:
        expect("forge>")
        send("/help", "Ordinary text starts")
        send("/remember Prefer simple functions", '"id":1')
        send("/memory functions", '"stale":false')
        send("/mcp connect authfixture", "Type approve to run once")
        send("approve", "Type approve to run once")
        auth_start = send("approve", "Open this sign-in link")
        pattern = re.compile(rb"(http://127\.0\.0\.1:\d+/authorize\?[^\r\n]+)[\r\n]")
        until(lambda: pattern.search(transcript[auth_start:]) is not None)
        url = pattern.search(transcript[auth_start:]).group(1).decode()
        with httpx.Client(follow_redirects=True, timeout=5, trust_env=False) as browser:
            assert browser.get(url).status_code == 200
        expect('"connected":true', auth_start)
        send("Fix addition. Preserve the tests.", "Type approve to run once")
        send("approve", "Type approve to run once")
        send("approve", "Type approve to run once")
        send("approve", "Status: review_ready")
        assert "a - b" in (project / "calculator.py").read_text()
        send("/apply", "Type approve to run once")
        send("approve", '"already_applied":false')
        assert "a + b" in (project / "calculator.py").read_text()
        send("/status", '"api_attempts":3')
        send("/quit", "Saved. Resume:")
        assert proc.wait(timeout=10) == 0
        report = next((temp / "state/sessions").glob("*/report.json"))
        data = json.loads(report.read_text())
        assert data["evidence_current"] and data["application"]["tree_hash"] == data["current_hash"]
        assert not data["baseline_verification"][0]["passed"] and data["verification"][0]["passed"]
        assert auth_server["authorizations"] == 1
        shutil.copy2(report, validation / "terminal-flow.report.json")
        shutil.copy2(report.with_name("changes.patch"), validation / "terminal-flow.patch")
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        os.close(master)
        model_fixture.close()
        auth_fixture.close()
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", transcript.decode(errors="replace")).replace("\r", "")
        (validation / "terminal-smoke.txt").write_text(text)
    summary = {
        "installed_entrypoint": str(forge),
        "real_pty": True,
        "interactive_approvals": True,
        "oauth_and_mcp_real_local_http": True,
        "browser_redirect_simulated": True,
        "model": "SCRIPTED-HTTP-FIXTURE, NOT AN LLM",
        "model_http_requests": 3,
        "baseline_failed_final_passed": True,
        "original_unchanged_before_apply": True,
        "original_updated_after_apply_approval": True,
        "exit_code": proc.returncode,
        "transcript": "validation/terminal-smoke.txt",
    }
    (validation / "terminal-smoke.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
