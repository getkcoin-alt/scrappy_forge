from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

from .util import ForgeError
from .credentials import openrouter_key


def retry_delay(value, attempt):
    if value:
        try:
            return max(0, float(value))
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                pass
    return 2**attempt


def parse_response(data):
    try:
        if data.get("error"):
            raise ValueError("API error")
        choice = data["choices"][0]
        if choice.get("finish_reason") in {"length", "error", "content_filter"}:
            raise ValueError("Incomplete response")
        m = choice["message"]
        if m["role"] != "assistant":
            raise ValueError("Invalid role")
        calls = m.get("tool_calls") or []
        if not isinstance(calls, list) or len(calls) > 8:
            raise ValueError("Too many tool calls")
        seen = set()
        for c in calls:
            if c.get("type") != "function" or not isinstance(c.get("id"), str) or c["id"] in seen:
                raise ValueError("Invalid call ID")
            seen.add(c["id"])
            if not isinstance(c["function"]["arguments"], str) or len(c["function"]["arguments"]) > 1_200_000:
                raise ValueError("Invalid arguments")
            if not isinstance(c["function"]["name"], str):
                raise ValueError("Invalid tool")
        if not calls and not isinstance(m.get("content"), str):
            raise ValueError("No answer")
        message = {k: m[k] for k in ("role", "content", "tool_calls", "reasoning_details") if k in m}
        return {"message": message, "model": data.get("model"), "usage": data.get("usage") or {}}
    except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise ForgeError("Invalid or incomplete provider response; no tools executed") from exc


class ChatProvider:
    """OpenRouter-free and localhost OpenAI-compatible adapters over requests."""

    def __init__(self, settings, *, session=None, sleep=asyncio.sleep, api_key=None):
        self.settings, self.sleep = settings, sleep
        self.http = session or requests.Session()
        self.http.trust_env = False
        self.headers = {"Content-Type": "application/json", "X-OpenRouter-Title": "Scrappy Forge"}
        if settings.provider == "openrouter":
            if settings.model != "openrouter/free" and not settings.model.endswith(":free"):
                raise ForgeError("OpenRouter is free-only")
            key = api_key or openrouter_key(settings)
            if not key:
                raise ForgeError(
                    "Run forge setup, set OPENROUTER_API_KEY locally, or configure a local model"
                )
            self.url = "https://openrouter.ai/api/v1/chat/completions"
            self.headers["Authorization"] = "Bearer " + key
        else:
            p = urlsplit(settings.endpoint)
            if (
                p.hostname not in {"127.0.0.1", "localhost", "::1"}
                or p.scheme not in {"http", "https"}
                or p.username
                or p.password
                or p.query
                or p.fragment
            ):
                raise ForgeError("Local provider endpoint must be a credential-free loopback URL")
            self.url = settings.endpoint.rstrip("/") + "/chat/completions"

    def close(self):
        self.http.close()

    async def complete(self, messages, tools, on_attempt=lambda: None):
        body = {"model": self.settings.model, "messages": messages, "max_tokens": self.settings.output_tokens}
        if tools:
            body.update(tools=tools, tool_choice="auto")
        if self.settings.provider == "openrouter":
            body["provider"] = {
                "require_parameters": True,
                "data_collection": "deny",
                "max_price": {"prompt": 0, "completion": 0, "request": 0},
            }
        for attempt in range(3):
            on_attempt()
            try:
                r = await asyncio.to_thread(
                    self.http.post,
                    self.url,
                    headers=self.headers,
                    json=body,
                    timeout=(10, 50),
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise ForgeError(
                    "Provider connection failed; ambiguous requests are not automatically replayed"
                ) from exc
            if r.status_code in {429, 502, 503, 504}:
                delay = retry_delay(r.headers.get("Retry-After"), attempt)
                if attempt == 2 or delay > 20:
                    raise ForgeError(f"Provider unavailable (HTTP {r.status_code}); resume later")
                await self.sleep(delay)
                continue
            if r.status_code != 200:
                raise ForgeError(
                    f"Provider HTTP {r.status_code}; verify model, credentials and privacy routing"
                )
            try:
                return parse_response(r.json())
            except requests.JSONDecodeError as exc:
                raise ForgeError("Provider returned invalid JSON") from exc
        raise ForgeError("Retry budget exhausted")
