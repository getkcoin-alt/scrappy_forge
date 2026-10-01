from __future__ import annotations

import json

from scrappy_forge.omni_eval_cli import persist_report


def test_persist_report_is_content_addressed_and_idempotent(tmp_path) -> None:
    report = {
        "report_schema": "omni-city-eval.v0.1",
        "scenario_id": "heldout-test-001",
        "simulated": True,
        "real_world_authority": False,
        "goal_score": 1.0,
    }

    first = persist_report(tmp_path, report)
    second = persist_report(tmp_path, report)

    assert first == second
    assert first.parent == tmp_path
    assert first.name.startswith("heldout-test-001-")
    assert json.loads(first.read_text(encoding="utf-8")) == report
    assert len(list(tmp_path.iterdir())) == 1


def test_different_reports_do_not_overwrite_each_other(tmp_path) -> None:
    first = persist_report(
        tmp_path,
        {"scenario_id": "same-scenario", "goal_score": 0.5, "simulated": True},
    )
    second = persist_report(
        tmp_path,
        {"scenario_id": "same-scenario", "goal_score": 1.0, "simulated": True},
    )

    assert first != second
    assert first.exists()
    assert second.exists()
    assert len(list(tmp_path.iterdir())) == 2
