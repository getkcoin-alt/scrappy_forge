"""Explicit terminal commands for configuration; never invoked by the model."""

from __future__ import annotations

import argparse
import fcntl
import json
import re
from pathlib import Path

from .apply_changes import apply_verified, preview
from .auth import TokenVault
from .config import Settings, default_config, env_name, validate_mcp
from .engine import Engine
from .execution import Runner
from .extensions import Skills, load_manifest, register_plugins
from .models import list_models
from .policy import Policy, deny
from .store import Store
from .tools import Registry
from .util import ForgeError, atomic_json, encoded
from .workspace import Workspace

COMMANDS = {"mcp", "plugins", "skills", "models", "apply", "auth"}


def update_config(path, change):
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise ForgeError("Refusing to replace a symlinked configuration file")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    if lock_path.is_symlink():
        raise ForgeError("Invalid configuration lock path")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ForgeError("Another process is updating this configuration") from exc
        if path.exists() and path.stat().st_size > 1_000_000:
            raise ForgeError("Configuration exceeds 1 MB")
        data = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(data, dict):
            raise ForgeError("Configuration must be an object")
        change(data)
        if set(data) - set(Settings.__dataclass_fields__):
            raise ForgeError("Unknown configuration fields")
        candidate = Settings(**{k: Path(v).expanduser() if k == "home" else v for k, v in data.items()})
        candidate.validate()
        atomic_json(path, data)
        return data


def import_mcp(path):
    path = Path(path).expanduser().resolve()
    if path.stat().st_size > 1_000_000:
        raise ForgeError("MCP import exceeds 1 MB")
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("mcpServers"), dict):
        raise ForgeError("Import expects an object containing mcpServers")
    if len(raw["mcpServers"]) > 100:
        raise ForgeError("MCP import exceeds 100 servers")
    result = {}
    variable = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
    for name, item in raw["mcpServers"].items():
        if not isinstance(item, dict) or set(item) - {"type", "command", "args", "env", "url", "headers"}:
            raise ForgeError(f"Unsupported imported fields for {name}; review and convert them explicitly")
        transport = item.get("type", "http" if "url" in item else "stdio")
        if transport == "stdio":
            command, args = item.get("command"), item.get("args", [])
            if (
                not isinstance(command, str)
                or not isinstance(args, list)
                or any(not isinstance(v, str) or "${" in v for v in [command, *args])
            ):
                raise ForgeError(
                    "Imported commands must be literal argv; plugin-root/template expansion is not supported"
                )
            env = {}
            if not isinstance(item.get("env", {}), dict):
                raise ForgeError("Imported env must be an object")
            for target, value in item.get("env", {}).items():
                match = variable.fullmatch(value) if isinstance(value, str) else None
                if not env_name(target) or not match:
                    raise ForgeError(
                        "Imported env values must be ${VARIABLE} references; literal credentials are refused"
                    )
                env[target] = match.group(1)
            config = {"transport": "stdio", "command": command, "args": args, "env": env}
        elif transport == "http":
            if not isinstance(item.get("headers", {}), dict):
                raise ForgeError("Imported headers must be an object")
            headers = {}
            for header, value in item.get("headers", {}).items():
                match = (
                    re.fullmatch(r"(Bearer |Basic )?\$\{([A-Z][A-Z0-9_]*)\}", value)
                    if isinstance(value, str)
                    else None
                )
                if not match:
                    raise ForgeError(
                        "Imported headers must use ${VARIABLE}, Bearer ${VARIABLE} or Basic ${VARIABLE}"
                    )
                headers[header] = {"env": match.group(2), "prefix": match.group(1) or ""}
            config = {"transport": "http", "url": item.get("url"), "headers_env": headers}
        else:
            raise ForgeError(
                "Import supports stdio and Streamable HTTP; SSE/WebSocket/native plugin bundles need adapters"
            )
        validate_mcp(name, config)
        result[name] = config
    return result


def extension_inventory(paths, kind):
    items = []
    for value in paths:
        path = Path(value).expanduser().resolve()
        data = load_manifest(path, kind)
        items.append({"name": data["name"], "version": data["version"], "path": str(path)})
    return items


def parser(command):
    p = argparse.ArgumentParser(prog="forge " + command)
    p.add_argument("--config", type=Path, default=default_config())
    if command == "mcp":
        p.add_argument("action", choices=["list", "add", "remove", "import"])
        p.add_argument("name", nargs="?")
        p.add_argument("--url")
        p.add_argument("--command")
        p.add_argument("--args", default="[]", help="JSON argv array for a stdio server")
        p.add_argument(
            "--env", action="append", default=[], help="Environment variable name to pass to stdio"
        )
        p.add_argument(
            "--header-env",
            action="append",
            default=[],
            help="Header=ENVIRONMENT_VARIABLE; value is resolved only when connecting",
        )
        p.add_argument("--allow-loopback", action="store_true")
        p.add_argument("--oauth", action="store_true")
        p.add_argument("--oauth-storage", choices=["memory", "encrypted"], default="memory")
        p.add_argument("--callback-port", type=int, default=8766)
        p.add_argument("--scopes")
        p.add_argument("--client-id")
        p.add_argument("--replace", action="store_true")
    elif command in {"plugins", "skills"}:
        p.add_argument("action", choices=["list", "add", "remove"])
        p.add_argument("name", nargs="?", help="Manifest path for add, registered name for remove")
    elif command == "auth":
        p.add_argument("action", choices=["status", "logout"])
        p.add_argument("name")
    elif command == "models":
        p.add_argument("--provider", choices=["openrouter", "local"])
    elif command == "apply":
        p.add_argument("session")
        p.add_argument("--preview", action="store_true")
        p.add_argument(
            "--expected-patch",
            help="Explicitly approve only this reviewed SHA256 patch in a noninteractive run",
        )
    return p


async def run(command, args, ui):
    settings = Settings.load(args.config)
    if command == "models":
        if args.provider:
            settings.provider = args.provider
        ui.say(encoded(await list_models(settings)))
        return 0
    if command == "auth":
        if args.name not in settings.mcp:
            raise ForgeError("Unknown MCP server")
        config = settings.mcp[args.name]
        if args.action == "logout":
            ui.say(encoded(TokenVault.logout(settings.home, args.name)))
            ui.say(
                "Close existing Forge connections too. This removes local credentials, not the provider's authorization grant."
            )
        else:
            ui.say(
                encoded(
                    {
                        "server": args.name,
                        "oauth": "oauth" in config,
                        "storage": config.get("oauth", {}).get("storage", "memory"),
                        "live_authentication_checked": False,
                    }
                )
            )
        return 0
    if command == "mcp":
        if args.action == "list":
            ui.say(encoded(settings.mcp))
            return 0
        if not args.name:
            raise ForgeError("Supply a server name or import path")
        if args.action == "remove":

            def remove(data):
                if args.name not in data.get("mcp", {}):
                    raise ForgeError("Unknown MCP server")
                del data["mcp"][args.name]

            update_config(args.config, remove)
            ui.say("Removed connection configuration. Existing sessions and provider grants are unchanged.")
            return 0
        if args.action == "import":
            additions = import_mcp(args.name)
        else:
            if bool(args.url) == bool(args.command):
                raise ForgeError("Choose exactly one of --url or --command")
            if args.url:
                headers = {}
                for value in args.header_env:
                    if "=" not in value:
                        raise ForgeError("Use --header-env Header=VARIABLE_NAME")
                    header, variable = value.split("=", 1)
                    headers[header] = variable
                c = {
                    "transport": "http",
                    "url": args.url,
                    "headers_env": headers,
                    "allow_loopback": args.allow_loopback,
                }
                if args.oauth:
                    c["oauth"] = {"storage": args.oauth_storage, "callback_port": args.callback_port}
                    if args.scopes is not None:
                        c["oauth"]["scopes"] = args.scopes
                    if args.client_id:
                        c["oauth"]["client_id"] = args.client_id
            else:
                if args.oauth or args.header_env:
                    raise ForgeError("OAuth/headers require an HTTP server")
                c = {
                    "transport": "stdio",
                    "command": args.command,
                    "args": json.loads(args.args),
                    "env": args.env,
                }
            validate_mcp(args.name, c)
            additions = {args.name: c}

        def add(data):
            existing = data.setdefault("mcp", {})
            conflicts = set(existing) & set(additions)
            if conflicts and not args.replace:
                raise ForgeError(
                    "Server names already exist; use --replace only after reviewing the new configuration"
                )
            existing.update(additions)

        update_config(args.config, add)
        ui.say(
            encoded(
                {
                    "configured": additions,
                    "connected": False,
                    "note": "Restart Forge, then /mcp connect NAME. Connection still requires approval.",
                }
            )
        )
        return 0
    if command in {"plugins", "skills"}:
        paths = getattr(settings, command)
        kind = "plugin" if command == "plugins" else "skill"
        current = extension_inventory(paths, kind)
        if args.action == "list":
            ui.say(encoded(current))
            return 0
        if not args.name:
            raise ForgeError("Supply a manifest path or registered name")
        if args.action == "add":
            path = Path(args.name).expanduser().resolve()
            data = load_manifest(path, kind)
            if command == "skills":
                Skills([str(path)])
            else:
                register_plugins(
                    [str(path)],
                    Registry(Policy("plan", set(), deny, lambda *_: None)),
                    Runner(),
                    Workspace(Path.cwd()),
                )
            if any(item["name"] == data["name"] for item in current):
                raise ForgeError("Extension name already exists; remove it before registering a replacement")
            updated = [*paths, str(path)]
        else:
            target = next((item for item in current if item["name"] == args.name), None)
            if not target:
                raise ForgeError("Unknown extension name")
            updated = [value for value in paths if Path(value).expanduser().resolve() != Path(target["path"])]
        update_config(args.config, lambda data: data.update({command: updated}))
        ui.say(
            "Extension configuration updated. Restart Forge to load the reviewed manifests. No extension code was executed."
        )
        return 0
    if command == "apply":
        store = Store(settings.home)
        try:
            state = store.load(args.session)

            async def approve(request):
                if args.expected_patch and request["tool"] == "workspace_apply":
                    return request["arguments"]["patch_sha256"] == args.expected_patch
                return await ui.approve(request)

            async with Engine(store, settings, state, approve=approve) as engine:
                if args.preview:
                    ui.say(encoded(preview(engine)))
                else:
                    ui.say(encoded(await apply_verified(engine, expected_patch=args.expected_patch)))
        finally:
            store.close()
        return 0
    raise ForgeError("Unknown management command")
