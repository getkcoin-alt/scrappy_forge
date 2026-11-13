"""Live-model evaluation of Scrappy Engine on small authored missions.

Unlike tests/test_scrappy_engine.py, every architect/engineer/debugger/reviewer step
here is a real model call (OpenRouter free models through Forge's ChatProvider). Each
task has a held-out check the models never see as a file, and results are reported
as-is, failures included.

    python validation/engine_live_eval.py --model <builder:free> --review-model <other:free> \
        --out results.json [--task auth] [--runs 1]

Checks run with trusted-local execution (host Python) because the tasks are authored
here and contain no untrusted code. Requires OPENROUTER_API_KEY (or a proxy that
injects it); --proxy-from-env routes requests through HTTPS_PROXY with its CA bundle.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

from scrappy_engine.contracts import MissionSpec
from scrappy_engine.intelligence import ChatJSONProvider, ProviderRouter
from scrappy_engine.runner import MissionRunner
from scrappy_engine.store import EngineStore
from scrappy_forge.config import Settings
from scrappy_forge.execution import Runner
from scrappy_forge.providers import ChatProvider

PY = sys.executable
UNIT = [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."]

TASKS = {
    "auth": {
        "objective": "Find the underlying bug in the authentication workflow, implement a fix, add regression"
        " tests, and make sure the complete test suite passes.",
        "criteria": ["Only the exact stored password authenticates", "A regression test covers the bug"],
        "files": {
            "auth.py": 'USERS = {"alice": "wonderland"}\n\n\ndef authenticate(username, password):\n'
            "    stored = USERS.get(username)\n    if stored is None:\n        return False\n"
            "    return stored.startswith(password)\n",
            "tests/__init__.py": "",
            "tests/test_auth.py": "import unittest\n\nfrom auth import authenticate\n\n\n"
            "class AuthTests(unittest.TestCase):\n    def test_valid_login(self):\n"
            '        self.assertTrue(authenticate("alice", "wonderland"))\n\n'
            "    def test_unknown_user(self):\n"
            '        self.assertFalse(authenticate("bob", "wonderland"))\n',
        },
        "heldout": "from auth import authenticate as a\n"
        "assert a('alice', 'wonderland')\nassert not a('alice', '')\nassert not a('alice', 'wonder')\n"
        "assert not a('alice', 'wonderland!')\nassert not a('bob', '')\n",
    },
    "pagination": {
        "objective": "Users report that page 1 of the listing API skips the first items and the last page is"
        " sometimes empty. Pages are 1-based. Find the root cause, fix it and add regression tests.",
        "criteria": [
            "Page 1 starts at the first item",
            "page_count is exact",
            "Invalid pages raise ValueError",
        ],
        "files": {
            "listing.py": "def paginate(items, page, size):\n"
            '    """Return the items on a 1-based page."""\n'
            "    start = page * size\n    return items[start:start + size]\n\n\n"
            "def page_count(total, size):\n    return total // size + 1\n",
            "tests/__init__.py": "",
            "tests/test_listing.py": "import unittest\n\nfrom listing import paginate\n\n\n"
            "class ListingTests(unittest.TestCase):\n    def test_returns_list(self):\n"
            "        self.assertIsInstance(paginate([1, 2, 3], 1, 2), list)\n",
        },
        "heldout": "from listing import paginate, page_count\nitems = list(range(10))\n"
        "assert paginate(items, 1, 3) == [0, 1, 2]\nassert paginate(items, 4, 3) == [9]\n"
        "assert page_count(10, 3) == 4\nassert page_count(9, 3) == 3\nassert page_count(0, 3) == 0\n"
        "for bad in (0, -1):\n    try:\n        paginate(items, bad, 3)\n    except ValueError:\n        pass\n"
        "    else:\n        raise AssertionError('page %r accepted' % bad)\n",
    },
    "duration": {
        "objective": "Implement parse_duration in durations.py. It converts strings such as '90s', '1h30m',"
        " '2h', '45m10s' into a whole number of seconds and raises ValueError for empty, malformed or"
        " out-of-order input such as '', 'abc', '10x', '30m1h'. Add unit tests.",
        "criteria": [
            "Units h, m, s in that order, each optional but at least one",
            "ValueError on bad input",
        ],
        "files": {
            "durations.py": "def parse_duration(text):\n    raise NotImplementedError\n",
            "tests/__init__.py": "",
            "tests/test_durations.py": "import unittest\n\nimport durations\n\n\n"
            "class DurationTests(unittest.TestCase):\n    def test_exists(self):\n"
            "        self.assertTrue(callable(durations.parse_duration))\n",
        },
        "heldout": "from durations import parse_duration as p\n"
        "assert p('90s') == 90 and p('1h30m') == 5400 and p('2h') == 7200 and p('45m10s') == 2710\n"
        "assert p('1h0m5s') == 3605\n"
        "for bad in ('', 'abc', '10x', '30m1h', 'h', '1.5h', '-5s', '1h 30m'):\n"
        "    try:\n        p(bad)\n    except ValueError:\n        pass\n"
        "    else:\n        raise AssertionError('accepted %r' % bad)\n",
    },
}


def make_repo(root: Path, task: dict) -> None:
    for name, content in task["files"].items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git = ["git", "-c", "user.name=eval", "-c", "user.email=eval@example.invalid"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run([*git, "add", "-A"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=root, check=True)


def chat(model: str, session: requests.Session | None) -> ChatProvider:
    settings = Settings(provider="openrouter", model=model, context_tokens=32000, output_tokens=6000)
    settings.validate()
    key = os.environ.get("OPENROUTER_API_KEY") or "proxy-injected"
    return ChatProvider(settings, session=session, api_key=key)


def proxied_session() -> requests.Session:
    session = requests.Session()
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        session.proxies = {"https": proxy}
    bundle = os.environ.get("REQUESTS_CA_BUNDLE") or "/root/.ccr/ca-bundle.crt"
    if Path(bundle).exists():
        session.verify = bundle
    return session


async def run_task(name: str, args, workdir: Path) -> dict:
    task = TASKS[name]
    repo = workdir / f"{name}-repo"
    repo.mkdir()
    make_repo(repo, task)
    heldout = workdir / f"{name}-heldout.py"
    heldout.write_text(task["heldout"])
    store = EngineStore(workdir / f"{name}.db")
    project = store.connect_project(name, repo)
    spec = MissionSpec.from_dict(
        {
            "project_id": project["id"],
            "objective": task["objective"],
            "environment": "trusted-local",
            "autonomy_level": "A3",
            "constraints": {"max_retries": args.max_retries, "max_model_calls": args.max_model_calls},
            "acceptance_criteria": task["criteria"],
            "checks": [
                {"name": "unit", "argv": UNIT, "timeout": 120},
                # The held-out script lives outside the repository: the models never see it.
                {"name": "heldout", "argv": [PY, str(heldout)], "timeout": 120},
            ],
        }
    )
    mission, _ = store.create_mission(spec)
    session = proxied_session() if args.proxy_from_env else None
    providers = [
        ChatJSONProvider(chat(args.model, session), name="builder", family=args.model),
        ChatJSONProvider(chat(args.review_model, session), name="reviewer", family=args.review_model),
    ]
    if args.fallback_model:
        providers.append(
            ChatJSONProvider(chat(args.fallback_model, session), name="fallback", family=args.fallback_model)
        )
    order = ["builder", "fallback"] if args.fallback_model else ["builder"]
    router = ProviderRouter(
        providers,
        {
            "architect": order,
            "engineer": order,
            "debugger": order,
            "reviewer": ["reviewer", *order],
        },
    )
    runner = MissionRunner(
        store, router, home=workdir / f"{name}-home", runner_factory=lambda env: Runner(mode="trusted-local")
    )
    started = time.monotonic()
    final = await runner.run(mission["id"])
    elapsed = round(time.monotonic() - started, 1)
    runs = store.agent_runs(mission["id"])
    result = final["result"] or {}
    history = (final["context"] or {}).get("history", [])
    return {
        "task": name,
        "state": final["state"],
        "reason": final["reason"],
        "attempts": final["attempts"],
        "model_calls": final["model_calls"],
        "failed_model_calls": sum(1 for r in runs if r["status"] != "succeeded"),
        "model_errors": [r["error"][:300] for r in runs if r["error"]][:6],
        "providers_used": sorted(
            {f"{r['role']}:{r['provider']}" for r in runs if r["status"] == "succeeded"}
        ),
        "cost_usd": final["cost_usd"],
        "seconds": elapsed,
        "reproduced": (final["context"] or {}).get("reproduced"),
        "final_checks": result.get("final_checks"),
        "review": result.get("review"),
        "attempt_gate_failures": [h.get("gate_failures") for h in history],
        "audit_chain_valid": store.verify_audit_chain(),
        "patch": next((a["content"] for a in store.artifacts(mission["id"]) if a["kind"] == "patch"), None),
    }


async def main_async(args) -> list[dict]:
    results = []
    with tempfile.TemporaryDirectory(prefix="scrappy-eval-") as temp:
        for run in range(args.runs):
            for name in args.task or list(TASKS):
                workdir = Path(temp) / f"run{run}"
                workdir.mkdir(exist_ok=True)
                print(f"[run {run}] {name} ...", file=sys.stderr, flush=True)
                try:
                    outcome = await run_task(name, args, workdir)
                except Exception as exc:  # report harness crashes as results, never hide them
                    outcome = {"task": name, "state": "HARNESS_ERROR", "reason": repr(exc)[:1000]}
                outcome["run"] = run
                print(
                    f"[run {run}] {name}: {outcome['state']} {outcome.get('reason') or ''}"[:300],
                    file=sys.stderr,
                    flush=True,
                )
                results.append(outcome)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--review-model", required=True)
    parser.add_argument("--fallback-model")
    parser.add_argument("--task", action="append", choices=sorted(TASKS))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--max-model-calls", type=int, default=16)
    parser.add_argument("--proxy-from-env", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    results = asyncio.run(main_async(args))
    payload = {
        "models": {"builder": args.model, "reviewer": args.review_model, "fallback": args.fallback_model},
        "results": results,
        "completed": sum(r["state"] == "COMPLETED" for r in results),
        "total": len(results),
    }
    args.out.write_text(json.dumps(payload, indent=2))
    print(f"completed {payload['completed']}/{payload['total']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
