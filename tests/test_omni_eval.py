from __future__ import annotations

import json
from pathlib import Path

import pytest

from scrappy_forge.omni_eval import (
    FINISH_ACTION,
    ControllerDecision,
    EvaluationConfig,
    ProviderController,
    evaluate_file,
)

ROOT = Path(__file__).resolve().parents[1]
HELD_OUT = ROOT / "benchmarks" / "omni_city_v0" / "held_out"


class SequenceController:
    def __init__(self, actions: list[str], controller_id: str = "test-sequence") -> None:
        self.actions = list(actions)
        self.controller_id = controller_id
        self.index = 0

    async def choose(self, observation, *, feedback, seed):
        if self.index >= len(self.actions):
            return ControllerDecision(action_id=FINISH_ACTION, model="fake-model")
        action = self.actions[self.index]
        self.index += 1
        return ControllerDecision(
            action_id=action,
            model="fake-model",
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        )


@pytest.mark.asyncio
async def test_held_out_generator_sequence_completes_with_machine_report() -> None:
    report = await evaluate_file(
        HELD_OUT / "hospital_generator_fuel.json",
        SequenceController(["connect-tanker", "transfer-fuel"]),
        config=EvaluationConfig(max_requests=5, max_action_attempts=4, scenario_seed=17),
    )

    assert report["report_schema"] == "omni-city-eval.v0.1"
    assert report["simulated"] is True
    assert report["real_world_authority"] is False
    assert report["complete"] is True
    assert report["goal_score"] == 1.0
    assert report["valid_actions"] == 2
    assert report["invalid_actions"] == 0
    assert report["usage"]["total_tokens"] == 24.0
    assert len(report["scenario_hash"]) == 64
    assert len(report["config_hash"]) == 64
    assert "not evidence of real-world control" in report["disclaimer"]


@pytest.mark.asyncio
async def test_precondition_failure_is_counted_and_successful_replan_is_measured() -> None:
    report = await evaluate_file(
        HELD_OUT / "water_quality_isolation.json",
        SequenceController(
            [
                "restore-bypass-supply",
                "isolate-alert-zone",
                "flush-bypass",
                "restore-bypass-supply",
            ]
        ),
        config=EvaluationConfig(max_requests=8, max_action_attempts=8),
    )

    assert report["complete"] is True
    assert report["invalid_actions"] == 1
    assert report["precondition_violations"] == 1
    assert report["replans"] == 1
    assert report["evaluator_interventions"] == 1


@pytest.mark.asyncio
async def test_unknown_action_never_becomes_an_implicit_capability() -> None:
    report = await evaluate_file(
        HELD_OUT / "hospital_generator_fuel.json",
        SequenceController(["restart-real-hospital-generator", FINISH_ACTION]),
        config=EvaluationConfig(max_requests=4, max_action_attempts=4),
    )

    assert report["complete"] is False
    assert report["invalid_actions"] == 1
    assert report["valid_actions"] == 0
    assert report["spent"] == 0.0
    assert report["decisions"][0]["accepted"] is False
    assert "unknown simulated action" in report["decisions"][0]["reason"]


class MutatingController:
    controller_id = "mutating-controller"

    async def choose(self, observation, *, feedback, seed):
        # A controller can mutate its copy, but not the evaluator's OmniCity state.
        for entity in observation["entities"]:
            entity["attributes"]["critical_power"] = "stable"
            entity["attributes"]["fuel"] = "stable"
        observation["goals"].clear()
        return ControllerDecision(action_id=FINISH_ACTION, model="fake-model")


@pytest.mark.asyncio
async def test_controller_cannot_mutate_scenario_truth_through_observation() -> None:
    report = await evaluate_file(
        HELD_OUT / "hospital_generator_fuel.json",
        MutatingController(),
        config=EvaluationConfig(max_requests=2, max_action_attempts=2),
    )

    assert report["complete"] is False
    assert report["goal_score"] == 0.0
    assert report["spent"] == 0.0


class CapturingProvider:
    def __init__(self) -> None:
        self.messages = None
        self.tools = None

    async def complete(self, messages, tools):
        self.messages = messages
        self.tools = tools
        return {
            "model": "fixed-test-model",
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "omni_city_action",
                            "arguments": json.dumps({"action_id": "connect-tanker"}),
                        },
                    }
                ],
            },
        }


@pytest.mark.asyncio
async def test_provider_controller_receives_only_simulator_decision_tool() -> None:
    provider = CapturingProvider()
    controller = ProviderController(provider, controller_id="fixed-test-model")
    report = await evaluate_file(
        HELD_OUT / "hospital_generator_fuel.json",
        controller,
        config=EvaluationConfig(max_requests=1, max_action_attempts=1, scenario_seed=9),
    )

    assert provider.tools is not None
    assert len(provider.tools) == 1
    assert provider.tools[0]["function"]["name"] == "omni_city_action"
    dumped = json.dumps(provider.tools)
    assert "terminal_run" not in dumped
    assert "file_write" not in dumped
    assert "mcp" not in dumped.lower()
    assert report["observed_models"] == ["fixed-test-model"]
    assert report["requests"] == 1


@pytest.mark.asyncio
async def test_scenario_and_config_hashes_are_reproducible() -> None:
    config = EvaluationConfig(max_requests=4, max_action_attempts=4, scenario_seed=42)
    first = await evaluate_file(
        HELD_OUT / "hospital_generator_fuel.json",
        SequenceController(["connect-tanker", "transfer-fuel"]),
        config=config,
    )
    second = await evaluate_file(
        HELD_OUT / "hospital_generator_fuel.json",
        SequenceController(["connect-tanker", "transfer-fuel"]),
        config=config,
    )

    assert first["scenario_hash"] == second["scenario_hash"]
    assert first["config_hash"] == second["config_hash"]
    assert first["goal_score"] == second["goal_score"]
    assert first["spent"] == second["spent"]
