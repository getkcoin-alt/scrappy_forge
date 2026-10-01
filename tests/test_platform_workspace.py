from __future__ import annotations

import json
from pathlib import Path

import pytest

from scrappy_forge.platform_workspace import all_components, load_manifest, materialize, status, validate_manifest
from scrappy_forge.util import ForgeError


def test_packaged_manifest_pins_four_core_components_plus_power_extension():
    manifest = load_manifest()
    assert manifest["schema"] == "scrappy-platform.v1"
    assert manifest["protocol"] == "SYNCBOND"
    assert manifest["protocol_version"] == "5.0.0"
    assert {item["name"] for item in manifest["components"]} == {
        "scrappy-os",
        "vault-zeta",
        "scrappy-forge",
        "command-center",
    }
    assert {item["name"] for item in manifest["extensions"]} == {"power"}
    assert all(len(item["sha"]) == 40 for item in all_components(manifest))


def test_manifest_refuses_arbitrary_repository():
    manifest = load_manifest()
    manifest["components"][0]["repository"] = "git@github.com:attacker/repo.git"
    with pytest.raises(ForgeError, match="getkcoin-alt"):
        validate_manifest(manifest)


def test_manifest_refuses_floating_or_short_sha():
    manifest = load_manifest()
    manifest["extensions"][0]["sha"] = "main"
    with pytest.raises(ForgeError, match="full commit"):
        validate_manifest(manifest)


def test_manifest_refuses_extension_that_impersonates_core_component():
    manifest = load_manifest()
    manifest["extensions"][0]["name"] = "scrappy-os"
    with pytest.raises(ForgeError, match="duplicate"):
        validate_manifest(manifest)


def test_status_without_root_is_manifest_only():
    value = status()
    assert value["protocol_version"] == "5.0.0"
    assert len(value["components"]) == 5
    assert {item["tier"] for item in value["components"]} == {"core", "extension"}
    assert all("materialized" not in item for item in value["components"])


def test_materialize_checks_out_exact_pins(monkeypatch, tmp_path: Path):
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Result()

    monkeypatch.setattr("scrappy_forge.platform_workspace.subprocess.run", fake_run)
    result = materialize(tmp_path / "platform")
    assert result["status"] == "materialized"
    manifest = load_manifest()
    checkouts = [call for call in calls if "checkout" in call]
    assert len(checkouts) == 5
    assert {call[-1] for call in checkouts} == {item["sha"] for item in all_components(manifest)}
    saved = json.loads((tmp_path / "platform" / "platform-state" / "manifest.json").read_text())
    assert saved["protocol_version"] == "5.0.0"
    assert saved["extensions"][0]["name"] == "power"


def test_materialize_refuses_nonempty_component_directory(tmp_path: Path):
    root = tmp_path / "platform"
    existing = root / "scrappy-os"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("do not overwrite")
    with pytest.raises(ForgeError, match="Refusing to overwrite"):
        materialize(root)
