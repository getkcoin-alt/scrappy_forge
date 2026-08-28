from unittest.mock import patch

import pytest
import requests
import httpx

from scrappy_forge.config import Settings
from scrappy_forge.providers import ChatProvider, parse_response
from scrappy_forge.models import free_tool_models, list_models
from scrappy_forge.util import ForgeError


class Response:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self.data, self.headers = (
            status,
            data or {"model": "free", "choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            headers or {},
        )

    def json(self):
        return self.data


class HTTP:
    def __init__(self, values):
        self.values, self.calls, self.trust_env = list(values), [], True

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.values.pop(0)

    def close(self):
        pass


async def test_free_provider_guards(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only")
    http = HTTP([Response()])
    p = ChatProvider(Settings(home=tmp_path), session=http)
    await p.complete([], [])
    call = http.calls[0][1]
    assert call["json"]["provider"]["max_price"] == {"prompt": 0, "completion": 0, "request": 0}
    assert call["json"]["provider"]["data_collection"] == "deny"
    assert not call["allow_redirects"] and not http.trust_env


def test_paid_model_refused(tmp_path):
    s = Settings(home=tmp_path, model="vendor/paid")
    with pytest.raises(ForgeError):
        s.validate()


def test_local_endpoint_is_loopback(tmp_path):
    with pytest.raises(ForgeError):
        ChatProvider(
            Settings(home=tmp_path, provider="local", model="local", endpoint="https://remote.example/v1")
        )


async def test_rate_retry_bounded(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only")
    http, sleeps = HTTP([Response(429, headers={"Retry-After": "2"}), Response()]), []

    async def sleep(seconds):
        sleeps.append(seconds)

    p = ChatProvider(Settings(home=tmp_path), session=http, sleep=sleep)
    await p.complete([], [])
    assert sleeps == [2] and len(http.calls) == 2


async def test_ambiguous_timeout_not_replayed(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only")
    http = HTTP([])
    with patch.object(http, "post", side_effect=requests.Timeout) as post:
        with pytest.raises(ForgeError):
            await ChatProvider(Settings(home=tmp_path), session=http).complete([], [])
    assert post.call_count == 1


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"choices": []},
        {"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "x"}}]},
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": "x", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                            {"id": "x", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                        ],
                    }
                }
            ]
        },
    ],
)
def test_malformed_responses(data):
    with pytest.raises(ForgeError):
        parse_response(data)


def test_model_catalog_filters_price_tools_and_context():
    base = {
        "id": "example/model:free",
        "pricing": {"prompt": "0", "completion": "0"},
        "supported_parameters": ["tools"],
        "context_length": 32000,
    }
    data = {
        "data": [
            base,
            {**base, "id": "example/paid"},
            {**base, "id": "example/expensive:free", "pricing": {"prompt": "0.1", "completion": "0"}},
            {**base, "id": "example/small:free", "context_length": 1000},
            {**base, "id": "example/no-tools:free", "supported_parameters": []},
            {**base, "pricing": ["invalid"]},
        ]
    }
    assert [m["id"] for m in free_tool_models(data, min_context=16000)] == ["example/model:free"]


async def test_model_catalog_sends_no_key_or_code(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-key-must-not-be-sent")
    requests_seen = []

    def handler(request):
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "example/model:free",
                        "pricing": {"prompt": "0", "completion": "0"},
                        "supported_parameters": ["tools"],
                        "context_length": 32000,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await list_models(Settings(), client)
    assert result["models"][0]["id"] == "example/model:free"
    assert requests_seen[0].method == "GET" and not requests_seen[0].content
    assert "authorization" not in requests_seen[0].headers


async def test_model_catalog_refuses_redirects():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example/models"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(ForgeError, match="HTTP 302"):
            await list_models(Settings(), client)
    assert len(seen) == 1
