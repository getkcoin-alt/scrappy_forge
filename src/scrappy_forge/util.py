from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


class ForgeError(RuntimeError):
    """Expected error safe to explain to a user."""


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def clean(text: str) -> str:
    """Remove terminal escape/control codes from external text before rendering."""
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", str(text))
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))


def redact(value):
    """Best effort; not a DLP guarantee. Never includes secrets supplied through env bindings."""
    if isinstance(value, dict):
        return {
            k: "[REDACTED]"
            if any(x in k.lower() for x in ("authorization", "password", "api_key", "secret", "access_token"))
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"\b(?:sk-or-v1-|sk-)[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
    return value


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".forge-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(encoded(value))
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def checked_url(url: str, *, local_allowed=False) -> str:
    p = urlsplit(url)
    if p.username or p.password or p.fragment or not p.hostname:
        raise ForgeError("URLs must not contain credentials or fragments")
    loopback = p.hostname in {"localhost", "127.0.0.1", "::1"}
    if p.scheme != "https" and not (local_allowed and loopback and p.scheme == "http"):
        raise ForgeError("HTTPS required except explicitly configured loopback HTTP")
    return url


def bound_env(names: list[str]) -> dict[str, str]:
    result = {}
    for name in names:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ForgeError("Invalid environment variable name")
        if name not in os.environ:
            raise ForgeError(f"Missing environment variable: {name}")
        result[name] = os.environ[name]
    return result


def environment_bindings(value):
    """Names only in config; resolve values just before launching a trusted process."""
    if isinstance(value, list):
        return bound_env(value)
    return {target: bound_env([source])[source] for target, source in value.items()}


def header_bindings(value):
    result = {}
    for header, binding in value.items():
        env = binding if isinstance(binding, str) else binding["env"]
        prefix = "" if isinstance(binding, str) else binding.get("prefix", "")
        resolved = prefix + bound_env([env])[env]
        if "\r" in resolved or "\n" in resolved:
            raise ForgeError("Header environment values must not contain line breaks")
        result[header] = resolved
    return result
