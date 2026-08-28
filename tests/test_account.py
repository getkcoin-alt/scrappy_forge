import json

import httpx
import pytest

from scrappy_forge.account import AccountClient
from scrappy_forge.config import Settings
from scrappy_forge.credentials import PrivateStore
from scrappy_forge.util import ForgeError


@pytest.mark.asyncio
async def test_account_tokens_are_bound_to_service_origin(tmp_path):
    s = Settings(home=tmp_path, hub_url="https://forge.example")
    first = AccountClient(s)
    first.save({"access_token": "fixture-access", "refresh_token": "fixture-refresh", "expires_in": 3600})
    assert await first.token() == "fixture-access"
    s.hub_url = "https://other.example"
    second = AccountClient(s)
    with pytest.raises(ForgeError, match="Not signed"):
        await second.token()
    await first.close()
    await second.close()


@pytest.mark.asyncio
async def test_account_refresh_rotates_only_account_tokens(tmp_path):
    s = Settings(home=tmp_path, hub_url="https://forge.example")
    calls = []

    def handler(request):
        calls.append(request)
        assert json.loads(request.content) == {"refresh_token": "old-refresh-fixture"}
        return httpx.Response(
            200,
            json={
                "access_token": "new-access-fixture",
                "refresh_token": "new-refresh-fixture",
                "expires_in": 300,
                "user": {"role": "user"},
            },
        )

    client = AccountClient(s, transport=httpx.MockTransport(handler))
    client.save(
        {"access_token": "old-access-fixture", "refresh_token": "old-refresh-fixture", "expires_in": 1}
    )
    PrivateStore(tmp_path).set("openrouter", "model-key-not-forwarded")
    with pytest.raises(ForgeError, match="expired"):
        await client.token()
    assert (await client.refresh())["role"] == "user"
    assert await client.token() == "new-access-fixture"
    assert "model-key-not-forwarded" not in str(calls[0].headers) + calls[0].content.decode()
    assert PrivateStore(tmp_path).get("openrouter") == "model-key-not-forwarded"
    await client.close()


@pytest.mark.asyncio
async def test_account_redirect_refused_and_errors_sanitized(tmp_path):
    def handler(request):
        return httpx.Response(302, text="secret-token-fixture", headers={"Location": "https://other.example"})

    client = AccountClient(
        Settings(home=tmp_path, hub_url="https://forge.example"), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ForgeError) as error:
        await client.request("GET", "/v1/me", token="secret-token-fixture")
    assert "secret-token-fixture" not in str(error.value)
    await client.close()


@pytest.mark.asyncio
async def test_account_session_rejects_invalid_expiry_and_tokens(tmp_path):
    client = AccountClient(Settings(home=tmp_path, hub_url="https://forge.example"))
    with pytest.raises(ForgeError):
        client.save(
            {"access_token": "valid-test-token", "refresh_token": "refresh-fixture", "expires_in": -1}
        )
    assert PrivateStore(tmp_path).get(client.binding) is None
    await client.close()
