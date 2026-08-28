"""Build/install the wheel, test masked setup and a real local release server.

No real API key, remote inference or identity provider is used. All generated
state stays in a printed temporary directory. Requires uv and the dev extras.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pty
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def run(argv, *, cwd, env):
    result = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=240)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {argv[0]}\n{result.stderr[-4000:]}")
    return result.stdout


def masked_setup(forge, root, env):
    master, slave = pty.openpty()
    transcript = bytearray()
    child = subprocess.Popen(
        [str(forge), "setup", "--skip-validation"],
        cwd=root,
        env={**env, "TERM": "dumb", "PROMPT_TOOLKIT_NO_CPR": "1"},
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    os.close(slave)

    def expect(text):
        deadline = time.monotonic() + 15
        while text.encode() not in transcript:
            if time.monotonic() > deadline:
                raise AssertionError("Masked setup prompt timed out")
            if select.select([master], [], [], 0.1)[0]:
                transcript.extend(os.read(master, 65536))

    try:
        expect("OpenRouter API key:")
        fixture = "fixture-not-a-real-openrouter-key"
        os.write(master, (fixture + "\n").encode())
        expect("Type save to store")
        os.write(master, b"save\n")
        expect("Saved locally with 0600")
        assert child.wait(timeout=10) == 0
        assert fixture.encode() not in transcript, "Secret input was echoed"
        record = Path(env["SCRAPPY_FORGE_HOME"]) / "credentials/secrets.json"
        assert stat.S_IMODE(record.stat().st_mode) == 0o600
        assert json.loads(record.read_text())["openrouter"] == fixture
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        os.close(master)
    run([str(forge), "setup", "--forget-key"], cwd=root, env=env)
    assert "openrouter" not in json.loads(record.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, help="Use an already built release wheel")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("Install uv before running release verification")
    root = Path(tempfile.mkdtemp(prefix="forge-release-check-"))
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "OPENROUTER_API_KEY"}}
    # Never alter HOME or any configured user's tool/state directory.
    env.update(
        UV_TOOL_DIR=str(root / "tools"),
        UV_TOOL_BIN_DIR=str(root / "bin"),
        UV_LINK_MODE="copy",
        SCRAPPY_FORGE_HOME=str(root / "state"),
        SCRAPPY_FORGE_CONFIG=str(root / "config.json"),
    )
    print(f"Release verification workspace: {root}", flush=True)
    if args.wheel:
        wheel = args.wheel.resolve(strict=True)
    else:
        run(
            [uv, "--no-config", "build", "--wheel", "--out-dir", str(root / "dist"), str(source)],
            cwd=root,
            env=env,
        )
        wheel = next((root / "dist").glob("scrappy_forge-*-py3-none-any.whl"))
    install = [
        uv,
        "--no-config",
        "tool",
        "install",
        "--python",
        sys.executable,
        str(wheel) + "[auth,dev,server]",
    ]
    run(install, cwd=root, env=env)
    run(install, cwd=root, env=env)  # Re-running an identical install must be harmless.
    forge = root / "bin/forge"
    installed_python = forge.resolve().with_name("python")
    location = run(
        [str(installed_python), "-c", "import scrappy_forge; print(scrappy_forge.__file__)"],
        cwd=root,
        env=env,
    ).strip()
    assert str(root / "tools") in location and str(source / "src") not in location
    version = run([str(forge), "--version"], cwd=root, env=env).strip()
    masked_setup(forge, root, env)
    project = root / "project"
    project.mkdir()
    (project / "hello.py").write_text("print('hello')\n")
    snapshot = root / "world.json"
    run(
        [str(forge), "world", "snapshot", "--repo", str(project), "--output", str(snapshot)],
        cwd=root,
        env=env,
    )
    graph = json.loads(
        run([str(forge), "world", "query", "--snapshot", str(snapshot), "--kind", "file"], cwd=root, env=env)
    )
    assert graph["total"] == 1
    run([str(forge), "account", "configure", "https://forge.example"], cwd=root, env=env)
    assert json.loads((root / "config.json").read_text())["hub_url"] == "https://forge.example"
    terminal = run([str(installed_python), str(source / "validation/terminal_smoke.py")], cwd=root, env=env)
    assert json.loads(terminal)["baseline_failed_final_passed"]
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    service_env = {k: v for k, v in env.items() if not k.startswith(("SUPABASE_", "FORGE_OWNER_"))}
    service_env.update(
        PORT=str(port), FORGE_PUBLIC_URL=origin, FORGE_RELEASE_DIR=str(wheel.parent), FORGE_SIGNUP_ENABLED="0"
    )
    log = root / "service.log"
    with log.open("w") as output:
        service = subprocess.Popen(
            [str(root / "bin/forge-hub")], cwd=root, env=service_env, stdout=output, stderr=output
        )
        try:
            with httpx.Client(base_url=origin, trust_env=False, timeout=5) as client:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        if client.get("/readyz").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() > deadline or service.poll() is not None:
                        raise AssertionError("Release service failed readiness; inspect " + str(log))
                    time.sleep(0.1)
                state = client.get("/v1/status").json()
                assert state["release_ready"] and not state["accounts_configured"]
                assert not state["remote_code_execution"]
                release = client.get("/v1/releases/latest").json()
                data = client.get("/downloads/" + release["wheel"])
                assert data.status_code == 200
                assert hashlib.sha256(data.content).hexdigest() == release["sha256"]
                installer = client.get("/install.sh").text
                assert "@@FORGE" not in installer and release["sha256"] in installer
                assert "--no-config tool install" in installer
                checked = subprocess.run(["sh", "-n"], input=installer, text=True, capture_output=True)
                assert checked.returncode == 0, checked.stderr
                for path in ("/guide", "/plans", "/world-model", "/login", "/assets/login.js"):
                    response = client.get(path)
                    assert response.status_code == 200
                    assert response.headers["x-content-type-options"] == "nosniff"
                assert client.get("/admin").status_code == 404
                assert client.post("/v1/auth/signup", json={}).status_code == 503
        finally:
            service.terminate()
            service.wait(timeout=10)
    result = {
        "version": version,
        "wheel": str(wheel),
        "installed_module": location,
        "masked_setup_and_removal": True,
        "world_file_graph": True,
        "installed_terminal_pipeline": True,
        "localhost_release_endpoints_and_checksum": True,
        "identity_provider": "disabled; no live account test",
        "model": "scripted fixture; no inference",
        "temporary_workspace": str(root),
    }
    (source / "validation/release-smoke.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
