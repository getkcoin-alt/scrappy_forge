import json

import httpx
import pytest
from starlette.testclient import TestClient

from forge_hub.app import HubSettings, create_app
from forge_hub.auth import AuthConfig, AuthError, RateLimit
from scrappy_forge import __version__

OWNER = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"
ACCESS, REFRESH = "fixture-access-token", "fixture-refresh-token"


@pytest.fixture
def hub_settings(tmp_path):
    (tmp_path / f"scrappy_forge-{__version__}-py3-none-any.whl").write_bytes(
        b"explicit fixture package, not an installable wheel"
    )
    return HubSettings(public_url="https://forge.example", release_dir=tmp_path)


def identity_transport(*, subject=USER, confirmed=True, anonymous=False, banned_until=None):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "identity.example"
        if request.url.path.endswith("/admin/users"):
            return httpx.Response(
                200, json={"users": [{"id": USER, "email": "user@example.com", "private": "not-returned"}]}
            )
        if "/admin/users/" in request.url.path:
            return httpx.Response(200, json={"id": request.url.path.split("/")[-1]})
        if request.url.path.endswith("/user"):
            return httpx.Response(
                200,
                json={
                    "id": subject,
                    "email": "user@example.com",
                    "email_confirmed_at": "2026-01-01T00:00:00Z" if confirmed else None,
                    "is_anonymous": anonymous,
                    "banned_until": banned_until,
                    "user_metadata": {"role": "owner", "admin": True},
                },
            )
        if request.url.path.endswith("/logout"):
            return httpx.Response(204)
        if request.url.path.endswith("/signup"):
            return httpx.Response(200, json={"id": USER})
        return httpx.Response(
            200, json={"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600}
        )

    return httpx.MockTransport(handler), calls


def configured(settings, **kwargs):
    settings.auth = AuthConfig(
        "https://identity.example", "fixture-publishable-key", OWNER, "fixture-secret-key", True
    )
    transport, calls = identity_transport(**kwargs)
    return TestClient(create_app(settings, auth_transport=transport), base_url="https://forge.example"), calls


def test_release_service_works_without_accounts(hub_settings):
    with TestClient(create_app(hub_settings), base_url="https://forge.example") as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        state = client.get("/v1/status").json()
        assert state["release_ready"] and not state["accounts_configured"]
        assert not state["remote_code_execution"] and not state["telemetry"]
        release = client.get("/v1/releases/latest").json()
        assert release["version"] == __version__ and len(release["sha256"]) == 64
        installer = client.get("/install.sh").text
        assert release["sha256"] in installer and "@@FORGE" not in installer
        assert client.get("/downloads/" + release["wheel"]).status_code == 200
        assert client.get("/downloads/other.whl").status_code == 404
        assert client.get("/v1/me").status_code == 401
        login = client.post(
            "/v1/auth/login", json={"email": "user@example.com", "password": "fixture-password"}
        )
        assert login.status_code == 503


def test_missing_release_is_not_ready(tmp_path):
    with TestClient(create_app(HubSettings(release_dir=tmp_path))) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        assert client.get("/install.sh").status_code == 503


def test_public_guides_use_release_origin(hub_settings):
    with TestClient(create_app(hub_settings), base_url="https://forge.example") as client:
        for path in ("/guide", "/plans", "/world-model"):
            page = client.get(path)
            assert page.status_code == 200 and "@@FORGE" not in page.text
        assert "https://forge.example/install.sh" in client.get("/guide").text
        for response in (
            client.post("/v1/auth/login", headers={"Origin": "https://evil.example"}, json={}),
            client.delete("/v1/me"),
        ):
            assert response.status_code in {403, 405}
            assert response.headers["x-content-type-options"] == "nosniff"


def test_login_page_is_user_only_and_security_headers_exist(hub_settings):
    with TestClient(create_app(hub_settings), base_url="https://forge.example") as client:
        page = client.get("/login")
        assert "Scrappy Forge" in page.text and "admin" not in page.text.lower()
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert page.headers["x-content-type-options"] == "nosniff"
        assert page.headers["cache-control"] == "no-store"
        assert "set-cookie" not in page.headers
        assert client.get("/admin").status_code == 404
        assert client.get("/assets/secrets.json").status_code == 404
        assert (
            client.post("/v1/auth/login", headers={"Origin": "https://evil.example"}, json={}).status_code
            == 403
        )


def test_login_does_not_trust_user_metadata_for_owner(hub_settings, caplog):
    client, calls = configured(hub_settings)
    with client:
        result = client.post(
            "/v1/auth/login", json={"email": "user@example.com", "password": "private-password-fixture"}
        )
        assert result.status_code == 200
        data = result.json()
        assert data["user"]["role"] == "user" and data["access_token"] == ACCESS
        assert calls[0].url.params["grant_type"] == "password"
        assert json.loads(calls[0].content)["password"] == "private-password-fixture"
        denial = client.get("/v1/admin/users", headers={"Authorization": "Bearer " + ACCESS})
        assert denial.status_code == 403
        assert not any("/admin/users" in str(r.url) for r in calls)
        assert "private-password-fixture" not in caplog.text and ACCESS not in caplog.text
        assert "fixture-secret-key" not in result.text


@pytest.mark.parametrize("flags", [{"confirmed": False}, {"anonymous": True}])
def test_unconfirmed_and_anonymous_accounts_are_denied(hub_settings, flags):
    client, _ = configured(hub_settings, **flags)
    with client:
        assert client.get("/v1/me", headers={"Authorization": "Bearer " + ACCESS}).status_code == 403


@pytest.mark.parametrize(
    "timestamp,status", [("2099-01-01T00:00:00Z", 403), ("2099-01-01", 401), ({}, 401), (True, 401)]
)
def test_suspension_metadata_fails_closed(hub_settings, timestamp, status):
    client, _ = configured(hub_settings, banned_until=timestamp)
    with client:
        assert client.get("/v1/me", headers={"Authorization": "Bearer " + ACCESS}).status_code == status


def test_owner_has_only_backend_administration(hub_settings):
    client, calls = configured(hub_settings, subject=OWNER)
    with client:
        headers = {"Authorization": "Bearer " + ACCESS}
        users = client.get("/v1/admin/users", headers=headers)
        assert users.status_code == 200 and "private" not in users.text
        assert calls[-1].headers["apikey"] == "fixture-secret-key"
        suspended = client.patch("/v1/admin/users/" + USER, headers=headers, json={"suspended": True})
        assert suspended.status_code == 200
        assert json.loads(calls[-1].content) == {"ban_duration": "876000h"}
        assert (
            client.patch("/v1/admin/users/" + OWNER, headers=headers, json={"suspended": True}).status_code
            == 403
        )
        assert (
            client.patch("/v1/admin/users/" + USER, headers=headers, json={"role": "owner"}).status_code
            == 400
        )


def test_refresh_and_logout_use_provider_without_cookies(hub_settings):
    client, calls = configured(hub_settings)
    with client:
        result = client.post("/v1/auth/refresh", json={"refresh_token": REFRESH})
        assert result.status_code == 200 and result.json()["refresh_token"] == REFRESH
        assert calls[0].url.params["grant_type"] == "refresh_token"
        signed_out = client.post("/v1/auth/logout", headers={"Authorization": "Bearer " + ACCESS}, json={})
        assert signed_out.status_code == 200 and calls[-1].url.params["scope"] == "local"
        assert "set-cookie" not in signed_out.headers


def test_registration_is_explicit_and_checks_password_and_fields(hub_settings):
    client, calls = configured(hub_settings)
    with client:
        assert (
            client.post(
                "/v1/auth/signup", json={"email": "user@example.com", "password": "short"}
            ).status_code
            == 400
        )
        response = client.post(
            "/v1/auth/signup", json={"email": "user@example.com", "password": "long-fixture-password"}
        )
        assert response.status_code == 202 and "access_token" not in response.text
        assert calls[-1].url.params["redirect_to"] == "https://forge.example/login"
    hub_settings.auth.signup_enabled = False
    with TestClient(create_app(hub_settings)) as client:
        assert client.post("/v1/auth/signup", json={}).status_code == 503


def test_auth_body_bounds_and_content_type(hub_settings):
    client, _ = configured(hub_settings)
    with client:
        assert client.post("/v1/auth/login", data={"email": "x", "password": "y"}).status_code == 415
        assert (
            client.post(
                "/v1/auth/login", content="x" * 20001, headers={"Content-Type": "application/json"}
            ).status_code
            == 413
        )
        assert (
            client.post(
                "/v1/auth/login", json={"email": "user@example.com", "password": "123", "admin": True}
            ).status_code
            == 400
        )


def test_provider_redirect_and_error_bodies_are_not_forwarded(hub_settings):
    hub_settings.auth = AuthConfig("https://identity.example", "public-key-fixture")
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            302, text="sensitive-error-body", headers={"Location": "https://evil.example"}
        )
    )
    with TestClient(create_app(hub_settings, auth_transport=transport)) as client:
        result = client.get("/v1/me", headers={"Authorization": "Bearer " + ACCESS})
        assert result.status_code == 502 and "sensitive-error-body" not in result.text


def test_rate_limiter_capacity_and_reset():
    limiter = RateLimit(maximum=2, capacity=1)
    limiter.check("first", now=0)
    limiter.check("first", now=1)
    with pytest.raises(AuthError):
        limiter.check("first", now=2)
    with pytest.raises(AuthError):
        limiter.check("second", now=2)
    limiter.check("second", now=61)


@pytest.mark.parametrize(
    "origin",
    [
        "http://public.example",
        "https://name:secret@forge.example",
        "https://forge.example/path",
        "https://forge.example?x=1",
        "https://forge.example';echo-x",
        "https://forge.example:0",
    ],
)
def test_public_origin_cannot_inject_installer(origin):
    with pytest.raises(ValueError):
        create_app(HubSettings(public_url=origin))
