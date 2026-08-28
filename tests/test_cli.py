import json
import hashlib
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture
def fixture_model_server():
    """Real HTTP transport, scripted responses. This is explicitly not a live model."""

    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            Handler.calls += 1
            if Handler.calls == 1:
                name, args = "file_read", {"path": "calculator.py"}
            elif Handler.calls == 2:
                observation = json.loads(request["messages"][-1]["content"])
                name, args = (
                    "file_edit",
                    {
                        "path": "calculator.py",
                        "old": "a - b",
                        "new": "a + b",
                        "expected_sha256": observation["sha256"],
                    },
                )
            else:
                response = {
                    "choices": [{"message": {"role": "assistant", "content": "Fixture repair complete."}}]
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
                return
            response = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": str(Handler.calls),
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args)},
                                }
                            ],
                        }
                    }
                ]
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def cli(args, input=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run(
        [sys.executable, "-m", "scrappy_forge", *args],
        input=input,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )


def test_cli_one_shot_real_http_and_tests(repo, tmp_path, fixture_model_server):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "home": str(tmp_path / "state"),
                "provider": "local",
                "model": "SCRIPTED-HTTP-FIXTURE",
                "endpoint": fixture_model_server,
                "execution": "trusted-local",
                "allow_tools": ["file_edit", "verification_run"],
                "checks": [
                    {
                        "name": "unit",
                        "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                        "timeout": 10,
                    }
                ],
            }
        )
    )
    result = cli(
        ["--repo", str(repo), "--config", str(config), "--allow-local-execution", "-p", "Fix addition"]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "review_ready" in result.stdout
    report = next((tmp_path / "state/sessions").glob("*/report.json"))
    data = json.loads(report.read_text())
    assert data["api_attempts"] == 3
    assert data["verification"][0]["passed"]
    assert not data["baseline_verification"][0]["passed"]
    assert "a - b" in (repo / "calculator.py").read_text()
    denied = cli(["apply", data["id"], "--config", str(config)])
    assert denied.returncode == 2
    assert "a - b" in (repo / "calculator.py").read_text()
    patch_hash = hashlib.sha256(report.with_name("changes.patch").read_bytes()).hexdigest()
    applied = cli(["apply", data["id"], "--config", str(config), "--expected-patch", patch_hash])
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "a + b" in (repo / "calculator.py").read_text()


def test_repl_commands_and_persistent_memory(repo, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"home": str(tmp_path / "state")}))
    result = cli(
        ["--repo", str(repo), "--config", str(config)],
        input="/help\n/remember Prefer simple functions\n/memory functions\n/tools\n/status\n/quit\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Prefer simple functions" in result.stdout
    assert "terminal_run" in result.stdout
    assert "Saved. Resume:" in result.stdout


def test_noninteractive_upload_requires_consent(repo, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"home": str(tmp_path / "state")}))
    result = cli(["--repo", str(repo), "--config", str(config), "-p", "Read files"])
    assert result.returncode == 2
    assert "allow-code-upload" in result.stderr


def test_doctor_no_key_required(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"home": str(tmp_path / "state")}))
    result = cli(["doctor", "--config", str(config)])
    assert result.returncode == 0
    assert "mcp_sdk" in result.stdout


def test_cli_mcp_lifecycle_does_not_execute_server(tmp_path):
    config = tmp_path / "config.json"
    result = cli(["mcp", "add", "fixture", "--command", "/does/not/exist", "--config", str(config)])
    assert result.returncode == 0, result.stderr
    assert json.loads(config.read_text())["mcp"]["fixture"]["command"] == "/does/not/exist"
    assert '"connected":false' in result.stdout
    duplicate = cli(["mcp", "add", "fixture", "--command", "other", "--config", str(config)])
    assert duplicate.returncode == 2
    removed = cli(["mcp", "remove", "fixture", "--config", str(config)])
    assert removed.returncode == 0
    assert json.loads(config.read_text())["mcp"] == {}


def test_cli_mcp_import_atomic_and_secret_references_only(tmp_path):
    config = tmp_path / "config.json"
    imported = tmp_path / "servers.json"
    imported.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer ${SERVICE_TOKEN}"},
                    },
                    "local": {"command": "python", "env": {"API_TOKEN": "${SOURCE_TOKEN}"}},
                }
            }
        )
    )
    result = cli(["mcp", "import", str(imported), "--config", str(config)])
    assert result.returncode == 0, result.stderr
    data = json.loads(config.read_text())
    assert data["mcp"]["remote"]["headers_env"]["Authorization"] == {
        "env": "SERVICE_TOKEN",
        "prefix": "Bearer ",
    }
    assert data["mcp"]["local"]["env"] == {"API_TOKEN": "SOURCE_TOKEN"}
    before = config.read_bytes()
    imported.write_text(
        json.dumps({"mcpServers": {"bad": {"command": "python", "env": {"KEY": "literal-secret-refused"}}}})
    )
    rejected = cli(["mcp", "import", str(imported), "--config", str(config)])
    assert rejected.returncode == 2
    assert "literal-secret-refused" not in rejected.stderr
    assert config.read_bytes() == before


def test_cli_skill_registration_and_removal(tmp_path):
    config = tmp_path / "config.json"
    body = tmp_path / "workflow.md"
    body.write_text("Read code and verify changes.")
    manifest = tmp_path / "skill.json"
    manifest.write_text(json.dumps({"format": 1, "name": "workflow", "version": "1.0.0", "file": body.name}))
    added = cli(["skills", "add", str(manifest), "--config", str(config)])
    assert added.returncode == 0, added.stderr
    listed = cli(["skills", "list", "--config", str(config)])
    assert '"name":"workflow"' in listed.stdout
    removed = cli(["skills", "remove", "workflow", "--config", str(config)])
    assert removed.returncode == 0
    assert json.loads(config.read_text())["skills"] == []
