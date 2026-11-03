"""scrappy-engine command line."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import shlex
import sys
from pathlib import Path

from scrappy_forge.util import ForgeError

from .contracts import MissionSpec
from .intelligence import ChatJSONProvider, ProviderRouter
from .runner import MissionRunner
from .store import EngineStore


def engine_home() -> Path:
    return Path(
        os.environ.get("SCRAPPY_ENGINE_HOME", Path.home() / ".local/share/scrappy-engine")
    ).expanduser()


def build_router(args) -> ProviderRouter:
    from scrappy_forge.config import Settings
    from scrappy_forge.providers import ChatProvider

    settings = Settings.load()
    providers = [ChatJSONProvider(ChatProvider(settings), name="primary", family=settings.model)]
    preferences = {}
    if getattr(args, "review_model", None):
        review = dataclasses.replace(settings, model=args.review_model)
        providers.append(ChatJSONProvider(ChatProvider(review), name="review", family=args.review_model))
        preferences["reviewer"] = ["review", "primary"]
    return ProviderRouter(providers, preferences)


def parse_check(text: str) -> dict:
    name, sep, command = text.partition("=")
    if not sep or not command.strip():
        raise ForgeError("--check must be NAME=COMMAND")
    return {"name": name.strip(), "argv": shlex.split(command), "timeout": 300}


def print_json(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


async def _run(args, store: EngineStore) -> int:
    project = store.connect_project(Path(args.project).name, Path(args.project))
    spec = MissionSpec.from_dict(
        {
            "project_id": project["id"],
            "objective": args.objective,
            "environment": "trusted-local" if args.trusted_local else "sandbox",
            "autonomy_level": args.level,
            "constraints": {"max_retries": args.max_retries, "max_model_calls": args.max_model_calls},
            "acceptance_criteria": args.criterion or [],
            "checks": [parse_check(c) for c in args.check],
        }
    )
    mission, _ = store.create_mission(spec, idempotency_key=args.idempotency_key)
    print(f"mission {mission['id']} created", file=sys.stderr)
    runner = MissionRunner(store, build_router(args), home=engine_home())
    final = await runner.run(mission["id"])
    print_json(
        {"id": final["id"], "state": final["state"], "reason": final["reason"], "result": final["result"]}
    )
    return 0 if final["state"] == "COMPLETED" else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="scrappy-engine", description="Scrappy Engine mission runner")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="create and run a mission against a local project")
    run.add_argument("project")
    run.add_argument("--objective", required=True)
    run.add_argument("--check", action="append", required=True, help="NAME=COMMAND acceptance check")
    run.add_argument("--criterion", action="append", help="human-readable acceptance criterion")
    run.add_argument("--level", default="A3", choices=["A0", "A1", "A2", "A3", "A4"])
    run.add_argument("--max-retries", type=int, default=2)
    run.add_argument("--max-model-calls", type=int, default=20)
    run.add_argument("--trusted-local", action="store_true", help="run checks on the host instead of Docker")
    run.add_argument("--review-model", help="different model for independent review")
    run.add_argument("--idempotency-key")
    for name in ("status", "events", "evidence", "resume", "cancel"):
        p = sub.add_parser(name)
        p.add_argument("mission")
    sub.add_parser("recover", help="mark interrupted executions uncertain and resume missions")
    sub.add_parser("list")
    serve = sub.add_parser("serve", help="run the mission API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--project-root", action="append", required=True)
    serve.add_argument("--review-model")
    args = parser.parse_args(argv)

    store = EngineStore(engine_home() / "engine.db")
    try:
        if args.command == "run":
            return asyncio.run(_run(args, store))
        if args.command == "status":
            m = store.get_mission(args.mission)
            print_json(
                {k: m[k] for k in ("id", "state", "reason", "attempts", "model_calls", "cost_usd", "result")}
            )
        elif args.command == "events":
            print_json(store.events(args.mission))
        elif args.command == "evidence":
            print_json(
                {"tool_calls": store.tool_calls(args.mission), "agent_runs": store.agent_runs(args.mission)}
            )
        elif args.command == "list":
            print_json([{k: m[k] for k in ("id", "state", "reason")} for m in store.missions()])
        elif args.command == "cancel":
            print_json(store.request_cancel(args.mission)["state"])
        elif args.command in {"resume", "recover"}:
            runner = MissionRunner(store, build_router(args), home=engine_home())
            ids = store.recover()
            if args.command == "resume":
                if store.get_mission(args.mission)["state"] == "BLOCKED":
                    runner.resume(args.mission)
                ids = [args.mission]
            for mission_id in ids:
                final = asyncio.run(runner.run(mission_id))
                print_json({"id": final["id"], "state": final["state"], "reason": final["reason"]})
        elif args.command == "serve":
            import uvicorn

            from .api import create_app

            token = os.environ.get("SCRAPPY_ENGINE_TOKEN", "")
            runner = MissionRunner(store, build_router(args), home=engine_home())
            app = create_app(runner, token=token, project_roots=[Path(p) for p in args.project_root])
            uvicorn.run(app, host=args.host, port=args.port)
        return 0
    except ForgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
