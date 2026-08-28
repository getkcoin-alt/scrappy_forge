from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from .tools import Tool
from .util import ForgeError, bound_env, checked_url, encoded, sha


def load_manifest(path: Path, kind: str):
    if path.stat().st_size > 100000:
        raise ForgeError("Extension manifest is too large")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ForgeError("Extension manifest must be an object")
    if (
        data.get("format") != 1
        or not isinstance(data.get("name"), str)
        or not re.fullmatch(r"[a-z][a-z0-9_-]{0,23}", data["name"])
    ):
        raise ForgeError(f"Invalid {kind} manifest format/name")
    if not isinstance(data.get("version"), str) or not re.fullmatch(r"\d+\.\d+\.\d+", data["version"]):
        raise ForgeError("Extension needs an explicit semantic version")
    if kind == "plugin":
        if not isinstance(data.get("tools"), list) or not 1 <= len(data["tools"]) <= 200:
            raise ForgeError("Plugin must declare 1–200 tools")
        for item in data["tools"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", item["name"])
            ):
                raise ForgeError("Invalid plugin tool name")
            if not isinstance(item.get("description", ""), str) or not isinstance(
                item.get("input_schema"), dict
            ):
                raise ForgeError("Plugin tools need a description string and JSON Schema object")
    elif not isinstance(data.get("file"), str):
        raise ForgeError("Skill needs a Markdown file path")
    return data


class Skills:
    """Versioned Markdown playbooks. Metadata is loaded first; bodies on demand."""

    def __init__(self, paths):
        self.items = {}
        for value in paths:
            p = Path(value).expanduser().resolve()
            data = load_manifest(p, "skill")
            body = (p.parent / data["file"]).resolve()
            if not body.is_relative_to(p.parent) or body.stat().st_size > 16000:
                raise ForgeError("Skill body must be a small file inside its manifest directory")
            if data["name"] in self.items:
                raise ForgeError("Duplicate skill")
            self.items[data["name"]] = {**data, "body_path": body, "sha256": sha(body.read_bytes())}

    def catalog(self):
        return [{k: v for k, v in x.items() if k not in {"body_path", "file"}} for x in self.items.values()]

    def read(self, name):
        if name not in self.items:
            raise ForgeError("Unknown skill")
        item = self.items[name]
        raw = item["body_path"].read_bytes()
        if sha(raw) != item["sha256"]:
            raise ForgeError("Skill changed after registration; restart to review the new version")
        return {
            "name": name,
            "version": item["version"],
            "sha256": item["sha256"],
            "content_untrusted": raw.decode(),
            "authority": "Guidance only; cannot override user permissions",
        }


def register_plugins(paths, registry, runner, workspace):
    loaded = []
    for value in paths:
        path = Path(value).expanduser().resolve()
        data = load_manifest(path, "plugin")
        fingerprint = sha(path.read_bytes())
        origin = f"plugin:{data['name']}@{data['version']}:{fingerprint[:12]}"
        for item in data.get("tools", []):
            name = f"plugin_{data['name']}_{item['name']}"
            kind = item.get("type")
            timeout = item.get("timeout", 60)
            if type(timeout) is not int or not 1 <= timeout <= 300:
                raise ForgeError("Plugin timeout must be 1–300 seconds")
            if kind == "command":
                argv = item.get("argv")
                if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
                    raise ForgeError("Plugin command needs argv; no shell interpolation is supported")

                async def command(args, item=item, path=path, fingerprint=fingerprint):
                    if sha(path.read_bytes()) != fingerprint:
                        raise ForgeError("Plugin changed; reload before execution")
                    result = await runner.run(
                        item["argv"],
                        workspace.root,
                        timeout=item.get("timeout", 60),
                        input_text=encoded(args),
                        env_names=item.get("env", []),
                        host=item.get("host", False),
                    )
                    if result["exit_code"] != 0 or result["timed_out"] or result["output_limited"]:
                        return {"isError": True, **result}
                    try:
                        return {"result": json.loads(result["output"]), "seconds": result["seconds"]}
                    except ValueError as exc:
                        raise ForgeError("Command plugin must write one JSON value to stdout") from exc

                handler = command
                risk = "execute"
                execution = {"argv": argv, "host": item.get("host", False), "env_names": item.get("env", [])}
            elif kind == "http":
                url = checked_url(item["url"], local_allowed=item.get("allow_loopback", False))
                method = item.get("method", "GET").upper()
                if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    raise ForgeError("Unsupported HTTP method")

                async def request(
                    args, item=item, url=url, method=method, path=path, fingerprint=fingerprint
                ):
                    if sha(path.read_bytes()) != fingerprint:
                        raise ForgeError("Plugin changed; reload before execution")
                    headers = {}
                    for header, env_name in item.get("headers_env", {}).items():
                        headers[header] = bound_env([env_name])[env_name]
                    async with httpx.AsyncClient(
                        timeout=item.get("timeout", 60), trust_env=False, follow_redirects=False
                    ) as client:
                        async with client.stream(
                            method,
                            url,
                            headers=headers,
                            params=args if method == "GET" else None,
                            json=args if method != "GET" else None,
                        ) as response:
                            if 300 <= response.status_code < 400:
                                raise ForgeError("Connector redirects are refused to protect credentials")
                            raw = bytearray()
                            async for chunk in response.aiter_bytes():
                                raw.extend(chunk)
                                if len(raw) > 200000:
                                    raise ForgeError("Connector response exceeds 200 KB")
                            return {
                                "status": response.status_code,
                                "isError": response.status_code >= 400,
                                "body": raw.decode(errors="replace"),
                            }

                handler, risk = request, "external"
                execution = {"url": url, "method": method, "headers_env": item.get("headers_env", {})}
            else:
                raise ForgeError("Plugins support command and http tools")
            registry.register(
                Tool(
                    name,
                    item.get("description", name)[:1400],
                    item["input_schema"],
                    handler,
                    risk=risk,
                    origin=origin,
                    timeout=timeout + 5,
                    execution=execution,
                )
            )
        loaded.append({"name": data["name"], "version": data["version"], "sha256": fingerprint})
    return loaded
