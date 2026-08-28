from __future__ import annotations

import difflib
import fnmatch
import os
import shutil
import stat
import tempfile
from pathlib import Path

from .util import ForgeError, encoded, sha

SKIP = {
    ".git",
    ".mcp.json",
    ".claude.json",
    ".forge",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
}


def excluded(path: Path) -> bool:
    return any(
        p in SKIP
        or p.lower().startswith(".env")
        or p in {".ssh", ".aws", ".netrc", ".npmrc"}
        or "credentials" in p.lower()
        or p.lower().endswith((".pem", ".key", ".p12", ".pfx"))
        for p in path.parts
    )


def manifest(root: Path) -> dict:
    result = {}
    total = 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not excluded(Path(parent, d).relative_to(root)))
        for d in dirs:
            if Path(parent, d).is_symlink():
                raise ForgeError(
                    "Symlink directories are unsupported; remove/exclude them before snapshotting"
                )
        for name in sorted(files):
            p = Path(parent, name)
            rel = p.relative_to(root)
            if excluded(rel):
                continue
            st = p.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise ForgeError(f"Non-regular file is not allowed: {rel}")
            total += st.st_size
            if st.st_size > 5_000_000 or total > 100_000_000 or len(result) >= 10000:
                raise ForgeError("Workspace limit exceeded: 5 MB/file, 100 MB total, 10,000 files")
            result[rel.as_posix()] = {"sha": sha(p.read_bytes()), "mode": stat.S_IMODE(st.st_mode) & 0o777}
    return result


def tree_hash(root: Path) -> str:
    return sha(encoded(manifest(root)))


def snapshot(source: Path, target: Path):
    files = manifest(source)
    target.mkdir(parents=True, exist_ok=False)
    for name, info in files.items():
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, dest)
        dest.chmod(info["mode"])


class Workspace:
    def __init__(self, root: Path, protected: list[str] | None = None):
        self.root = root.resolve()
        self.protected = protected or []

    def path(self, name: str) -> Path:
        if not isinstance(name, str) or not name or "\\" in name or "\0" in name:
            raise ForgeError("Use a relative POSIX path")
        rel = Path(name)
        if rel.is_absolute() or ".." in rel.parts or excluded(rel):
            raise ForgeError("Path is outside permitted source files")
        p = self.root / rel
        for part in (p, *p.parents):
            if part == self.root:
                break
            if part.is_symlink():
                raise ForgeError("Symlinks are not allowed")
        if not p.resolve().is_relative_to(self.root):
            raise ForgeError("Path escaped workspace")
        return p

    def is_protected(self, name: str):
        name = Path(name).as_posix()
        return any(
            fnmatch.fnmatch(name, pattern)
            or name == pattern.rstrip("/")
            or name.startswith(pattern.rstrip("/") + "/")
            for pattern in self.protected
        )

    def read(self, path: str, start: int = 1, lines: int = 160):
        p = self.path(path)
        if p.stat().st_size > 1_000_000:
            raise ForgeError("Text tool file limit is 1 MB")
        raw = p.read_bytes()
        text = raw.decode("utf-8")
        parts = text.splitlines(keepends=True)
        selected = "".join(parts[start - 1 : start - 1 + lines])
        return {
            "path": Path(path).as_posix(),
            "sha256": sha(raw),
            "start": start,
            "total_lines": len(parts),
            "content": selected[:20000],
            "truncated": len(selected) > 20000,
        }

    def write(self, path: str, content: str, expected_sha256: str | None):
        p = self.path(path)
        if not isinstance(content, str) or len(content.encode()) > 1_000_000:
            raise ForgeError("Write size exceeds 1 MB")
        actual = sha(p.read_bytes()) if p.exists() else None
        if actual != expected_sha256:
            raise ForgeError("Stale source hash; read the file again")
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(p.stat().st_mode) & 0o777 if p.exists() else 0o644
        fd, name = tempfile.mkstemp(dir=p.parent, prefix=".forge-write-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.chmod(name, mode)
            os.replace(name, p)
        finally:
            Path(name).unlink(missing_ok=True)
        return {"path": Path(path).as_posix(), "sha256": sha(content)}

    def edit(self, path: str, old: str, new: str, expected_sha256: str):
        p = self.path(path)
        text = p.read_text()
        if not old or text.count(old) != 1:
            raise ForgeError("Replacement must match exactly once")
        return self.write(path, text.replace(old, new, 1), expected_sha256)

    def search(self, query: str):
        hits = []
        for name in manifest(self.root):
            p = self.root / name
            if p.stat().st_size > 1_000_000:
                continue
            try:
                for number, line in enumerate(p.read_text().splitlines(), 1):
                    if query in line:
                        hits.append({"path": name, "line": number, "text": line[:300]})
                        if len(hits) >= 60:
                            return {"matches": hits, "truncated": True}
            except UnicodeError:
                continue
        return {"matches": hits, "truncated": False}


def diff(base: Path, work: Path) -> tuple[str, list[str]]:
    before, after = manifest(base), manifest(work)
    changed = [p for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]
    output = []
    for name in changed:
        if any(c in name for c in '\n\r\t"'):
            raise ForgeError("Patch export does not support control characters or quotes in filenames")
        output.append(f"diff --git a/{name} b/{name}\n")
        if name not in before:
            output.append(f"new file mode {0o100000 | after[name]['mode']:o}\n")
        elif name not in after:
            output.append(f"deleted file mode {0o100000 | before[name]['mode']:o}\n")
        elif before[name]["mode"] != after[name]["mode"]:
            output += [
                f"old mode {0o100000 | before[name]['mode']:o}\n",
                f"new mode {0o100000 | after[name]['mode']:o}\n",
            ]
        try:
            old = (base / name).read_text().splitlines(keepends=True) if name in before else []
            new = (work / name).read_text().splitlines(keepends=True) if name in after else []
        except UnicodeError as exc:
            raise ForgeError(
                "Binary changes require manual review; text patch export is unsupported"
            ) from exc
        for line in difflib.unified_diff(
            old,
            new,
            fromfile="a/" + name if name in before else "/dev/null",
            tofile="b/" + name if name in after else "/dev/null",
        ):
            output.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(output), changed
