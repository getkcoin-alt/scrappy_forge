"""User-triggered, hash-guarded application of verified text changes.

This is not a model tool. It never commits, pushes or deploys code.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from .util import ForgeError, sha
from .workspace import Workspace, diff, manifest, snapshot, tree_hash


def preview(engine):
    original = Path(engine.state["project"])
    current = tree_hash(engine.workspace.root)
    patch, files = diff(engine.base, engine.workspace.root)
    original_hash = tree_hash(original)
    verified = bool(
        engine.state.get("verified_hash") == current
        and engine.state.get("verification")
        and all(r.get("passed") for r in engine.state["verification"])
    )
    return {
        "project": str(original),
        "base_hash": engine.state["base_hash"],
        "candidate_hash": current,
        "original_hash": original_hash,
        "patch_sha256": sha(patch),
        "changed_files": files,
        "verified": verified,
        "already_applied": bool(files and original_hash == current),
        "original_matches_baseline": original_hash == engine.state["base_hash"],
        "patch": patch,
    }


def _atomic_copy(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".forge-apply-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as data:
            shutil.copyfileobj(data, output)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp, source.stat().st_mode & 0o777)
        os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)


async def apply_verified(engine, *, expected_patch: str | None = None):
    info = preview(engine)
    pending = engine.state.get("apply_pending")
    if pending and info["original_hash"] not in {pending["base_hash"], pending["candidate_hash"]}:
        raise ForgeError(
            f"An earlier apply was interrupted; review the backup at {pending['backup']} before continuing"
        )
    if expected_patch is not None and info["patch_sha256"] != expected_patch:
        raise ForgeError("The reviewed patch hash no longer matches")
    if not info["changed_files"]:
        raise ForgeError("There are no changes to apply")
    if not info["verified"]:
        raise ForgeError(
            "Apply requires passing configured checks for this exact workspace hash; run /verify"
        )
    if not info["original_matches_baseline"] and not info["already_applied"]:
        raise ForgeError(
            "Original project changed since this session started; apply refused without overwriting it"
        )
    await engine.policy.authorize("workspace_apply", "external", info, protected=True)
    # The user may have taken time to review the patch while either tree changed.
    if tree_hash(engine.workspace.root) != info["candidate_hash"]:
        raise ForgeError("Workspace changed during review; verify and review again")
    original = Workspace(Path(info["project"]))
    locks = engine.store.home / "project-locks"
    locks.mkdir(exist_ok=True, mode=0o700)
    with (locks / (sha(info["project"]) + ".lock")).open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ForgeError("Another Forge process is applying changes to this project") from exc
        observed = tree_hash(original.root)
        if observed == info["candidate_hash"]:
            result = {"already_applied": True, "tree_hash": observed, "patch_sha256": info["patch_sha256"]}
        else:
            if observed != info["base_hash"]:
                raise ForgeError("Original project changed during review; apply refused")
            before, after = manifest(original.root), manifest(engine.workspace.root)
            for name in after:
                if any(parent.as_posix() in before for parent in Path(name).parents if parent != Path(".")):
                    raise ForgeError("File-to-directory replacements require manual patch review")
            for name in before:
                if any(parent.as_posix() in after for parent in Path(name).parents if parent != Path(".")):
                    raise ForgeError("Directory-to-file replacements require manual patch review")
            transaction = engine.directory / "applications" / uuid.uuid4().hex[:12]
            snapshot(original.root, transaction / "backup")
            snapshot(engine.workspace.root, transaction / "candidate")
            if tree_hash(transaction / "backup") != info["base_hash"]:
                raise ForgeError("Original changed while backing up; no original files modified")
            if tree_hash(transaction / "candidate") != info["candidate_hash"]:
                raise ForgeError("Workspace changed while staging; no original files modified")
            if tree_hash(original.root) != info["base_hash"]:
                raise ForgeError("Original project changed while staging; no original files modified")
            engine.state["apply_pending"] = {
                "backup": str(transaction / "backup"),
                "base_hash": info["base_hash"],
                "candidate_hash": info["candidate_hash"],
                "patch_sha256": info["patch_sha256"],
            }
            engine.audit("apply_started", engine.state["apply_pending"])
            engine.save()
            completed = []
            try:
                for name in info["changed_files"]:
                    target = original.path(name)
                    # Recheck each path immediately before replacing it.
                    actual = (
                        {"sha": sha(target.read_bytes()), "mode": target.stat().st_mode & 0o777}
                        if target.exists()
                        else None
                    )
                    if actual != before.get(name):
                        raise ForgeError("An original file changed during application")
                    if name in after:
                        _atomic_copy(transaction / "candidate" / name, target)
                    else:
                        target.unlink()
                    completed.append(name)
                if tree_hash(original.root) != info["candidate_hash"]:
                    raise ForgeError("Final original hash did not match the reviewed candidate")
            except BaseException as exc:
                unrestored = []
                for name in reversed(completed):
                    try:
                        target = original.path(name)
                        actual = (
                            {"sha": sha(target.read_bytes()), "mode": target.stat().st_mode & 0o777}
                            if target.exists()
                            else None
                        )
                        if actual != after.get(name):
                            unrestored.append(name)
                            continue
                        if name in before:
                            _atomic_copy(transaction / "backup" / name, target)
                        else:
                            target.unlink(missing_ok=True)
                    except (OSError, ForgeError):
                        unrestored.append(name)
                engine.audit(
                    "apply_failed", {"backup": str(transaction / "backup"), "unrestored": unrestored}
                )
                if unrestored:
                    raise ForgeError(
                        f"Apply interrupted; some files need manual recovery from {transaction / 'backup'}"
                    ) from exc
                engine.state.pop("apply_pending", None)
                engine.save()
                raise ForgeError("Apply failed; completed file changes were restored from backup") from exc
            result = {
                "already_applied": False,
                "tree_hash": info["candidate_hash"],
                "patch_sha256": info["patch_sha256"],
                "changed_files": info["changed_files"],
                "backup": str(transaction / "backup"),
            }
        engine.state.pop("apply_pending", None)
        engine.state["application"] = {**result, "at": time.time()}
        engine.audit("workspace_applied", result)
        engine.save()
        engine.export()
        return result
