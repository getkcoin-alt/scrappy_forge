"""MCP OAuth via the official SDK, with local consent and optional encrypted storage.

No authentication codes or tokens are appended to the model transcript or journal.
The SDK minor version is pinned because the small hardening subclass uses its context API.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.utils import handle_token_response_scopes
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)

from .util import ForgeError, atomic_json, bound_env, checked_url, encoded, sha


def origin(url):
    parsed = urlsplit(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


class TokenVault:
    """Memory by default; encrypted records require a separately supplied Fernet key."""

    def __init__(self, home: Path, server: str, config: dict):
        self.binding = sha(encoded({"server": server, "url": config["url"], "oauth": config["oauth"]}))
        self.directory = home / "oauth" / sha(server)
        self.path = self.directory / (self.binding + ".json")
        self.data = {"binding": self.binding}
        self.cipher = None
        self._lock = None
        oauth = config["oauth"]
        if oauth.get("storage", "memory") == "encrypted":
            try:
                from cryptography.fernet import Fernet
            except ImportError as exc:
                raise ForgeError(
                    "Encrypted OAuth storage requires the auth extra: pip install '.[auth]'"
                ) from exc
            env_name = oauth.get("key_env", "FORGE_TOKEN_KEY")
            key = bound_env([env_name])[env_name]
            try:
                self.cipher = Fernet(key.encode())
            except (ValueError, TypeError) as exc:
                raise ForgeError(
                    "OAuth encryption key must be a valid Fernet key from your local secret manager"
                ) from exc
            self.reload()

    def reload(self):
        if self.path.exists() and self.cipher:
            from cryptography.fernet import InvalidToken

            if self.path.is_symlink() or self.path.stat().st_size > 100_000:
                raise ForgeError("Invalid OAuth vault file")
            try:
                encrypted = json.loads(self.path.read_text())["ciphertext"]
                self.data = json.loads(self.cipher.decrypt(encrypted.encode()))
            except (ValueError, KeyError, InvalidToken):
                raise ForgeError("OAuth vault could not be decrypted; check the key or sign out") from None
            if self.data.get("binding") != self.binding:
                raise ForgeError("OAuth vault binding mismatch")

    def lock(self):
        if not self.cipher or self._lock:
            return
        if self.directory.is_symlink() or self.directory.parent.is_symlink():
            raise ForgeError("Invalid OAuth vault directory")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.path.with_suffix(".lock")
        if path.is_symlink():
            raise ForgeError("Invalid OAuth vault lock")
        lock = path.open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.reload()
        except BaseException as exc:
            lock.close()
            if isinstance(exc, BlockingIOError):
                raise ForgeError(
                    "This encrypted OAuth connection is already in use by another session"
                ) from exc
            raise
        self._lock = lock

    def unlock(self):
        if self._lock:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    def save(self):
        if self.cipher:
            if self.directory.is_symlink() or self.directory.parent.is_symlink():
                raise ForgeError("OAuth vault directories must not be symlinks")
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
            atomic_json(self.path, {"ciphertext": self.cipher.encrypt(encoded(self.data).encode()).decode()})
            self.path.chmod(0o600)

    async def get_tokens(self):
        raw = self.data.get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens):
        self.data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self.data["expires_at"] = time.time() + tokens.expires_in if tokens.expires_in is not None else None
        self.save()

    async def get_client_info(self):
        raw = self.data.get("client")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info):
        self.data["client"] = client_info.model_dump(mode="json", exclude_none=True)
        self.save()

    def save_context(self, context):
        self.data["context"] = {
            "auth_server_url": context.auth_server_url,
            "oauth_metadata": context.oauth_metadata.model_dump(mode="json", exclude_none=True)
            if context.oauth_metadata
            else None,
            "protected_resource_metadata": context.protected_resource_metadata.model_dump(
                mode="json", exclude_none=True
            )
            if context.protected_resource_metadata
            else None,
        }
        self.save()

    @staticmethod
    def logout(home, server):
        directory = Path(home) / "oauth" / sha(server)
        if directory.is_symlink():
            raise ForgeError("Invalid OAuth vault directory")
        count = 0
        if directory.exists():
            paths, locks = list(directory.glob("*.json")), []
            try:
                for path in paths:
                    lock_path = path.with_suffix(".lock")
                    if lock_path.is_symlink():
                        raise ForgeError("Invalid OAuth vault lock")
                    lock = lock_path.open("a")
                    locks.append(lock)
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        raise ForgeError("Close active Forge connections before signing out") from exc
                for path in paths:
                    path.unlink()
                    count += 1
            finally:
                for lock in locks:
                    lock.close()
        return {"deleted_local_records": count, "remote_tokens_revoked": False}


class LoopbackCallback:
    def __init__(self, port=8766, timeout=300):
        self.port, self.timeout = port, timeout
        self.server = None
        self.result = None
        self.expected_state = None

    @property
    def uri(self):
        return f"http://127.0.0.1:{self.port}/oauth/callback"

    async def start(self, state):
        await self.close()
        self.result = asyncio.get_running_loop().create_future()
        self.expected_state = state
        try:
            self.server = await asyncio.start_server(
                self._handle, "127.0.0.1", self.port, limit=16384, backlog=8
            )
        except OSError as exc:
            raise ForgeError("Cannot bind the OAuth callback port; configure another loopback port") from exc

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        if self.result and not self.result.done():
            self.result.cancel()

    async def _handle(self, reader, writer):
        status, message, outcome = "400 Bad Request", "Invalid OAuth callback.", None
        try:
            async with asyncio.timeout(5):
                headers = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            lines = headers.split("\r\n")
            method, target, version = lines[0].split(" ")
            fields = {}
            for line in lines[1:]:
                if line:
                    name, value = line.split(":", 1)
                    if name.lower() in fields:
                        raise ValueError("Duplicate header")
                    fields[name.lower()] = value.strip()
            parsed = urlsplit(target)
            query = parse_qs(parsed.query, keep_blank_values=True)
            state = query.get("state", [])
            valid = (
                method == "GET"
                and version in {"HTTP/1.0", "HTTP/1.1"}
                and not parsed.scheme
                and not parsed.netloc
                and parsed.path == "/oauth/callback"
                and fields.get("host") == f"127.0.0.1:{self.port}"
                and len(state) == 1
                and bool(self.expected_state)
                and secrets.compare_digest(state[0], self.expected_state)
                and self.result is not None
                and not self.result.done()
            )
            if valid and len(query.get("error", [])) == 1:
                outcome = ForgeError("OAuth authorization was declined or failed")
                status, message = "200 OK", "Authorization was not completed. Return to the terminal."
            elif valid and len(query.get("code", [])) == 1 and 0 < len(query["code"][0]) <= 8192:
                outcome = (query["code"][0], state[0])
                status, message = "200 OK", "Authorization received. You can close this tab."
            body = message.encode()
            writer.write(
                (
                    f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                ).encode()
                + body
            )
            await writer.drain()
            if outcome is not None and not self.result.done():
                if isinstance(outcome, Exception):
                    self.result.set_exception(outcome)
                else:
                    self.result.set_result(outcome)
        except (
            ValueError,
            UnicodeError,
            OSError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
        ):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def wait(self):
        if not self.result:
            raise ForgeError("OAuth callback listener was not started")
        try:
            async with asyncio.timeout(self.timeout):
                return await self.result
        except TimeoutError as exc:
            raise ForgeError("OAuth sign-in timed out; reconnect to try again") from exc
        finally:
            await self.close()


class GuardedOAuth(OAuthClientProvider):
    def __init__(self, *args, vault, allowed_scopes=None, **kwargs):
        super().__init__(*args, storage=vault, **kwargs)
        self.vault, self.allowed_scopes = vault, allowed_scopes

    async def _initialize(self):
        await super()._initialize()
        saved = self.vault.data.get("context")
        if saved:
            self.context.auth_server_url = saved["auth_server_url"]
            self.context.oauth_metadata = (
                OAuthMetadata.model_validate(saved["oauth_metadata"]) if saved.get("oauth_metadata") else None
            )
            self.context.protected_resource_metadata = (
                ProtectedResourceMetadata.model_validate(saved["protected_resource_metadata"])
                if saved.get("protected_resource_metadata")
                else None
            )
            self.context.token_expiry_time = self.vault.data.get("expires_at")
        elif self.context.current_tokens:
            # Never refresh a saved token using a guessed or changed endpoint.
            self.context.clear_tokens()

    async def _perform_authorization_code_grant(self):
        metadata = self.context.oauth_metadata
        if not metadata or "S256" not in (metadata.code_challenge_methods_supported or []):
            raise ForgeError("Authorization server must advertise PKCE S256 support")
        requested = set((self.context.client_metadata.scope or "").split())
        if self.allowed_scopes is not None:
            allowed = set(self.allowed_scopes.split())
            if not requested <= allowed:
                raise ForgeError("Server requested scopes outside the configured OAuth scope limit")
            self.context.client_metadata.scope = self.allowed_scopes
        return await super()._perform_authorization_code_grant()

    async def _handle_token_response(self, response):
        try:
            if response.status_code != 200:
                raise ForgeError("OAuth token endpoint rejected the request")
            await super()._handle_token_response(response)
            self.vault.save_context(self.context)
        except Exception:
            # The SDK logs flow exceptions. Do not expose token-endpoint bodies in a traceback.
            raise ForgeError("OAuth token exchange failed; no credential details are logged") from None

    async def _handle_refresh_response(self, response):
        if response.status_code != 200:
            self.context.clear_tokens()
            return False
        try:
            token = await handle_token_response_scopes(response)
            if token.refresh_token is None and self.context.current_tokens:
                token.refresh_token = self.context.current_tokens.refresh_token
            self.context.current_tokens = token
            self.context.update_token_expiry(token)
            await self.vault.set_tokens(token)
            self.vault.save_context(self.context)
            return True
        except Exception:
            self.context.clear_tokens()
            return False


class OAuthConnection:
    def __init__(self, home, server, config, policy, *, announce=None, interactive=False, on_url=None):
        self.name, self.config, self.policy = server, config, policy
        self.announce, self.interactive, self.on_url = announce, interactive, on_url
        # SDK errors may contain untrusted token-endpoint response bodies.
        for name in ("mcp.client.auth.oauth2", "mcp.client.auth.utils"):
            logger = logging.getLogger(name)
            if not any(isinstance(f, SafeOAuthLog) for f in logger.filters):
                logger.addFilter(SafeOAuthLog())
        oauth = config["oauth"]
        self.vault = TokenVault(home, server, config)
        self.callback = LoopbackCallback(oauth.get("callback_port", 8766), oauth.get("timeout", 300))
        self.approved_origins = {origin(config["url"]), *self.vault.data.get("approved_origins", [])}
        metadata = OAuthClientMetadata(
            redirect_uris=[self.callback.uri],
            client_name="Scrappy Forge",
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=oauth.get("scopes"),
        )
        self.provider = GuardedOAuth(
            config["url"],
            metadata,
            vault=self.vault,
            allowed_scopes=oauth.get("scopes"),
            redirect_handler=self.redirect,
            callback_handler=self.callback.wait,
            timeout=oauth.get("timeout", 300),
            client_metadata_url=oauth.get("client_metadata_url"),
        )
        if oauth.get("client_id") and not self.vault.data.get("client"):
            self.vault.data["client"] = OAuthClientInformationFull(
                client_id=oauth["client_id"], **metadata.model_dump(mode="json")
            ).model_dump(mode="json", exclude_none=True)

    async def request_guard(self, request):
        url = checked_url(str(request.url), local_allowed=self.config.get("allow_loopback", False))
        destination = origin(url)
        if request.headers.get("Authorization", "").lower().startswith("bearer ") and destination != origin(
            self.config["url"]
        ):
            raise ForgeError("Refusing to send the MCP bearer token to another origin")
        if destination not in self.approved_origins:
            await self.policy.authorize(
                "oauth_origin_" + self.name,
                "external",
                {"origin": destination, "purpose": "OAuth discovery/token endpoint"},
                protected=True,
            )
            self.approved_origins.add(destination)
            self.vault.data["approved_origins"] = sorted(self.approved_origins)
            self.vault.save()

    async def redirect(self, url):
        checked_url(url, local_allowed=self.config.get("allow_loopback", False))
        if not self.interactive:
            raise ForgeError(
                "OAuth sign-in requires an interactive terminal; cached encrypted tokens may be used noninteractively"
            )
        query = parse_qs(urlsplit(url).query)
        if query.get("redirect_uri") != [self.callback.uri] or len(query.get("state", [])) != 1:
            raise ForgeError("Invalid OAuth redirect request")
        await self.policy.authorize(
            "oauth_login_" + self.name,
            "external",
            {
                "authorization_origin": origin(url),
                "scopes": query.get("scope", [""])[0],
                "callback": self.callback.uri,
            },
            protected=True,
        )
        await self.callback.start(query["state"][0])
        if self.announce:
            self.announce(
                "Open this sign-in link in your browser. Never paste the callback or tokens into chat:\n"
                + url
            )
        if self.on_url:
            await self.on_url(url)

    async def client(self):
        self.vault.lock()
        return httpx.AsyncClient(
            auth=self.provider,
            timeout=30,
            trust_env=False,
            follow_redirects=False,
            event_hooks={"request": [self.request_guard]},
        )

    async def close(self):
        try:
            await self.callback.close()
        finally:
            self.vault.unlock()


class SafeOAuthLog(logging.Filter):
    def filter(self, record):
        record.msg = "MCP OAuth internal event (credential details withheld)"
        record.args, record.exc_info, record.exc_text = (), None, None
        return True
