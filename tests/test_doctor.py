import json
import subprocess
from types import SimpleNamespace

import pytest

from scrappy_forge import doctor
from scrappy_forge.cli import parser, run


def config(mode="docker"):
    return SimpleNamespace(execution=mode, image="python:3.12-slim", permissions="ask")


def named(checks):
    return {check["name"]: check for check in checks}


def test_python_requirement_is_explicit(monkeypatch):
    monkeypatch.setattr(doctor.sys, "version_info", (3, 10, 0))
    monkeypatch.setattr(doctor.sys, "version", "3.10.0 synthetic")
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    check = named(doctor.prerequisite_checks(config()))["python"]
    assert check["required"]
    assert check["status"] == "missing"
    assert "3.11+" in check["message"]


def test_missing_docker_and_optional_git_do_not_run_commands(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: pytest.fail("unexpected command"))
    checks = named(doctor.prerequisite_checks(config()))
    assert checks["docker_cli"]["required"]
    assert checks["docker_cli"]["status"] == "missing"
    assert checks["docker_daemon"]["status"] == "not_checked"
    assert not checks["git"]["required"]
    assert "PATH" in checks["git"]["message"]


@pytest.mark.parametrize("failure", ["exit", "timeout", "oserror"])
def test_unusable_docker_skips_image_check_and_hides_raw_errors(monkeypatch, failure):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/" + name)
    calls = []

    def invoke(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 5
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 5, stderr="private daemon details")
        if failure == "oserror":
            raise OSError("private executable details")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(doctor.subprocess, "run", invoke)
    checks = named(doctor.prerequisite_checks(config()))
    assert len(calls) == 1
    assert checks["docker_cli"]["status"] == "ok"
    assert checks["docker_daemon"]["status"] == "error"
    assert checks["docker_image"]["status"] == "not_checked"
    assert "private" not in json.dumps(checks)


@pytest.mark.parametrize("image_code", [0, 1])
def test_daemon_and_configured_image_are_separate_checks(monkeypatch, image_code):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/" + name)
    calls = []

    def invoke(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0 if len(calls) == 1 else image_code)

    monkeypatch.setattr(doctor.subprocess, "run", invoke)
    checks = named(doctor.prerequisite_checks(config()))
    assert calls == [
        ["/usr/bin/docker", "info", "--format", "{{.ServerVersion}}"],
        ["/usr/bin/docker", "image", "inspect", "--format", "{{.Id}}", "--", "python:3.12-slim"],
    ]
    assert checks["docker_daemon"]["status"] == "ok"
    assert checks["docker_image"]["status"] == ("ok" if image_code == 0 else "error")


@pytest.mark.parametrize("consent", [False, True])
def test_local_mode_never_contacts_docker_or_changes_settings(monkeypatch, consent):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: pytest.fail("unexpected Docker contact"))
    settings = config("trusted-local")
    before = vars(settings).copy()
    checks = named(doctor.prerequisite_checks(settings, allow_local_execution=consent))
    assert not checks["docker_cli"]["required"]
    assert checks["local_execution_consent"]["status"] == ("ok" if consent else "blocked")
    assert vars(settings) == before


async def test_doctor_command_emits_diagnostics_without_creating_state(tmp_path, monkeypatch, capsys):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"home": str(tmp_path / "state"), "execution": "trusted-local"}))
    monkeypatch.setattr("scrappy_forge.cli.openrouter_key", lambda _: None)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    assert await run(parser().parse_args(["doctor", "--config", str(config_file)])) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["docker_available"] is False
    assert report["live_inference_tested"] is False
    assert named(report["checks"])["local_execution_consent"]["status"] == "blocked"
    assert not (tmp_path / "state").exists()
