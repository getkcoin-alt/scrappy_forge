"""Primary terminal surface for the persistent Scrappy operator.

Normal prompts and existing commands delegate to the mature Forge CLI. Operator-
level capability and evaluator commands are handled here so the product can grow
without destabilizing the coding loop.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .capabilities import append_pending_request, broker_from_settings, unresolved_pending_requests
from .config import Settings
from .evaluator import EvaluatorReview, aggregate_reviews
from .util import ForgeError, clean


def _capability(argv: list[str]) -> int:
    settings = Settings.load(None)
    broker = broker_from_settings(settings)
    if not argv or argv[0] == "list":
        print(
            json.dumps(
                {"available": broker.list(), "pending": unresolved_pending_requests(settings)},
                indent=2,
            )
        )
        return 0
    if argv[0] == "request":
        if len(argv) < 4:
            raise ForgeError(
                "Usage: scrappy capability request KIND SCOPE REASON [--provider NAME] [--no-secret]"
            )
        kind, scope, reason = argv[1], argv[2], argv[3]
        provider = None
        secret_required = True
        index = 4
        while index < len(argv):
            if argv[index] == "--provider" and index + 1 < len(argv):
                provider = argv[index + 1]
                index += 2
            elif argv[index] == "--no-secret":
                secret_required = False
                index += 1
            else:
                raise ForgeError(f"Unknown capability request option: {argv[index]}")
        request = broker.request(
            kind=kind,
            reason=reason,
            required_scope=scope,
            suggested_provider=provider,
            secret_required=secret_required,
        )
        append_pending_request(settings.home, request)
        print(json.dumps(request.to_dict(), indent=2))
        return 0
    raise ForgeError("Usage: scrappy capability list | request ...")


def _evaluator(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "aggregate":
        raise ForgeError("Usage: scrappy evaluator aggregate REVIEWS.json")
    path = Path(argv[1]).expanduser().resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("manifest_sha256"), str):
        raise ForgeError("Evaluator input requires manifest_sha256")
    items = raw.get("reviews")
    if not isinstance(items, list):
        raise ForgeError("Evaluator input requires reviews[]")
    reviews = [EvaluatorReview(**item) for item in items]
    result = aggregate_reviews(
        raw["manifest_sha256"],
        reviews,
        min_approvals=int(raw.get("min_approvals", 2)),
        min_confidence=float(raw.get("min_confidence", 0.7)),
    )
    print(json.dumps(result, indent=2))
    return 0


def main() -> int:
    try:
        argv = sys.argv[1:]
        if argv and argv[0] == "capability":
            return _capability(argv[1:])
        if argv and argv[0] == "evaluator":
            return _evaluator(argv[1:])
        from .cli import main as forge_main

        return forge_main()
    except (ForgeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(clean("scrappy: " + str(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
