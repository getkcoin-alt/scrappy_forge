"""Omni-City v0: deterministic world-model benchmark environment.

This module is deliberately a simulator, not a real-world control interface.
It gives an agent a machine-readable changing world with explicit actions,
preconditions, costs and goals so planning/recovery can be measured without
pretending a language model controls physical infrastructure.

The scenario format is JSON-serializable and intentionally small. Real sensors,
robots, utilities or city systems are future adapters; they must never reuse the
simulator's authority semantics by accident.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import ForgeError, encoded, sha

SCHEMA_VERSION = "omni-city.v0"
MAX_ENTITIES = 2_000
MAX_RELATIONS = 5_000
MAX_ACTIONS = 500
MAX_GOALS = 200


class ScenarioError(ForgeError):
    """A scenario is malformed or violates simulator invariants."""


class ActionRejected(ForgeError):
    """A simulated action failed a precondition or budget constraint."""


def _require_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ScenarioError(f"{name} is required")
    return text


def _entity_index(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entities = value.get("entities")
    if not isinstance(entities, list) or not entities:
        raise ScenarioError("entities must be a non-empty list")
    if len(entities) > MAX_ENTITIES:
        raise ScenarioError(f"entities exceed limit {MAX_ENTITIES}")

    out: dict[str, dict[str, Any]] = {}
    for raw in entities:
        if not isinstance(raw, dict):
            raise ScenarioError("every entity must be an object")
        entity_id = _require_text(raw.get("id"), "entity.id")
        kind = _require_text(raw.get("kind"), f"entity {entity_id} kind")
        if entity_id in out:
            raise ScenarioError(f"duplicate entity id: {entity_id}")
        attributes = raw.get("attributes", {})
        if not isinstance(attributes, dict):
            raise ScenarioError(f"entity {entity_id} attributes must be an object")
        out[entity_id] = {"id": entity_id, "kind": kind, "attributes": copy.deepcopy(attributes)}
    return out


def _conditions(raw: Any, *, name: str, entities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ScenarioError(f"{name} must be a list")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ScenarioError(f"{name} entries must be objects")
        entity = _require_text(item.get("entity"), f"{name}.entity")
        attribute = _require_text(item.get("attribute"), f"{name}.attribute")
        if entity not in entities:
            raise ScenarioError(f"{name} references unknown entity: {entity}")
        if "equals" not in item:
            raise ScenarioError(f"{name} condition requires equals")
        out.append({"entity": entity, "attribute": attribute, "equals": copy.deepcopy(item["equals"])})
    return out


def validate_scenario(value: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one Omni-City v0 scenario."""

    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ScenarioError(f"schema_version must be {SCHEMA_VERSION}")

    scenario_id = _require_text(value.get("scenario_id"), "scenario_id")
    title = _require_text(value.get("title"), "title")
    entities = _entity_index(value)

    relations = value.get("relations", [])
    if not isinstance(relations, list) or len(relations) > MAX_RELATIONS:
        raise ScenarioError("relations must be a bounded list")
    normalized_relations: list[dict[str, str]] = []
    for item in relations:
        if not isinstance(item, dict):
            raise ScenarioError("relation entries must be objects")
        source = _require_text(item.get("source"), "relation.source")
        relation = _require_text(item.get("relation"), "relation.relation")
        target = _require_text(item.get("target"), "relation.target")
        if source not in entities or target not in entities:
            raise ScenarioError(f"relation references unknown entity: {source} -> {target}")
        normalized_relations.append({"source": source, "relation": relation, "target": target})

    raw_goals = value.get("goals")
    if not isinstance(raw_goals, list) or not raw_goals or len(raw_goals) > MAX_GOALS:
        raise ScenarioError("goals must be a non-empty bounded list")
    goals: list[dict[str, Any]] = []
    for item in raw_goals:
        if not isinstance(item, dict):
            raise ScenarioError("goal entries must be objects")
        conditions = _conditions([item], name="goal", entities=entities)
        weight = item.get("weight", 1)
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
            raise ScenarioError("goal weight must be positive")
        goals.append({**conditions[0], "weight": float(weight)})

    raw_actions = value.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions or len(raw_actions) > MAX_ACTIONS:
        raise ScenarioError("actions must be a non-empty bounded list")
    actions: dict[str, dict[str, Any]] = {}
    for raw in raw_actions:
        if not isinstance(raw, dict):
            raise ScenarioError("action entries must be objects")
        action_id = _require_text(raw.get("id"), "action.id")
        if action_id in actions:
            raise ScenarioError(f"duplicate action id: {action_id}")
        cost = raw.get("cost", 0)
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0:
            raise ScenarioError(f"action {action_id} cost must be non-negative")
        requires = _conditions(raw.get("requires", []), name=f"action {action_id} requires", entities=entities)
        effects = _conditions(raw.get("effects", []), name=f"action {action_id} effects", entities=entities)
        if not effects:
            raise ScenarioError(f"action {action_id} must have at least one effect")
        actions[action_id] = {
            "id": action_id,
            "description": _require_text(raw.get("description"), f"action {action_id} description"),
            "cost": float(cost),
            "requires": requires,
            "effects": effects,
        }

    budget = value.get("budget", 0)
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget < 0:
        raise ScenarioError("budget must be non-negative")

    return {
        "schema_version": SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "title": title,
        "description": str(value.get("description") or "").strip(),
        "budget": float(budget),
        "entities": entities,
        "relations": normalized_relations,
        "goals": goals,
        "actions": actions,
    }


def load_scenario(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScenarioError(f"could not load scenario: {exc}") from exc
    return validate_scenario(value)


def _matches(entity: dict[str, Any], condition: dict[str, Any]) -> bool:
    return entity["attributes"].get(condition["attribute"]) == condition["equals"]


@dataclass(slots=True)
class OmniCity:
    """Mutable in-memory state for one deterministic simulation episode."""

    scenario: dict[str, Any]
    spent: float = 0.0
    step: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.scenario = copy.deepcopy(validate_scenario(self.scenario))

    @property
    def scenario_id(self) -> str:
        return self.scenario["scenario_id"]

    def observe(self) -> dict[str, Any]:
        """Return an evidence-shaped, explicitly simulated world snapshot."""

        public = {
            "schema_version": SCHEMA_VERSION,
            "scenario_id": self.scenario_id,
            "simulated": True,
            "observation_mode": "deterministic_simulation",
            "step": self.step,
            "budget": self.scenario["budget"],
            "spent": self.spent,
            "entities": list(copy.deepcopy(self.scenario["entities"]).values()),
            "relations": copy.deepcopy(self.scenario["relations"]),
            "available_actions": [
                {
                    "id": action["id"],
                    "description": action["description"],
                    "cost": action["cost"],
                }
                for action in self.scenario["actions"].values()
            ],
            "goals": copy.deepcopy(self.scenario["goals"]),
            "provenance": {
                "source": f"omni-city:{self.scenario_id}",
                "confidence": 1.0,
                "scope": "simulation_only",
            },
        }
        public["snapshot_id"] = sha(encoded(public))
        return public

    def apply(self, action_id: str) -> dict[str, Any]:
        action = self.scenario["actions"].get(action_id)
        if action is None:
            raise ActionRejected(f"unknown simulated action: {action_id}")
        if self.spent + action["cost"] > self.scenario["budget"]:
            raise ActionRejected(
                f"action {action_id} exceeds budget: {self.spent + action['cost']} > {self.scenario['budget']}"
            )

        unmet = [
            condition
            for condition in action["requires"]
            if not _matches(self.scenario["entities"][condition["entity"]], condition)
        ]
        if unmet:
            detail = ", ".join(
                f"{c['entity']}.{c['attribute']} == {c['equals']!r}" for c in unmet
            )
            raise ActionRejected(f"preconditions not met for {action_id}: {detail}")

        before = self.observe()["snapshot_id"]
        changes: list[dict[str, Any]] = []
        for effect in action["effects"]:
            entity = self.scenario["entities"][effect["entity"]]
            attribute = effect["attribute"]
            old = copy.deepcopy(entity["attributes"].get(attribute))
            entity["attributes"][attribute] = copy.deepcopy(effect["equals"])
            changes.append(
                {
                    "entity": effect["entity"],
                    "attribute": attribute,
                    "before": old,
                    "after": copy.deepcopy(effect["equals"]),
                }
            )

        self.spent += action["cost"]
        self.step += 1
        after = self.observe()["snapshot_id"]
        event = {
            "step": self.step,
            "action_id": action_id,
            "cost": action["cost"],
            "changes": changes,
            "before_snapshot": before,
            "after_snapshot": after,
            "simulated": True,
        }
        self.events.append(event)
        return copy.deepcopy(event)

    def score(self) -> dict[str, Any]:
        achieved = 0.0
        total = sum(goal["weight"] for goal in self.scenario["goals"])
        evidence: list[dict[str, Any]] = []
        for goal in self.scenario["goals"]:
            entity = self.scenario["entities"][goal["entity"]]
            ok = _matches(entity, goal)
            if ok:
                achieved += goal["weight"]
            evidence.append(
                {
                    "entity": goal["entity"],
                    "attribute": goal["attribute"],
                    "target": copy.deepcopy(goal["equals"]),
                    "observed": copy.deepcopy(entity["attributes"].get(goal["attribute"])),
                    "weight": goal["weight"],
                    "achieved": ok,
                }
            )
        return {
            "scenario_id": self.scenario_id,
            "goal_score": round(achieved / total, 6) if total else 0.0,
            "goals_achieved_weight": achieved,
            "goals_total_weight": total,
            "budget": self.scenario["budget"],
            "spent": self.spent,
            "steps": self.step,
            "complete": achieved == total,
            "evidence": evidence,
        }


def run_sequence(scenario: dict[str, Any], action_ids: list[str]) -> dict[str, Any]:
    """Run a candidate plan and return score + auditable simulation events."""

    city = OmniCity(scenario)
    for action_id in action_ids:
        city.apply(action_id)
    return {"score": city.score(), "events": copy.deepcopy(city.events), "final": city.observe()}
