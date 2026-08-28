"""Railway ASGI service: releases + optional managed accounts. No remote execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from scrappy_forge import __version__

from .auth import AuthConfig, AuthError, IdentityProvider, RateLimit, audit

PACKAGE_ROOT = Path(__file__).parent


@dataclass
class HubSettings:
    public_url: str = "http://127.0.0.1:8080"
    release_dir: Path = field(default_factory=lambda: Path("dist"))
    commit: str = "unknown"
    auth: AuthConfig = field(default_factory=AuthConfig)

    @classmethod
    def environment(cls):
        domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
        public = os.environ.get("FORGE_PUBLIC_URL") or (
            "https://" + domain if domain else "http://127.0.0.1:8080"
        )
        return cls(
            public.rstrip("/"),
            Path(os.environ.get("FORGE_RELEASE_DIR", "dist")),
            os.environ.get("RAILWAY_GIT_COMMIT_SHA", "unknown"),
            AuthConfig(
                os.environ.get("SUPABASE_URL", ""),
                os.environ.get("SUPABASE_PUBLISHABLE_KEY", ""),
                os.environ.get("FORGE_OWNER_SUBJECT", ""),
                os.environ.get("SUPABASE_SECRET_KEY", ""),
                os.environ.get("FORGE_SIGNUP_ENABLED") == "1",
            ),
        )

    def validate(self):
        p = urlsplit(self.public_url)
        if (
            not p.hostname
            or not re.fullmatch(r"[a-zA-Z0-9.-]+", p.hostname)
            or any(c.isspace() for c in self.public_url)
            or p.username
            or p.password
            or p.query
            or p.fragment
            or p.path not in {"", "/"}
            or (p.scheme != "https" and not (p.scheme == "http" and p.hostname in {"127.0.0.1", "localhost"}))
        ):
            raise ValueError("FORGE_PUBLIC_URL must be an HTTPS origin (loopback HTTP for development only)")
        if p.port is not None and not 1 <= p.port <= 65535:
            raise ValueError("Invalid public port")
        self.auth.validate()


class Guard:
    def __init__(self, app, origin):
        self.app, self.origin = app, origin.rstrip("/")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        origin = headers.get(b"origin", b"").decode("latin1")

        async def secured(message):
            if message["type"] == "http.response.start":
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                    (
                        b"content-security-policy",
                        b"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                    ),
                ]
                if self.origin.startswith("https:"):
                    extra.append((b"strict-transport-security", b"max-age=31536000"))
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        if origin and origin != self.origin:
            return await JSONResponse({"error": "Cross-origin requests are not allowed"}, 403)(
                scope, receive, secured
            )
        if scope["method"] not in {"GET", "HEAD", "POST", "PATCH"}:
            return await JSONResponse({"error": "Method not allowed"}, 405)(scope, receive, secured)
        await self.app(scope, receive, secured)


async def body(request, allowed, required=()):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise AuthError(415, "Send application/json")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 20000:
            raise AuthError(413, "Request exceeds 20 KB")
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise AuthError(400, "Invalid JSON") from exc
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise AuthError(400, "Invalid request fields")
    return value


def bearer(request):
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise AuthError(401, "A bearer token is required")
    return authorization[7:]


def session_payload(value):
    if not all(
        isinstance(value.get(k), str) and 1 <= len(value[k]) <= 16000
        for k in ("access_token", "refresh_token")
    ):
        raise AuthError(502, "Identity provider returned an incomplete session")
    seconds = value.get("expires_in", 3600)
    if type(seconds) is not int or not 1 <= seconds <= 86400:
        raise AuthError(502, "Identity provider returned an invalid session expiry")
    return {
        "access_token": value["access_token"],
        "refresh_token": value["refresh_token"],
        "expires_in": seconds,
        "token_type": "bearer",
    }


def create_app(settings=None, *, auth_transport=None):
    s = settings or HubSettings.environment()
    s.validate()
    provider = IdentityProvider(s.auth, transport=auth_transport)
    limiter = RateLimit(maximum=60)
    identity_limiter = RateLimit(maximum=8)
    wheel_name = f"scrappy_forge-{__version__}-py3-none-any.whl"
    wheel = s.release_dir / wheel_name
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest() if wheel.is_file() else None

    def limit(request, tag):
        peer = request.client.host if request.client else "unknown"
        limiter.check(tag + ":" + peer)

    async def error_handler(request, exc):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    async def health(request):
        return JSONResponse({"status": "ok", "version": __version__})

    async def status(request):
        return JSONResponse(
            {
                "version": __version__,
                "commit": s.commit,
                "release_ready": digest is not None,
                "accounts_configured": s.auth.configured,
                "signup_enabled": s.auth.configured and s.auth.signup_enabled,
                "owner_configured": bool(s.auth.owner_subject),
                "admin_backend_configured": bool(s.auth.owner_subject and s.auth.secret_key),
                "remote_code_execution": False,
                "telemetry": False,
            }
        )

    async def ready(request):
        return JSONResponse({"release_ready": digest is not None}, 200 if digest else 503)

    async def release(request):
        if not digest:
            raise AuthError(503, "Release package is not built")
        return JSONResponse(
            {
                "schema_version": 1,
                "version": __version__,
                "commit": s.commit,
                "wheel": wheel_name,
                "sha256": digest,
                "size": wheel.stat().st_size,
                "download_url": s.public_url + "/downloads/" + wheel_name,
                "python": ">=3.11",
                "platforms": ["macOS arm64", "macOS x86_64", "Linux", "WSL"],
                "accounts_required_for_local_coding": False,
                "signature": None,
                "integrity": "SHA256 over HTTPS; not an independent publisher signature",
            }
        )

    async def download(request):
        if not digest or request.path_params["filename"] != wheel_name:
            raise AuthError(404, "Release not found")
        return FileResponse(wheel, media_type="application/octet-stream", filename=wheel_name)

    async def installer(request):
        if not digest:
            raise AuthError(503, "Release package is not built")
        text = (PACKAGE_ROOT / "install.sh.in").read_text()
        text = (
            text.replace("@@FORGE_URL@@", s.public_url)
            .replace("@@FORGE_WHEEL@@", wheel_name)
            .replace("@@FORGE_SHA256@@", digest)
        )
        return PlainTextResponse(text, media_type="text/x-shellscript")

    async def login_page(request):
        return HTMLResponse((PACKAGE_ROOT / "static/login.html").read_text())

    async def guide(request):
        names = {"/guide": "user-guide.md", "/plans": "plans.md", "/world-model": "world-model.md"}
        content = (PACKAGE_ROOT / "static" / names[request.url.path]).read_text()
        return PlainTextResponse(content.replace("@@FORGE_URL@@", s.public_url))

    async def asset(request):
        name = request.path_params["name"]
        if name not in {"login.js", "style.css"}:
            raise AuthError(404, "Not found")
        return FileResponse(
            PACKAGE_ROOT / "static" / name,
            media_type="text/javascript" if name.endswith(".js") else "text/css",
        )

    async def login(request):
        limit(request, "login")
        data = await body(request, {"email", "password"}, {"email", "password"})
        if (
            not isinstance(data["email"], str)
            or not re.fullmatch(r"[^\s@]{1,100}@[^\s@]{1,150}", data["email"])
            or not isinstance(data["password"], str)
            or not 1 <= len(data["password"]) <= 1000
        ):
            raise AuthError(400, "Provide an email and password")
        data["email"] = data["email"].strip().lower()
        identity_limiter.check(hashlib.sha256(data["email"].encode()).hexdigest())
        value = await provider.request("POST", "/token", data=data, params={"grant_type": "password"})
        session = session_payload(value)
        me = await provider.user(session["access_token"])
        audit("account.login", subject=me["id"])
        return JSONResponse({**session, "user": me})

    async def signup(request):
        limit(request, "signup")
        if not (s.auth.configured and s.auth.signup_enabled):
            raise AuthError(
                503,
                "Registration is not enabled. The owner must configure authentication and production email delivery.",
            )
        data = await body(request, {"email", "password"}, {"email", "password"})
        if (
            not isinstance(data["email"], str)
            or not re.fullmatch(r"[^\s@]{1,100}@[^\s@]{1,150}", data["email"])
            or not isinstance(data["password"], str)
            or not 12 <= len(data["password"]) <= 1000
        ):
            raise AuthError(400, "Use a valid email and a password of at least 12 characters")
        identity_limiter.check("signup:" + hashlib.sha256(data["email"].lower().encode()).hexdigest())
        await provider.request("POST", "/signup", data=data, params={"redirect_to": s.public_url + "/login"})
        # Do not auto-authenticate signup or disclose whether an address already exists.
        return JSONResponse({"message": "Check your email to confirm your account, then sign in."}, 202)

    async def refresh(request):
        limit(request, "refresh")
        data = await body(request, {"refresh_token"}, {"refresh_token"})
        if not isinstance(data["refresh_token"], str) or not 8 <= len(data["refresh_token"]) <= 16000:
            raise AuthError(400, "Invalid refresh token")
        value = await provider.request("POST", "/token", data=data, params={"grant_type": "refresh_token"})
        payload = session_payload(value)
        me = await provider.user(payload["access_token"])
        return JSONResponse({**payload, "user": me})

    async def me(request):
        limit(request, "me")
        return JSONResponse(await provider.user(bearer(request)))

    async def logout(request):
        limit(request, "logout")
        token = bearer(request)
        user = await provider.user(token)
        await provider.request("POST", "/logout", token=token, params={"scope": "local"})
        audit("account.logout", subject=user["id"])
        return JSONResponse(
            {"signed_out": True, "note": "Existing access tokens may remain valid until their expiry."}
        )

    async def owner(request):
        limit(request, "admin")
        user = await provider.user(bearer(request))
        if user["role"] != "owner":
            audit("admin.denied", subject=user["id"])
            raise AuthError(403, "Owner access required")
        return user

    async def admin_users(request):
        user = await owner(request)
        try:
            page = max(1, min(10000, int(request.query_params.get("page", "1"))))
        except ValueError as exc:
            raise AuthError(400, "Invalid page") from exc
        result = await provider.request(
            "GET", "/admin/users", privileged=True, params={"page": page, "per_page": 50}
        )
        audit("admin.users.list", subject=user["id"], page=page)
        return JSONResponse(
            {
                "page": page,
                "users": [
                    {k: row.get(k) for k in ("id", "email", "created_at", "last_sign_in_at", "banned_until")}
                    for row in result.get("users", [])[:50]
                ],
            }
        )

    async def admin_suspend(request):
        user = await owner(request)
        try:
            target = str(uuid.UUID(request.path_params["subject"]))
        except ValueError as exc:
            raise AuthError(400, "Invalid account ID") from exc
        if target == s.auth.owner_subject:
            raise AuthError(403, "This endpoint cannot suspend the owner")
        data = await body(request, {"suspended"}, {"suspended"})
        if type(data["suspended"]) is not bool:
            raise AuthError(400, "suspended must be a boolean")
        await provider.request(
            "PUT",
            "/admin/users/" + target,
            privileged=True,
            data={"ban_duration": "876000h" if data["suspended"] else "none"},
        )
        audit("admin.user.suspend", subject=user["id"], target=target, suspended=data["suspended"])
        return JSONResponse({"id": target, "suspended": data["suspended"]})

    @asynccontextmanager
    async def lifespan(app):
        yield
        await provider.close()

    app = Starlette(
        routes=[
            Route("/", login_page),
            Route("/login", login_page),
            Route("/guide", guide),
            Route("/plans", guide),
            Route("/world-model", guide),
            Route("/healthz", health),
            Route("/readyz", ready),
            Route("/v1/status", status),
            Route("/v1/releases/latest", release),
            Route("/downloads/{filename}", download),
            Route("/install.sh", installer),
            Route("/assets/{name}", asset),
            Route("/v1/auth/login", login, methods=["POST"]),
            Route("/v1/auth/signup", signup, methods=["POST"]),
            Route("/v1/auth/refresh", refresh, methods=["POST"]),
            Route("/v1/auth/logout", logout, methods=["POST"]),
            Route("/v1/me", me),
            Route("/v1/admin/users", admin_users),
            Route("/v1/admin/users/{subject}", admin_suspend, methods=["PATCH"]),
        ],
        exception_handlers={AuthError: error_handler},
        lifespan=lifespan,
    )
    app.add_middleware(Guard, origin=s.public_url)
    return app


def main():
    import uvicorn

    # One worker: rate limits are in-process. No trusted forwarded headers from arbitrary clients.
    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        workers=1,
        access_log=False,
        proxy_headers=False,
        limit_concurrency=100,
        timeout_keep_alive=5,
        log_level="info",
    )


if __name__ == "__main__":
    main()
