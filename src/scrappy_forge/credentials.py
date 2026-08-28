"""Local credentials, outside projects and never included in model state.

This is owner-only, UNENCRYPTED storage. Saving requires explicit user consent.
Environment variables or an external secret manager remain valid alternatives.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path

from .util import ForgeError, atomic_json, encoded


class PrivateStore:
    def __init__(self, state_home: Path):
        self.root = state_home.expanduser().absolute() / "credentials"
        self.path = self.root / "secrets.json"

    def _prepare(self):
        if self.root.is_symlink():
            raise ForgeError("Refusing a symlinked credentials directory")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ForgeError("Credentials directory must be owned by you with permissions 0700")

    def _read(self):
        if self.path.is_symlink():
            raise ForgeError("Refusing a symlinked credentials file")
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_nlink != 1
                or info.st_size > 65536
            ):
                raise ForgeError("Credentials must be a private, owner-only regular file (0600)")
            try:
                data = json.load(handle)
            except (ValueError, UnicodeError) as exc:
                raise ForgeError("Invalid local credentials; restore them or run forge setup") from exc
            if not isinstance(data, dict):
                raise ForgeError("Invalid local credentials format")
            return data

    def get(self, name):
        if not self.root.exists() and not self.root.is_symlink():
            return None
        self._prepare()
        return self._read().get(name)

    def set(self, name, value):
        self._prepare()
        lock_path = self.root / "write.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise ForgeError("Invalid credential lock file")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ForgeError("Another Forge process is updating credentials; retry later") from exc
            data = self._read()
            if value is None:
                data.pop(name, None)
            else:
                data[name] = value
            if len(encoded(data).encode()) > 65536:
                raise ForgeError("Credential storage exceeds its size limit")
            atomic_json(self.path, data)
            self.path.chmod(0o600)


def validate_key_shape(key):
    if (
        not isinstance(key, str)
        or not 8 <= len(key) <= 4096
        or not key.isascii()
        or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in key)
    ):
        raise ForgeError("API key must be a single nonempty printable token, not a command or URL")
    return key


def openrouter_key(settings):
    key = os.environ.get("OPENROUTER_API_KEY") or PrivateStore(settings.home).get("openrouter")
    return validate_key_shape(key) if key else None
