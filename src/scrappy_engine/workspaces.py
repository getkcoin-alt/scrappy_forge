"""Isolated mission workspaces: a git worktree on its own branch, or a snapshot copy.

The operator's checkout is never edited. Each mission gets:
  <home>/workspaces/<mission>/baseline  immutable copy used for diffs and baseline evidence
  <home>/workspaces/<mission>/work      the only tree agents may change
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from scrappy_forge.util import ForgeError
from scrappy_forge.workspace import snapshot


@dataclass(frozen=True)
class MissionWorkspace:
    root: Path
    baseline: Path
    work: Path
    kind: str  # "git-worktree" | "snapshot"
    branch: str | None = None

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "baseline": str(self.baseline),
            "work": str(self.work),
            "kind": self.kind,
            "branch": self.branch,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "MissionWorkspace":
        return cls(
            Path(raw["root"]), Path(raw["baseline"]), Path(raw["work"]), raw["kind"], raw.get("branch")
        )


def _git(project: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(project), *argv], capture_output=True, text=True, timeout=120)


def is_git_repo(project: Path) -> bool:
    if shutil.which("git") is None:
        return False
    done = _git(project, "rev-parse", "--show-toplevel")
    return done.returncode == 0 and Path(done.stdout.strip()).resolve() == project.resolve()


def prepare(home: Path, mission_id: str, project: Path) -> MissionWorkspace:
    root = (home / "workspaces" / mission_id).resolve()
    baseline, work = root / "baseline", root / "work"
    if work.exists() and baseline.exists():
        kind = "git-worktree" if (work / ".git").exists() else "snapshot"
        return MissionWorkspace(
            root, baseline, work, kind, f"scrappy/{mission_id}" if kind != "snapshot" else None
        )
    if root.exists():
        # A previous attempt crashed half-way through preparation; start clean.
        cleanup(project, MissionWorkspace(root, baseline, work, "snapshot"))
    root.mkdir(parents=True, mode=0o700)
    branch = None
    if is_git_repo(project) and _git(project, "rev-parse", "--verify", "HEAD").returncode == 0:
        branch = f"scrappy/{mission_id}"
        done = _git(project, "worktree", "add", "-b", branch, str(work), "HEAD")
        if done.returncode != 0:
            raise ForgeError("git worktree add failed: " + done.stderr[-1000:])
        kind = "git-worktree"
    else:
        snapshot(project, work)
        kind = "snapshot"
    snapshot(work, baseline)
    return MissionWorkspace(root, baseline, work, kind, branch)


def cleanup(project: Path, ws: MissionWorkspace, *, keep_branch: bool = True) -> None:
    if ws.kind == "git-worktree" and ws.work.exists():
        _git(project, "worktree", "remove", "--force", str(ws.work))
        if not keep_branch and ws.branch:
            _git(project, "branch", "-D", ws.branch)
    shutil.rmtree(ws.root, ignore_errors=True)
