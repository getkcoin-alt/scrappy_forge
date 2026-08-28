"""Exercise real file edits and tests with a scripted provider; no LLM or API call.

Install Forge first, then: python examples/offline_demo.py
Only the tiny project created by this script is executed in trusted-local mode.
"""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from scrappy_forge.config import Settings
from scrappy_forge.engine import Engine, create_session
from scrappy_forge.store import Store


class DemoProvider:
    def __init__(self):
        self.step = 0

    async def complete(self, messages, tools, on_attempt):
        on_attempt()
        self.step += 1
        if self.step == 1:
            name, args = "file_read", {"path": "calculator.py"}
        elif self.step == 2:
            observed = json.loads(messages[-1]["content"])
            name, args = (
                "file_edit",
                {
                    "path": "calculator.py",
                    "old": "a - b",
                    "new": "a + b",
                    "expected_sha256": observed["sha256"],
                },
            )
        else:
            return {
                "model": "SCRIPTED-DEMO-NOT-AN-LLM",
                "message": {
                    "role": "assistant",
                    "content": "Scripted edit finished; the controller verifies.",
                },
            }
        return {
            "model": "SCRIPTED-DEMO-NOT-AN-LLM",
            "message": {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": f"demo-{self.step}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            },
        }


async def demo(root):
    project = root / "project"
    project.mkdir()
    (project / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
    (project / "tests").mkdir()
    (project / "tests/test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\n"
        "class AdditionTest(unittest.TestCase):\n"
        "    def test_add(self): self.assertEqual(add(2, 3), 5)\n"
    )
    settings = Settings(
        home=root / "state",
        provider="local",
        model="SCRIPTED-DEMO-NOT-AN-LLM",
        execution="trusted-local",
        max_steps=5,
        allow_tools=["file_edit", "verification_run"],
        checks=[{"name": "unit", "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests"]}],
    )
    settings.validate()
    store = Store(settings.home)
    try:
        state = create_session(store, settings, project)
        async with Engine(store, settings, state, DemoProvider()) as engine:
            result = await engine.ask("Fix addition. Preserve the tests.")
            summary = {
                "provider": "SCRIPTED-DEMO-NOT-AN-LLM",
                "network_requests": 0,
                "execution": "trusted-local: only this script's generated toy project",
                "status": result["status"],
                "baseline_passed": result["baseline_verification"][0]["passed"],
                "final_passed": result["verification"][0]["passed"] if result["verification"] else False,
                "original_unchanged": "a - b" in (project / "calculator.py").read_text(),
                "report": str(engine.directory / "report.json"),
                "patch": str(engine.directory / "changes.patch"),
                "note": "Tests the execution pipeline, not AI reasoning or a 10x advantage.",
            }
            print(json.dumps(summary, indent=2))
            return 0 if summary["status"] == "review_ready" and summary["original_unchanged"] else 1
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, help="New directory for the generated demo; must not already exist"
    )
    args = parser.parse_args()
    root = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix="forge-offline-demo-"))
    if args.output:
        root.mkdir(parents=True, exist_ok=False)
    return asyncio.run(demo(root))


if __name__ == "__main__":
    sys.exit(main())
