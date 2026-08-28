"""Discover models from provider metadata without sending source code or credentials."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx

from .util import ForgeError


def free_tool_models(data, *, min_context=0):
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ForgeError("Invalid model catalog")
    found = []
    for model in data["data"]:
        if not isinstance(model, dict):
            continue
        name = model.get("id", "")
        if not isinstance(name, str) or not (name.endswith(":free") or name == "openrouter/free"):
            continue
        prices = model.get("pricing") or {}
        if not isinstance(prices, dict):
            continue
        try:
            if not all(
                Decimal(str(prices.get(k, "-1" if k != "request" else "0"))) == 0
                for k in ("prompt", "completion", "request")
            ):
                continue
        except (InvalidOperation, ValueError):
            continue
        supported = model.get("supported_parameters") or []
        context = model.get("context_length")
        if "tools" not in supported or type(context) is not int or context < min_context:
            continue
        found.append({"id": name, "context_tokens": context, "tools": True, "catalog_price": "0"})
    return sorted(found, key=lambda row: (-row["context_tokens"], row["id"]))


async def list_models(settings, client=None):
    if settings.provider == "openrouter":
        url = "https://openrouter.ai/api/v1/models"
    else:
        endpoint = urlsplit(settings.endpoint)
        if (
            endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
            or endpoint.scheme not in {"http", "https"}
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ForgeError("Local model discovery requires a credential-free loopback endpoint")
        url = settings.endpoint.rstrip("/") + "/models"
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False)
    try:
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code != 200:
                raise ForgeError(f"Model catalog returned HTTP {response.status_code}")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 8_000_000:
                    raise ForgeError("Model catalog exceeds 8 MB")
        data = json.loads(raw)
        if settings.provider == "openrouter":
            models = free_tool_models(data, min_context=settings.context_tokens)
        else:
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise ForgeError("Invalid local model catalog")
            models = [
                {"id": m["id"], "tools": "not verified"}
                for m in data["data"]
                if isinstance(m, dict) and isinstance(m.get("id"), str)
            ]
        return {
            "models": models,
            "catalog": url,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "note": "Catalog metadata is not a live inference test or a guarantee of provider availability.",
        }
    except (httpx.HTTPError, ValueError) as exc:
        raise ForgeError("Unable to read model catalog; no source code was sent") from exc
    finally:
        if own_client:
            await client.aclose()
