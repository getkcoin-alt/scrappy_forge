from __future__ import annotations

import json
import os
import re
import subprocess
from importlib import resources
from pathlib import Path

from .util import ForgeError

_SHA = re.compile(r"^[0-9a-f]{40}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_CORE_COMPONENTS = {"scrappy-os", "vault-zeta", "scrappy-forge", "command-center"}


def load_manifest() -> dict:
    override = os.environ.get("SCRAPPY_PLATFORM_MANIFEST")
    if override:
        path = Path(override).expanduser().resolve()
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        text = resources.files("scrappy_forge").joinpath("platform_manifest.json").read_text(encoding="utf-8")
        raw = json.loads(text)
    validate_manifest(raw)
    return raw


def _validate_component(item: dict, *, seen: set[str]) -> None:
    if not isinstance(item, dict):
        raise ForgeError("Invalid platform component")
    name = item.get("name")
    repo = item.get("repository")
    ref = item.get("ref")
    sha = item.get("sha")
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name in seen:
        raise ForgeError("Invalid or duplicate component name")
    if not isinstance(repo, str) or not repo.startswith("git@github.com:getkcoin-alt/") or not repo.endswith(".git"):
        raise ForgeError("Platform repositories must be pinned getkcoin-alt GitHub SSH remotes")
    if not isinstance(ref, str) or not ref.startswith("architecture/"):
        raise ForgeError("Platform components must use explicit architecture refs")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ForgeError("Platform component SHA must be a full commit")
    if not isinstance(item.get("role"), str) or not item["role"].strip():
        raise ForgeError("Platform component role is required")
    seen.add(name)


def all_components(raw: dict) -> list[dict]:
    return [*raw.get("components", []), *raw.get("extensions", [])]


def validate_manifest(raw: dict) -> None:
    if not isinstance(raw, dict) or raw.get("schema") != "scrappy-platform.v1":
        raise ForgeError("Unsupported Scrappy Platform manifest")
    if raw.get("protocol") != "SYNCBOND" or raw.get("protocol_version") != "5.0.0":
        raise ForgeError("Platform manifest requires SYNCBOND 5.0.0")
    components = raw.get("components")
    if not isinstance(components, list) or len(components) != 4:
        raise ForgeError("Platform manifest must define exactly four core components")
    if {item.get("name") for item in components if isinstance(item, dict)} != _CORE_COMPONENTS:
        raise ForgeError("Platform core component set is invalid")
    extensions = raw.get("extensions", [])
    if not isinstance(extensions, list):
        raise ForgeError("Platform extensions must be a list")
    seen: set[str] = set()
    for item in [*components, *extensions]:
        _validate_component(item, seen=seen)


def status(root: Path | None = None) -> dict:
    manifest = load_manifest()
    result = {
        "schema": manifest["schema"],
        "protocol_version": manifest["protocol_version"],
        "components": [],
    }
    core_names = {item["name"] for item in manifest["components"]}
    for item in all_components(manifest):
        row = {
            "name": item["name"],
            "role": item["role"],
            "ref": item["ref"],
            "sha": item["sha"],
            "tier": "core" if item["name"] in core_names else "extension",
        }
        if root is not None:
            path = root / item["name"]
            row["path"] = str(path)
            row["materialized"] = (path / ".git").exists() or (path / ".git").is_file()
            if row["materialized"]:
                proc = subprocess.run(
                    ["git", "-C", str(path), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                row["current_sha"] = proc.stdout.strip() if proc.returncode == 0 else None
                row["matches_pin"] = row["current_sha"] == item["sha"]
        result["components"].append(row)
    return result


def materialize(root: Path) -> dict:
    manifest = load_manifest()
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for item in all_components(manifest):
        path = root / item["name"]
        if path.exists() and any(path.iterdir()):
            raise ForgeError(f"Refusing to overwrite non-empty component directory: {path}")
        if not path.exists():
            path.mkdir()
        proc = subprocess.run(
            ["git", "clone", "--no-checkout", item["repository"], str(path)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if proc.returncode != 0:
            raise ForgeError(f"Clone failed for {item['name']}: {proc.stderr.strip()[:500]}")
        checkout = subprocess.run(
            ["git", "-C", str(path), "checkout", "--detach", item["sha"]],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if checkout.returncode != 0:
            raise ForgeError(f"Checkout failed for {item['name']}: {checkout.stderr.strip()[:500]}")
        results.append({"name": item["name"], "path": str(path), "sha": item["sha"]})
    state = root / "platform-state"
    state.mkdir(exist_ok=True)
    (state / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"root": str(root), "components": results, "status": "materialized"}
