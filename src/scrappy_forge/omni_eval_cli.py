"""Standalone CLI for held-out Omni-City model evaluation.

Usage:
    python -m scrappy_forge.omni_eval_cli path/to/scenario.json
    python -m scrappy_forge.omni_eval_cli path/to/scenario.json --output-dir ./reports

This runner intentionally does not construct a Forge Engine, Registry, Runner,
MCP manager, workspace or host-tool set. The configured model receives only the
synthetic ``omni_city_action`` simulator decision tool.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .config import Settings
from .omni_eval import EvaluationConfig, ProviderController, evaluate_file
from .providers import ChatProvider
from .util import ForgeError, atomic_json, encoded, sha


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scrappy_forge.omni_eval_cli",
        description="Evaluate the configured model in a deterministic Omni-City simulator.",
    )
    parser.add_argument("scenario", type=Path, help="Omni-City scenario JSON file")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Optional directory for content-addressed JSON reports; default prints JSON.",
    )
    parser.add_argument("--max-requests", type=int, default=16)
    parser.add_argument("--max-actions", type=int, default=12)
    parser.add_argument("--seed", type=int, default=0)
    return parser


def persist_report(output_dir: Path, report: dict) -> Path:
    """Write a content-addressed report without overwriting different evidence."""

    report_id = sha(encoded(report))
    scenario_id = str(report.get("scenario_id") or "scenario").replace("/", "-")[:120]
    path = output_dir / f"{scenario_id}-{report_id[:16]}.json"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if sha(existing) != sha(encoded(report)):
            raise ForgeError(f"report path collision: {path}")
        return path
    atomic_json(path, report)
    return path


async def _run(args: argparse.Namespace) -> int:
    settings = Settings.load()
    config = EvaluationConfig(
        max_requests=args.max_requests,
        max_action_attempts=args.max_actions,
        scenario_seed=args.seed,
    )
    provider = ChatProvider(settings)
    try:
        controller = ProviderController(
            provider,
            controller_id=f"{settings.provider}:{settings.model}",
        )
        report = await evaluate_file(args.scenario, controller, config=config)
    finally:
        provider.close()

    if args.output_dir is None:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        path = persist_report(args.output_dir, report)
        print(path)
    return 0 if report["complete"] else 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (ForgeError, OSError, ValueError) as exc:
        raise SystemExit(f"omni-city evaluation failed: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
