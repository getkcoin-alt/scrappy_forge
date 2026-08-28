from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .util import ForgeError, checked_url


def default_config():
    return Path(os.environ.get("SCRAPPY_FORGE_CONFIG", Path.home() / ".config/scrappy-forge/config.json"))


def env_name(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Z][A-Z0-9_]*", value))


def validate_mcp(name, config):
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", name)
        or not isinstance(config, dict)
    ):
        raise ForgeError("MCP needs a 1–24 character server name and an object configuration")
    if set(config) - {"transport", "url", "command", "args", "env", "headers_env", "allow_loopback", "oauth"}:
        raise ForgeError("Unknown MCP configuration fields")
    transport = config.get("transport", "stdio")
    if type(config.get("allow_loopback", False)) is not bool:
        raise ForgeError("allow_loopback must be a boolean")
    if transport == "stdio":
        command, args = config.get("command"), config.get("args", [])
        if (
            not isinstance(command, str)
            or not command
            or "\0" in command
            or not isinstance(args, list)
            or len(args) > 128
            or not all(isinstance(a, str) and "\0" not in a and len(a) <= 16000 for a in args)
        ):
            raise ForgeError("MCP stdio needs a command and a bounded string argument array")
        if "oauth" in config or "headers_env" in config or "url" in config:
            raise ForgeError("OAuth, headers and URL are HTTP MCP options")
        env = config.get("env", [])
        if isinstance(env, list):
            valid = all(env_name(v) for v in env)
        elif isinstance(env, dict):
            valid = all(env_name(k) and env_name(v) for k, v in env.items())
        else:
            valid = False
        if not valid:
            raise ForgeError("MCP environment bindings must contain variable names, never values")
    elif transport == "http":
        if not isinstance(config.get("url"), str):
            raise ForgeError("HTTP MCP needs a URL")
        checked_url(config["url"], local_allowed=config.get("allow_loopback", False))
        if any(k in config for k in ("command", "args", "env")):
            raise ForgeError("Command/args/env are stdio MCP options")
        headers = config.get("headers_env", {})
        if not isinstance(headers, dict):
            raise ForgeError("headers_env must be an object")
        for header, value in headers.items():
            if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", header):
                raise ForgeError("Invalid HTTP header name")
            if isinstance(value, str):
                valid = env_name(value)
            elif isinstance(value, dict):
                valid = (
                    not (set(value) - {"env", "prefix"})
                    and env_name(value.get("env"))
                    and isinstance(value.get("prefix", ""), str)
                    and len(value.get("prefix", "")) <= 30
                    and not any(c in value.get("prefix", "") for c in "\r\n")
                )
            else:
                valid = False
            if not valid:
                raise ForgeError("Header bindings need an environment variable name and optional prefix")
        if "oauth" in config:
            oauth = config["oauth"]
            if not isinstance(oauth, dict) or set(oauth) - {
                "storage",
                "key_env",
                "callback_port",
                "timeout",
                "scopes",
                "client_id",
                "client_metadata_url",
            }:
                raise ForgeError("Invalid OAuth configuration fields")
            if any(h.lower() == "authorization" for h in headers):
                raise ForgeError("Choose OAuth or an Authorization header binding, not both")
            if not isinstance(oauth.get("storage", "memory"), str) or oauth.get("storage", "memory") not in {
                "memory",
                "encrypted",
            }:
                raise ForgeError("OAuth storage must be memory or encrypted")
            if not env_name(oauth.get("key_env", "FORGE_TOKEN_KEY")):
                raise ForgeError("OAuth key_env must name a local environment variable")
            for field, low, high, default in (
                ("callback_port", 1024, 65535, 8766),
                ("timeout", 30, 300, 300),
            ):
                value = oauth.get(field, default)
                if type(value) is not int or not low <= value <= high:
                    raise ForgeError(f"OAuth {field} must be between {low} and {high}")
            for field in ("scopes", "client_id", "client_metadata_url"):
                if field in oauth and (
                    not isinstance(oauth[field], str) or len(oauth[field]) > 2000 or "\n" in oauth[field]
                ):
                    raise ForgeError(f"OAuth {field} must be a bounded string")
            if oauth.get("client_metadata_url"):
                checked_url(oauth["client_metadata_url"])
    else:
        raise ForgeError("Supported MCP transports: stdio and http (Streamable HTTP)")


@dataclass
class Settings:
    home: Path = field(
        default_factory=lambda: Path(
            os.environ.get("SCRAPPY_FORGE_HOME", Path.home() / ".local/share/scrappy-forge")
        )
    )
    provider: str = "openrouter"
    model: str = "openrouter/free"
    endpoint: str = "http://127.0.0.1:11434/v1"
    hub_url: str = ""
    execution: str = "docker"
    image: str = "python:3.12-slim"
    context_tokens: int = 16000
    output_tokens: int = 2048
    max_steps: int = 30
    max_tool_seconds: int = 60
    permissions: str = "ask"
    allow_tools: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    mcp: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    protected: list[str] = field(
        default_factory=lambda: ["tests/*", "test_*", "**/test_*", "*lock*", ".github/*"]
    )

    @classmethod
    def load(cls, path: Path | None = None):
        settings = cls()
        if path is None:
            path = default_config()
        if path.exists():
            if path.stat().st_size > 1_000_000:
                raise ForgeError("Configuration exceeds 1 MB")
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ForgeError("Configuration must be a JSON object")
            unknown = set(data) - set(cls.__dataclass_fields__)
            if unknown:
                raise ForgeError(f"Unknown config keys: {', '.join(sorted(unknown))}")
            for k, v in data.items():
                setattr(settings, k, Path(v).expanduser() if k == "home" else v)
        settings.validate()
        return settings

    def validate(self):
        if not isinstance(self.hub_url, str):
            raise ForgeError("hub_url must be a string")
        if self.hub_url:
            from urllib.parse import urlsplit

            checked_url(self.hub_url, local_allowed=True)
            parts = urlsplit(self.hub_url)
            if parts.path not in {"", "/"} or parts.query:
                raise ForgeError("hub_url must be an origin without path or query")
        for key in ("provider", "model", "endpoint", "execution", "image", "permissions"):
            if not isinstance(getattr(self, key), str) or not getattr(self, key).strip():
                raise ForgeError(f"{key} must be a nonempty string")
        if self.provider not in {"openrouter", "local"}:
            raise ForgeError("Provider must be openrouter or local")
        if (
            self.provider == "openrouter"
            and self.model != "openrouter/free"
            and not self.model.endswith(":free")
        ):
            raise ForgeError("OpenRouter is free-only; use an available :free model")
        if self.execution not in {"docker", "trusted-local"}:
            raise ForgeError("Unknown execution mode")
        if self.permissions not in {"ask", "accept-edits", "plan"}:
            raise ForgeError("Unknown permission mode")
        for key, low, high in [
            ("context_tokens", 2000, 200000),
            ("output_tokens", 128, 8192),
            ("max_steps", 1, 200),
            ("max_tool_seconds", 1, 300),
        ]:
            v = getattr(self, key)
            if type(v) is not int or not low <= v <= high:
                raise ForgeError(f"{key} must be {low}–{high}")
        if self.output_tokens >= self.context_tokens // 2:
            raise ForgeError("Reserve at least half the context for input")
        for key in ("allow_tools", "plugins", "skills", "protected"):
            if not isinstance(getattr(self, key), list) or not all(
                isinstance(v, str) for v in getattr(self, key)
            ):
                raise ForgeError(f"{key} must be a string list")
        if not isinstance(self.mcp, dict) or not isinstance(self.checks, list):
            raise ForgeError("mcp must be an object and checks a list")
        if len(self.mcp) > 100:
            raise ForgeError("At most 100 MCP servers may be configured")
        for name, config in self.mcp.items():
            validate_mcp(name, config)
        names = set()
        for check in self.checks:
            if (
                not isinstance(check, dict)
                or not isinstance(check.get("name"), str)
                or not check["name"].strip()
                or len(check["name"]) > 80
                or check["name"] in names
            ):
                raise ForgeError("Checks need unique names")
            names.add(check["name"])
            if (
                not isinstance(check.get("argv"), list)
                or not check["argv"]
                or not all(isinstance(v, str) and "\0" not in v for v in check["argv"])
                or not check["argv"][0]
            ):
                raise ForgeError("Check argv must be a nonempty string array")
            timeout = check.get("timeout", 60)
            if type(timeout) is not int or not 1 <= timeout <= 300:
                raise ForgeError("Check timeout must be 1–300 seconds")
