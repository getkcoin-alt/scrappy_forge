"""Mission API (Starlette — the ASGI core FastAPI is built on, already a Forge dependency).

Every route except /v1/health requires ``Authorization: Bearer <SCRAPPY_ENGINE_TOKEN>``.
Projects can only be connected from directories under the configured project roots,
so an API caller cannot point the engine at arbitrary host paths.
"""

from __future__ import annotations

import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from scrappy_forge.util import ForgeError

from .autonomy import ROLE_SCOPES
from .contracts import MissionSpec
from .intelligence import ROLES
from .runner import MissionRunner
from .states import TERMINAL, MissionState

MAX_BODY = 256_000


def public_mission(mission: dict) -> dict:
    ctx = mission["context"]
    return {
        "id": mission["id"],
        "project_id": mission["project_id"],
        "state": mission["state"],
        "reason": mission["reason"],
        "blocked_state": mission["blocked_state"],
        "attempts": mission["attempts"],
        "model_calls": mission["model_calls"],
        "cost_usd": round(mission["cost_usd"], 6),
        "cancel_requested": mission["cancel_requested"],
        "spec": mission["spec"],
        "plan": ctx.get("plan"),
        "result": mission["result"],
        "created_at": mission["created_at"],
        "updated_at": mission["updated_at"],
    }


def create_app(runner: MissionRunner, *, token: str, project_roots: list[Path], autostart: bool = True):
    if len(token) < 24:
        raise ForgeError("SCRAPPY_ENGINE_TOKEN must be at least 24 characters")
    store = runner.store
    roots = [Path(r).resolve() for r in project_roots]
    tasks: dict[str, asyncio.Task] = {}

    def launch(mission_id: str) -> None:
        if not autostart:
            return
        task = tasks.get(mission_id)
        if task is None or task.done():
            tasks[mission_id] = asyncio.create_task(runner.run(mission_id))

    def authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        return header.startswith("Bearer ") and hmac.compare_digest(header[7:].encode(), token.encode())

    async def body(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise ForgeError("Request body too large")
        try:
            value = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise ForgeError("Request body must be JSON") from exc
        if not isinstance(value, dict):
            raise ForgeError("Request body must be a JSON object")
        return value

    def guarded(handler):
        async def wrapped(request: Request):
            if not authorized(request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            try:
                return await handler(request)
            except ForgeError as exc:
                status = 404 if str(exc).startswith("Unknown ") else 400
                if "Idempotency key" in str(exc):
                    status = 409
                return JSONResponse({"error": str(exc)}, status_code=status)

        return wrapped

    async def health(_request):
        return JSONResponse(
            {"status": "ok", "store": store.display_name, "audit_chain_valid": store.verify_audit_chain()}
        )

    async def connect_project(request):
        data = await body(request)
        path = Path(str(data.get("path", ""))).resolve()
        if not any(path == root or path.is_relative_to(root) for root in roots):
            raise ForgeError("Project path is outside the configured project roots")
        name = str(data.get("name") or path.name)[:200]
        return JSONResponse(store.connect_project(name, path), status_code=201)

    async def list_projects(_request):
        return JSONResponse({"projects": store.projects()})

    async def create_mission(request):
        spec = MissionSpec.from_dict(await body(request))
        key = request.headers.get("idempotency-key")
        if key is not None and not (8 <= len(key) <= 200):
            raise ForgeError("Idempotency-Key must be 8-200 characters")
        mission, created = store.create_mission(spec, idempotency_key=key)
        if created:
            launch(mission["id"])
        return JSONResponse(public_mission(mission), status_code=201 if created else 200)

    async def list_missions(_request):
        return JSONResponse({"missions": [public_mission(m) for m in store.missions()]})

    async def get_mission(request):
        return JSONResponse(public_mission(store.get_mission(request.path_params["id"])))

    async def cancel(request):
        mission_id = request.path_params["id"]
        mission = store.request_cancel(mission_id)
        if mission["state"] in {MissionState.BLOCKED.value, MissionState.CREATED.value}:
            mission = store.transition(mission_id, MissionState.CANCELLED, reason="cancelled by operator")
        return JSONResponse(public_mission(mission))

    async def resume(request):
        mission = runner.resume(request.path_params["id"])
        launch(mission["id"])
        return JSONResponse(public_mission(mission))

    async def events(request):
        try:
            after = int(request.query_params.get("after", "0"))
        except ValueError as exc:
            raise ForgeError("after must be an integer") from exc
        mission_id = request.path_params["id"]
        store.get_mission(mission_id)
        return JSONResponse({"events": store.events(mission_id, after=after)})

    async def evidence(request):
        mission_id = request.path_params["id"]
        store.get_mission(mission_id)
        return JSONResponse(
            {
                "tool_calls": store.tool_calls(mission_id),
                "agent_runs": store.agent_runs(mission_id),
                "decisions": store.decisions(mission_id),
                "tasks": store.tasks(mission_id),
                "artifacts": [
                    {k: a[k] for k in ("id", "kind", "sha256", "created_at")}
                    for a in store.artifacts(mission_id)
                ],
            }
        )

    async def agents(_request):
        return JSONResponse(
            {
                "agents": [
                    {
                        "role": role.name,
                        "charter": role.charter,
                        "scopes": sorted(ROLE_SCOPES.get(role.name, ())),
                        "uses_model": role.output_schema is not None,
                    }
                    for role in ROLES.values()
                ]
            }
        )

    async def models(_request):
        return JSONResponse({"providers": runner.router.catalogue(store.provider_stats())})

    @asynccontextmanager
    async def lifespan(_app):
        for mission_id in store.recover():
            mission = store.get_mission(mission_id)
            if MissionState(mission["state"]) not in TERMINAL:
                launch(mission_id)
        yield
        for task in tasks.values():
            task.cancel()

    routes = [
        Route("/v1/health", health),
        Route("/v1/projects", guarded(list_projects)),
        Route("/v1/projects/connect", guarded(connect_project), methods=["POST"]),
        Route("/v1/missions", guarded(list_missions)),
        Route("/v1/missions", guarded(create_mission), methods=["POST"]),
        Route("/v1/missions/{id}", guarded(get_mission)),
        Route("/v1/missions/{id}/cancel", guarded(cancel), methods=["POST"]),
        Route("/v1/missions/{id}/resume", guarded(resume), methods=["POST"]),
        Route("/v1/missions/{id}/events", guarded(events)),
        Route("/v1/missions/{id}/evidence", guarded(evidence)),
        Route("/v1/agents", guarded(agents)),
        Route("/v1/models", guarded(models)),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.tasks = tasks
    return app
