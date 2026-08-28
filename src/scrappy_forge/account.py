"""Optional Forge account commands. Account tokens never enter the agent loop."""

from __future__ import annotations

import argparse
import time
import uuid
from pathlib import Path

import httpx

from .config import Settings, default_config
from .credentials import PrivateStore
from .management import update_config
from .util import ForgeError, encoded, sha


class AccountClient:
    def __init__(self, settings, *, transport=None):
        self.origin = settings.hub_url.rstrip("/")
        if not self.origin:
            raise ForgeError("Run forge account configure https://YOUR-FORGE-SERVICE first")
        settings.validate()
        self.vault = PrivateStore(settings.home)
        self.binding = "account:" + sha(self.origin)
        self.http = httpx.AsyncClient(
            transport=transport, trust_env=False, timeout=20, follow_redirects=False
        )

    async def close(self):
        await self.http.aclose()

    async def request(self, method, path, data=None, token=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        try:
            async with self.http.stream(method, self.origin + path, headers=headers, json=data) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1_000_000:
                        raise ForgeError("Account response exceeded its size limit")
                if not 200 <= response.status_code < 300:
                    status = response.status_code
                    if status == 503:
                        raise ForgeError(
                            "Account service is not configured or is unavailable. Local coding still works."
                        )
                    raise ForgeError(
                        f"Account request failed (HTTP {status}); no automatic replay was performed"
                    )
                import json

                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("not an object")
                return value
        except (httpx.HTTPError, ValueError) as exc:
            raise ForgeError(
                "Account connection failed; verify the service URL and retry explicitly"
            ) from exc

    def save(self, result):
        if not all(
            isinstance(result.get(k), str) and 8 <= len(result[k]) <= 16000
            for k in ("access_token", "refresh_token")
        ):
            raise ForgeError("Account service returned an invalid session")
        seconds = result.get("expires_in")
        if type(seconds) is not int or not 1 <= seconds <= 86400:
            raise ForgeError("Account service returned an invalid session expiry")
        record = {k: result[k] for k in ("access_token", "refresh_token")}
        record.update(origin=self.origin, expires_at=time.time() + seconds)
        self.vault.set(self.binding, record)

    async def token(self):
        record = self.vault.get(self.binding)
        if not isinstance(record, dict) or record.get("origin") != self.origin:
            raise ForgeError("Not signed into this Forge service; run forge account login")
        if record.get("expires_at", 0) <= time.time() + 30:
            # No silent refresh/replay: rotating a refresh token concurrently can invalidate sessions.
            raise ForgeError("Account session expired; run forge account refresh (one terminal at a time)")
        token = record.get("access_token")
        if not isinstance(token, str) or not 8 <= len(token) <= 16000:
            raise ForgeError("Invalid saved account session; sign in again")
        return token

    async def refresh(self):
        import fcntl
        import os
        import stat

        self.vault._prepare()
        lock_path = self.vault.root / (self.binding.replace(":", "-") + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise ForgeError("Invalid account lock")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ForgeError("Another terminal is refreshing this account; retry later") from exc
            record = self.vault.get(self.binding)
            if not isinstance(record, dict) or record.get("origin") != self.origin:
                raise ForgeError("No saved account session")
            result = await self.request(
                "POST", "/v1/auth/refresh", {"refresh_token": record["refresh_token"]}
            )
            self.save(result)
            return result.get("user", {})


def parser():
    p = argparse.ArgumentParser(
        prog="forge account", description="Optional account login; never receives your model API key"
    )
    p.add_argument(
        "action",
        choices=["configure", "login", "signup", "me", "refresh", "logout", "users", "suspend", "restore"],
    )
    p.add_argument("value", nargs="?", help="Service origin for configure, account UUID for suspend/restore")
    p.add_argument("--config", type=Path, default=default_config())
    return p


async def run(args, ui):
    settings = Settings.load(args.config)
    if args.action == "configure":
        if not args.value:
            raise ForgeError("Provide your Forge service's HTTPS origin")
        settings.hub_url = args.value.rstrip("/")
        settings.validate()
        update_config(args.config, lambda data: data.update(hub_url=settings.hub_url))
        ui.say("Account service configured: " + settings.hub_url + "\nNo credentials were sent.")
        return 0
    client = AccountClient(settings)
    try:
        if args.action in {"login", "signup"}:
            if not ui.interactive:
                raise ForgeError(
                    "Account login/signup requires an interactive terminal; passwords are never CLI arguments"
                )
            ui.say(
                "Account service: " + client.origin + "\nThis is your Forge account, NOT your model API key."
            )
            status = await client.request("GET", "/v1/status")
            if not status.get("accounts_configured") or (
                args.action == "signup" and not status.get("signup_enabled")
            ):
                raise ForgeError(
                    "Account login/registration is not enabled on this service. Local BYOK coding still works."
                )
            email = (await ui.read("Account email: ")).strip()
            password = await ui.secret("Account password: ")
            result = await client.request(
                "POST", "/v1/auth/" + args.action, {"email": email, "password": password}
            )
            password = None
            if args.action == "signup":
                ui.say("Check your email to confirm your account, then run forge account login.")
                return 0
            if (
                await ui.read(
                    "Type save to keep session tokens in an unencrypted owner-only file; Enter discards them: "
                )
            ).strip() == "save":
                client.save(result)
                ui.say("Signed in. Account tokens saved locally; your model API key was not sent.")
            else:
                ui.say("Login succeeded; tokens discarded. No persistent account session was created.")
            ui.say(encoded(result.get("user", {})))
        elif args.action == "refresh":
            ui.say(encoded(await client.refresh()))
        elif args.action == "logout":
            record = client.vault.get(client.binding)
            try:
                if isinstance(record, dict):
                    await client.request("POST", "/v1/auth/logout", {}, token=record.get("access_token"))
                ui.say("Account signed out. Existing access tokens may remain valid until expiry.")
            finally:
                client.vault.set(client.binding, None)
                ui.say("Saved local account tokens removed. Model API key unchanged.")
        elif args.action == "me":
            ui.say(encoded(await client.request("GET", "/v1/me", token=await client.token())))
        elif args.action == "users":
            ui.say(encoded(await client.request("GET", "/v1/admin/users", token=await client.token())))
        else:
            try:
                target = str(uuid.UUID(args.value or ""))
            except ValueError as exc:
                raise ForgeError("Provide the account UUID, not an email or username") from exc
            if (
                not ui.interactive
                or (await ui.read(f"Type {args.action} to {args.action} account {target}: ")).strip()
                != args.action
            ):
                raise ForgeError("Account administration not approved")
            ui.say(
                encoded(
                    await client.request(
                        "PATCH",
                        "/v1/admin/users/" + target,
                        {"suspended": args.action == "suspend"},
                        token=await client.token(),
                    )
                )
            )
    finally:
        await client.close()
    return 0
