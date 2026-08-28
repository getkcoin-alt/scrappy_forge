import asyncio
import base64
import hashlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from mcp.shared.auth import OAuthMetadata, OAuthToken

from scrappy_forge.auth import LoopbackCallback, OAuthConnection, TokenVault
from scrappy_forge.engine import Engine, create_session
from scrappy_forge.mcp_client import tool_name
from scrappy_forge.policy import Policy
from scrappy_forge.util import ForgeError


async def yes(_):
    return True


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def oauth_server():
    """Real HTTP OAuth issuer + MCP resource, with only generated fixture credentials."""
    records = {"authorizations": 0, "refreshes": 0, "token": "fixture-access-1"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, data=None, headers=None):
            body = json.dumps(data).encode() if data is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlsplit(self.path)
            base = records["url"]
            if parsed.path == "/.well-known/oauth-protected-resource":
                self.reply(
                    200,
                    {
                        "resource": base + "/mcp",
                        "authorization_servers": [base],
                        "scopes_supported": ["read"],
                    },
                )
            elif parsed.path == "/.well-known/oauth-authorization-server":
                self.reply(
                    200,
                    {
                        "issuer": base,
                        "authorization_endpoint": base + "/authorize",
                        "token_endpoint": base + "/token",
                        "registration_endpoint": base + "/register",
                        "response_types_supported": ["code"],
                        "grant_types_supported": ["authorization_code", "refresh_token"],
                        "token_endpoint_auth_methods_supported": ["none"],
                        "scopes_supported": ["read"],
                        "code_challenge_methods_supported": ["S256"],
                    },
                )
            elif parsed.path == "/authorize":
                query = parse_qs(parsed.query)
                records["authorization"] = query
                records["authorizations"] += 1
                self.reply(
                    302,
                    headers={
                        "Location": query["redirect_uri"][0]
                        + "?"
                        + urlencode({"code": "fixture-code", "state": query["state"][0]})
                    },
                )
            else:
                self.reply(405)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            base = records["url"]
            if self.path == "/register":
                metadata = json.loads(raw)
                records["client"] = metadata
                self.reply(201, {**metadata, "client_id": "fixture-client"})
                return
            if self.path == "/token":
                form = parse_qs(raw.decode())
                records["last_token_request"] = form
                if form.get("resource") != [base + "/mcp"]:
                    self.reply(400, {"error": "wrong_resource"})
                    return
                if form.get("grant_type") == ["authorization_code"]:
                    challenge = (
                        base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest())
                        .rstrip(b"=")
                        .decode()
                    )
                    if form.get("code") != ["fixture-code"] or records["authorization"].get(
                        "code_challenge"
                    ) != [challenge]:
                        self.reply(400, {"error": "bad_pkce"})
                        return
                elif form.get("grant_type") == ["refresh_token"] and form.get("refresh_token") == [
                    "fixture-refresh-1"
                ]:
                    records["refreshes"] += 1
                    records["token"] = "fixture-access-2"
                else:
                    self.reply(400, {"error": "bad_grant"})
                    return
                self.reply(
                    200,
                    {
                        "access_token": records["token"],
                        "token_type": "Bearer",
                        "refresh_token": "fixture-refresh-2" if records["refreshes"] else "fixture-refresh-1",
                        "expires_in": 3600,
                        "scope": "read",
                    },
                )
                return
            if self.path != "/mcp":
                self.reply(404)
                return
            if self.headers.get("Authorization") != "Bearer " + records["token"]:
                self.reply(
                    401,
                    headers={
                        "WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
                    },
                )
                return
            request = json.loads(raw)
            if "id" not in request:
                self.reply(202)
                return
            method = request["method"]
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "OAuth fixture", "version": "1.0"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo after authentication",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"value": {"type": "string"}},
                                "required": ["value"],
                            },
                        }
                    ]
                }
            elif method == "tools/call":
                result = {"content": [{"type": "text", "text": request["params"]["arguments"]["value"]}]}
            else:
                result = {}
            self.reply(200, {"jsonrpc": "2.0", "id": request["id"], "result": result})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    records["url"] = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield records
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


async def test_oauth_mcp_real_http_pkce_encrypted_resume_and_refresh(
    store, settings, repo, oauth_server, monkeypatch
):
    monkeypatch.setenv("FORGE_TEST_VAULT_KEY", Fernet.generate_key().decode())
    config = {
        "transport": "http",
        "url": oauth_server["url"] + "/mcp",
        "allow_loopback": True,
        "oauth": {
            "callback_port": free_port(),
            "storage": "encrypted",
            "key_env": "FORGE_TEST_VAULT_KEY",
            "scopes": "read",
        },
    }
    settings.mcp = {"authenticated": config}
    settings.validate()

    async def browser_fixture(url):
        # Simulates the browser's consent redirect only. No real account or browser UI is claimed.
        async with httpx.AsyncClient(follow_redirects=True, trust_env=False, timeout=5) as client:
            response = await client.get(url)
            assert response.status_code == 200

    async with Engine(
        store,
        settings,
        create_session(store, settings, repo),
        approve=yes,
        oauth_interactive=True,
        oauth_on_url=browser_fixture,
    ) as engine:
        async with asyncio.timeout(15):
            assert (await engine.mcp.connect("authenticated"))["connected"]
            result = await engine.registry.call(
                tool_name("authenticated", "echo"), {"value": "authenticated call"}
            )
            assert "authenticated call" in json.dumps(result)
        assert oauth_server["authorizations"] == 1
        assert oauth_server["authorization"]["code_challenge_method"] == ["S256"]
        assert oauth_server["authorization"]["resource"] == [config["url"]]
    vault = TokenVault(settings.home, "authenticated", config)
    assert "fixture-access-1" not in vault.path.read_text()
    assert vault.path.stat().st_mode & 0o777 == 0o600
    vault.data["expires_at"] = time.time() - 1
    vault.save()
    # A fresh process-equivalent connection loads expiry and issuer metadata, then refreshes.
    async with Engine(store, settings, create_session(store, settings, repo), approve=yes) as engine:
        async with asyncio.timeout(15):
            assert (await engine.mcp.connect("authenticated"))["connected"]
            assert "after refresh" in json.dumps(
                await engine.registry.call(tool_name("authenticated", "echo"), {"value": "after refresh"})
            )
    assert oauth_server["authorizations"] == 1
    assert oauth_server["refreshes"] == 1
    assert (
        await TokenVault(settings.home, "authenticated", config).get_tokens()
    ).refresh_token == "fixture-refresh-2"


async def test_callback_rejects_wrong_state_host_and_replay():
    callback = LoopbackCallback(free_port(), timeout=2)
    await callback.start("expected-state")
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            bad = await client.get(callback.uri, params={"code": "fixture-code", "state": "wrong"})
            assert bad.status_code == 400 and not callback.result.done()
            bad_host = await client.get(
                callback.uri,
                params={"code": "fixture-code", "state": "expected-state"},
                headers={"Host": "attacker.example"},
            )
            assert bad_host.status_code == 400 and not callback.result.done()
            good = await client.get(callback.uri, params={"code": "fixture-code", "state": "expected-state"})
            assert good.status_code == 200 and "fixture-code" not in good.text
            replay = await client.get(
                callback.uri, params={"code": "fixture-code", "state": "expected-state"}
            )
            assert replay.status_code == 400
        assert await callback.wait() == ("fixture-code", "expected-state")
    finally:
        await callback.close()


async def test_oauth_memory_does_not_write_tokens(tmp_path):
    config = {"url": "https://example.com/mcp", "oauth": {}}
    vault = TokenVault(tmp_path, "sample", config)
    await vault.set_tokens(OAuthToken(access_token="fixture-token", token_type="Bearer", expires_in=60))
    assert not vault.path.exists()
    assert (await vault.get_tokens()).access_token == "fixture-token"


async def test_oauth_wrong_encryption_key_fails_closed(tmp_path, monkeypatch):
    config = {"url": "https://example.com/mcp", "oauth": {"storage": "encrypted", "key_env": "FIXTURE_KEY"}}
    monkeypatch.setenv("FIXTURE_KEY", Fernet.generate_key().decode())
    vault = TokenVault(tmp_path, "sample", config)
    await vault.set_tokens(OAuthToken(access_token="fixture-token", token_type="Bearer", expires_in=60))
    monkeypatch.setenv("FIXTURE_KEY", Fernet.generate_key().decode())
    with pytest.raises(ForgeError, match="could not be decrypted"):
        TokenVault(tmp_path, "sample", config)


async def test_oauth_pkce_and_scope_limits(tmp_path):
    config = {"url": "https://example.com/mcp", "oauth": {"scopes": "read"}}
    connection = OAuthConnection(tmp_path, "sample", config, Policy("ask", set(), yes, lambda *_: None))
    with pytest.raises(ForgeError, match="PKCE S256"):
        await connection.provider._perform_authorization_code_grant()
    connection.provider.context.oauth_metadata = OAuthMetadata(
        issuer="https://auth.example.com",
        authorization_endpoint="https://auth.example.com/authorize",
        token_endpoint="https://auth.example.com/token",
        response_types_supported=["code"],
        code_challenge_methods_supported=["S256"],
    )
    connection.provider.context.client_metadata.scope = "read write"
    with pytest.raises(ForgeError, match="scope limit"):
        await connection.provider._perform_authorization_code_grant()


async def test_oauth_does_not_forward_bearer_to_another_origin(tmp_path):
    config = {"url": "https://example.com/mcp", "oauth": {}}
    connection = OAuthConnection(tmp_path, "sample", config, Policy("ask", set(), yes, lambda *_: None))
    request = httpx.Request(
        "GET", "https://other.example/metadata", headers={"Authorization": "Bearer fixture-token"}
    )
    with pytest.raises(ForgeError, match="another origin"):
        await connection.request_guard(request)


async def test_encrypted_cache_lock_prevents_concurrent_refresh_and_logout(tmp_path, monkeypatch):
    config = {"url": "https://example.com/mcp", "oauth": {"storage": "encrypted", "key_env": "FIXTURE_KEY"}}
    monkeypatch.setenv("FIXTURE_KEY", Fernet.generate_key().decode())
    first = TokenVault(tmp_path, "sample", config)
    await first.set_tokens(OAuthToken(access_token="fixture-token", token_type="Bearer", expires_in=60))
    first.lock()
    try:
        second = TokenVault(tmp_path, "sample", config)
        with pytest.raises(ForgeError, match="already in use"):
            second.lock()
        with pytest.raises(ForgeError, match="Close active"):
            TokenVault.logout(tmp_path, "sample")
    finally:
        first.unlock()
    assert TokenVault.logout(tmp_path, "sample")["deleted_local_records"] == 1
    assert not first.path.exists()


async def test_callback_timeout_closes_listener():
    callback = LoopbackCallback(free_port(), timeout=0.02)
    await callback.start("expected")
    with pytest.raises(ForgeError, match="timed out"):
        await callback.wait()
    assert callback.server is None


async def test_oauth_token_errors_do_not_expose_response_body(tmp_path):
    config = {"url": "https://example.com/mcp", "oauth": {}}
    connection = OAuthConnection(tmp_path, "sample", config, Policy("ask", set(), yes, lambda *_: None))
    with pytest.raises(ForgeError) as error:
        await connection.provider._handle_token_response(
            httpx.Response(400, json={"access_token": "fixture-sensitive-value"})
        )
    assert "fixture-sensitive-value" not in str(error.value)
