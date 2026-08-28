"""Small Supabase Auth adapter; the managed provider owns passwords and sessions.

Disabled until explicitly configured. User-controlled metadata never grants roles.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


class AuthError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


@dataclass
class AuthConfig:
    url: str = ""
    publishable_key: str = ""
    owner_subject: str = ""
    secret_key: str = ""
    signup_enabled: bool = False

    @property
    def configured(self):
        return bool(self.url and self.publishable_key)

    def validate(self):
        if self.url:
            p = urlsplit(self.url)
            if (
                p.scheme != "https"
                or not p.hostname
                or p.username
                or p.password
                or p.query
                or p.fragment
                or p.path not in {"", "/"}
            ):
                raise ValueError("SUPABASE_URL must be a credential-free HTTPS origin")
        if self.owner_subject:
            self.owner_subject = str(uuid.UUID(self.owner_subject))
        if bool(self.url) != bool(self.publishable_key):
            raise ValueError("Configure both SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY")


class RateLimit:
    """Bounded, per-process throttle. Production uses one worker until a shared limiter is added."""

    def __init__(self, maximum=30, window=60, capacity=5000):
        self.maximum, self.window, self.capacity = maximum, window, capacity
        self.entries = OrderedDict()

    def check(self, key, now=None):
        now = time.monotonic() if now is None else now
        while self.entries and next(iter(self.entries.values()))[0] + self.window <= now:
            self.entries.popitem(last=False)
        start, count = self.entries.get(key, (now, 0))
        if start + self.window <= now:
            self.entries.pop(key, None)
            start, count = now, 0
        if count >= self.maximum or (key not in self.entries and len(self.entries) >= self.capacity):
            raise AuthError(429, "Too many requests; wait a minute and try again")
        self.entries[key] = (start, count + 1)


class IdentityProvider:
    def __init__(self, config, *, transport=None):
        config.validate()
        self.config = config
        self.http = httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False, timeout=15
        )

    async def close(self):
        await self.http.aclose()

    async def request(self, method, path, *, data=None, token=None, privileged=False, params=None):
        if not self.config.configured:
            raise AuthError(503, "Account sign-in is not configured. Local BYOK coding still works.")
        key = self.config.secret_key if privileged else self.config.publishable_key
        if not key:
            raise AuthError(503, "Owner account administration is not configured")
        headers = {"apikey": key}
        if token:
            headers["Authorization"] = "Bearer " + token
        # Legacy service_role is a JWT; modern secret keys belong only in apikey.
        elif privileged and key.startswith("eyJ"):
            headers["Authorization"] = "Bearer " + key
        try:
            async with self.http.stream(
                method,
                self.config.url.rstrip("/") + "/auth/v1" + path,
                headers=headers,
                json=data,
                params=params,
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1_000_000:
                        raise AuthError(502, "Identity provider response exceeded the size limit")
                if not 200 <= response.status_code < 300:
                    status = response.status_code
                    # Never relay error bodies, email addresses, passwords or tokens into logs/errors.
                    if status == 429:
                        raise AuthError(429, "Identity provider rate limit reached; try later")
                    if status in {400, 401, 403, 422}:
                        raise AuthError(
                            401, "Authentication failed. Check credentials and email confirmation."
                        )
                    raise AuthError(502, "Identity provider is unavailable")
                if not body:
                    return {}
                import json

                value = json.loads(body)
                if not isinstance(value, dict):
                    raise ValueError("not an object")
                return value
        except AuthError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise AuthError(502, "Identity provider request failed") from exc

    async def user(self, token):
        if not token or not isinstance(token, str) or len(token) > 16000 or any(c.isspace() for c in token):
            raise AuthError(401, "A valid bearer token is required")
        user = await self.request("GET", "/user", token=token)
        try:
            subject = str(uuid.UUID(user["id"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise AuthError(401, "Identity provider returned an invalid user") from exc
        if user.get("is_anonymous") or not user.get("email_confirmed_at"):
            raise AuthError(403, "A confirmed, non-anonymous account is required")
        banned_until = user.get("banned_until")
        if banned_until is not None and banned_until != "":
            from datetime import datetime, timezone

            try:
                expiry = datetime.fromisoformat(banned_until.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    raise ValueError("Missing time zone")
                if expiry > datetime.now(timezone.utc):
                    raise AuthError(403, "Account is suspended")
            except (ValueError, TypeError, AttributeError) as exc:
                raise AuthError(401, "Invalid account status") from exc
        owner = bool(self.config.owner_subject and subject == self.config.owner_subject)
        return {
            "id": subject,
            "email": user.get("email"),
            "role": "owner" if owner else "user",
            "plan": "local_byok",
            "scopes": ["profile:read"] + (["admin:users:read", "admin:users:suspend"] if owner else []),
        }


def audit(event, **fields):
    import json

    # Only call with typed IDs/status, never request headers, bodies, keys or passwords.
    logging.getLogger("forge.audit").info(json.dumps({"event": event, **fields}, sort_keys=True))
