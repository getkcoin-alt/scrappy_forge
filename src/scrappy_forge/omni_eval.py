"""Held-out controller evaluation for Omni-City.

The evaluator exposes exactly one simulated decision capability to a controller:
choose an action id from the current Omni-City observation, or finish. It never
passes Forge host tools, workspace handles, shell access, network access, or the
mutable scenario object to the controller.

Reports are measurements of simulator performance only. They are not claims of
real-world autonomy, safety, or general intelligence.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from .omni_city import ActionRejected, OmniCity, load_scenario
from .util import encoded, sha

FINISH_ACTION = "__finish__"


@dataclass(slots=True, frozen=True)
class EvaluationConfig:
    max_requests: int = 16
    max_action_attempts: int = 12
    scenario_seed: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.max_requests <= 100:
            raise ValueError("max_requests must be between 1 and 100")
        if not 1 <= self.max_action_attempts <= 100:
            raise ValueError("max_action_attempts must be between 1 and 100")
        if not 0 <= self.scenario_seed <= 2**31 - 1:
            raise ValueError("scenario_seed must be a non-negative 32-bit integer")


@dataclass(slots=True)
class ControllerDecision:
    action_id: str | None
    model: str | None = None
    usage: dict[str, Any] | None = None
    error: str | None = None


class OmniController(Protocol):
    controller_id: str

    async def choose(
        self,
        observation: dict[str, Any],
        *,
        feedback: str | None,
        seed: int,
    ) -> ControllerDecision: ...


class ProviderController:
    """Adapter for Forge's OpenAI-compatible provider interface.

    The model receives one synthetic function tool, ``omni_city_action``. That
    tool is not registered with Forge and cannot reach the host. The evaluator
    interprets its argument as a simulator action id only.
    """

    def __init__(self, provider: Any, *, controller_id: str) -> None:
        self.provider = provider
        self.controller_id = controller_id

    async def choose(
        self,
        observation: dict[str, Any],
        *,
        feedback: str | None,
        seed: int,
    ) -> ControllerDecision:
        action_ids = [
            str(item.get("id"))
            for item in observation.get("available_actions", [])
            if isinstance(item, dict) and item.get("id")
        ]
        allowed = [*action_ids, FINISH_ACTION]
        tool = {
            "type": "function",
            "function": {
                "name": "omni_city_action",
                "description": (
                    "Choose exactly one action in the deterministic Omni-City simulation. "
                    "This has no real-world or host authority."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action_id": {
                            "type": "string",
                            "enum": allowed,
                        }
                    },
                    "required": ["action_id"],
                    "additionalProperties": False,
                },
            },
        }
        user_payload = {
            "benchmark": "omni-city",
            "simulated": True,
            "scenario_seed": seed,
            "feedback_from_previous_attempt": feedback,
            "observation": copy.deepcopy(observation),
            "instruction": (
                "Choose the next simulated action. Use __finish__ only when the goals "
                "are satisfied or no useful simulated action remains."
            ),
        }
        result = await self.provider.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "You are being evaluated inside a deterministic simulator. "
                        "You have no shell, network, filesystem, deployment or physical authority. "
                        "Select exactly one omni_city_action tool call from the supplied action ids."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(user_payload, separators=(",", ":"), default=str),
                },
            ],
            [tool],
        )
        message = result.get("message") if isinstance(result, dict) else None
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not isinstance(calls, list) or len(calls) != 1:
            return ControllerDecision(
                action_id=None,
                model=result.get("model") if isinstance(result, dict) else None,
                usage=result.get("usage") if isinstance(result, dict) else None,
                error="controller must emit exactly one omni_city_action tool call",
            )
        call = calls[0]
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or function.get("name") != "omni_city_action":
            return ControllerDecision(
                action_id=None,
                model=result.get("model"),
                usage=result.get("usage"),
                error="controller emitted an unexpected tool",
            )
        try:
            args = json.loads(function.get("arguments", ""))
        except (TypeError, json.JSONDecodeError):
            return ControllerDecision(
                action_id=None,
                model=result.get("model"),
                usage=result.get("usage"),
                error="controller emitted invalid JSON arguments",
            )
        action_id = args.get("action_id") if isinstance(args, dict) else None
        return ControllerDecision(
            action_id=str(action_id) if action_id is not None else None,
            model=result.get("model"),
            usage=result.get("usage"),
        )


def _usage_add(total: dict[str, float], usage: dict[str, Any] | None) -> None:
    if not isinstance(usage, dict):
        return
    for source, target in (
        ("prompt_tokens", "prompt_tokens"),
        ("completion_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
        ("cost", "reported_cost"),
    ):
        value = usage.get(source)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[target] = total.get(target, 0.0) + float(value)


async def evaluate_scenario(
    scenario: dict[str, Any],
    controller: OmniController,
    *,
    config: EvaluationConfig | None = None,
) -> dict[str, Any]:
    """Run one bounded controller episode and return a machine-readable report."""

    config = config or EvaluationConfig()
    city = OmniCity(scenario)
    scenario_hash = sha(encoded(city.scenario))
    config_hash = sha(encoded(asdict(config)))

    requests = 0
    action_attempts = 0
    valid_actions = 0
    invalid_actions = 0
    precondition_violations = 0
    budget_violations = 0
    replans = 0
    evaluator_interventions = 0
    feedback: str | None = None
    had_rejection = False
    decisions: list[dict[str, Any]] = []
    usage: dict[str, float] = {}
    observed_models: set[str] = set()
    stopped_because = "request_budget_exhausted"

    while requests < config.max_requests:
        score = city.score()
        if score["complete"]:
            stopped_because = "goals_complete"
            break
        if action_attempts >= config.max_action_attempts:
            stopped_because = "action_attempt_budget_exhausted"
            break

        observation = city.observe()
        snapshot_id = observation["snapshot_id"]
        decision = await controller.choose(
            copy.deepcopy(observation),
            feedback=feedback,
            seed=config.scenario_seed,
        )
        requests += 1
        _usage_add(usage, decision.usage)
        if decision.model:
            observed_models.add(decision.model)

        action_id = decision.action_id
        record: dict[str, Any] = {
            "request": requests,
            "snapshot_id": snapshot_id,
            "action_id": action_id,
            "controller_error": decision.error,
        }

        if decision.error or not action_id:
            invalid_actions += 1
            evaluator_interventions += 1
            feedback = decision.error or "No action id was supplied."
            had_rejection = True
            record["accepted"] = False
            record["reason"] = feedback
            decisions.append(record)
            continue

        if action_id == FINISH_ACTION:
            stopped_because = "controller_finished"
            record["accepted"] = True
            record["finish"] = True
            decisions.append(record)
            break

        action_attempts += 1
        known_actions = {
            str(item.get("id")) for item in observation.get("available_actions", []) if isinstance(item, dict)
        }
        if action_id not in known_actions:
            invalid_actions += 1
            evaluator_interventions += 1
            feedback = f"unknown simulated action: {action_id}"
            had_rejection = True
            record["accepted"] = False
            record["reason"] = feedback
            decisions.append(record)
            continue

        try:
            event = city.apply(action_id)
        except ActionRejected as exc:
            text = str(exc)
            invalid_actions += 1
            evaluator_interventions += 1
            if "preconditions not met" in text:
                precondition_violations += 1
            if "exceeds budget" in text:
                budget_violations += 1
            feedback = text
            had_rejection = True
            record["accepted"] = False
            record["reason"] = text
            decisions.append(record)
            continue

        valid_actions += 1
        if had_rejection:
            replans += 1
            had_rejection = False
        feedback = None
        record["accepted"] = True
        record["event"] = event
        decisions.append(record)

    final_score = city.score()
    return {
        "report_schema": "omni-city-eval.v0.1",
        "simulated": True,
        "real_world_authority": False,
        "scenario_id": city.scenario_id,
        "scenario_hash": scenario_hash,
        "scenario_seed": config.scenario_seed,
        "controller_id": controller.controller_id,
        "observed_models": sorted(observed_models),
        "config": asdict(config),
        "config_hash": config_hash,
        "stopped_because": stopped_because,
        "complete": final_score["complete"],
        "goal_score": final_score["goal_score"],
        "budget": final_score["budget"],
        "spent": final_score["spent"],
        "requests": requests,
        "action_attempts": action_attempts,
        "valid_actions": valid_actions,
        "invalid_actions": invalid_actions,
        "precondition_violations": precondition_violations,
        "budget_violations": budget_violations,
        "replans": replans,
        "evaluator_interventions": evaluator_interventions,
        "usage": usage,
        "score_evidence": final_score["evidence"],
        "decisions": decisions,
        "final_snapshot_id": city.observe()["snapshot_id"],
        "disclaimer": "Simulator benchmark result only; not evidence of real-world control or AGI.",
    }


async def evaluate_file(
    path: Path,
    controller: OmniController,
    *,
    config: EvaluationConfig | None = None,
) -> dict[str, Any]:
    return await evaluate_scenario(load_scenario(path), controller, config=config)
