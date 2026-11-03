"""Typed contracts exchanged between layers.

Agents never pass chat transcripts to each other. They exchange these validated
structures, and every model output is checked against a JSON Schema before the
orchestrator looks at it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from jsonschema import Draft202012Validator

from scrappy_forge.util import ForgeError, encoded, sha

from .autonomy import AutonomyLevel

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,64}$"},
        "argv": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {"type": "string", "minLength": 1, "maxLength": 4000},
        },
        "timeout": {"type": "integer", "minimum": 1, "maximum": 3600},
    },
    "required": ["name", "argv"],
    "additionalProperties": False,
}

MISSION_SCHEMA = {
    "type": "object",
    "properties": {
        "project_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "objective": {"type": "string", "minLength": 3, "maxLength": 8000},
        "execution_mode": {"enum": ["autonomous"]},
        "environment": {"enum": ["sandbox", "trusted-local"]},
        "autonomy_level": {"enum": [level.name for level in AutonomyLevel]},
        "constraints": {
            "type": "object",
            "properties": {
                "max_parallel_agents": {"type": "integer", "minimum": 1, "maximum": 16},
                "max_retries": {"type": "integer", "minimum": 0, "maximum": 10},
                "budget_limit_usd": {"type": "number", "minimum": 0, "maximum": 10000},
                "max_model_calls": {"type": "integer", "minimum": 1, "maximum": 500},
                "allow_production_deploy": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "acceptance_criteria": {
            "type": "array",
            "maxItems": 50,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "checks": {"type": "array", "minItems": 1, "maxItems": 20, "items": CHECK_SCHEMA},
    },
    "required": ["project_id", "objective", "checks"],
    "additionalProperties": False,
}


def validate(schema: dict, value, what: str) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
    if errors:
        where = "/".join(str(p) for p in errors[0].path) or "<root>"
        raise ForgeError(f"{what} failed schema validation at {where}: {errors[0].message[:300]}")


@dataclass(frozen=True)
class MissionConstraints:
    max_parallel_agents: int = 1
    max_retries: int = 2
    budget_limit_usd: float = 5.0
    max_model_calls: int = 20
    allow_production_deploy: bool = False


@dataclass(frozen=True)
class CheckSpec:
    name: str
    argv: tuple[str, ...]
    timeout: int = 120

    def to_dict(self) -> dict:
        return {"name": self.name, "argv": list(self.argv), "timeout": self.timeout}


@dataclass(frozen=True)
class MissionSpec:
    project_id: str
    objective: str
    checks: tuple[CheckSpec, ...]
    execution_mode: str = "autonomous"
    environment: str = "sandbox"
    autonomy_level: AutonomyLevel = AutonomyLevel.A3
    constraints: MissionConstraints = field(default_factory=MissionConstraints)
    acceptance_criteria: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict) -> "MissionSpec":
        validate(MISSION_SCHEMA, raw, "Mission request")
        constraints = MissionConstraints(**(raw.get("constraints") or {}))
        if constraints.allow_production_deploy:
            # A5 needs a preconfigured, explicitly trusted deployment target. No such
            # adapter exists yet, so the request is refused instead of silently ignored.
            raise ForgeError("Production deployment (A5) is not available in this engine version")
        names = [check["name"] for check in raw["checks"]]
        if len(set(names)) != len(names):
            raise ForgeError("Check names must be unique")
        return cls(
            project_id=raw["project_id"],
            objective=raw["objective"].strip(),
            checks=tuple(
                CheckSpec(c["name"], tuple(c["argv"]), int(c.get("timeout", 120))) for c in raw["checks"]
            ),
            execution_mode=raw.get("execution_mode", "autonomous"),
            environment=raw.get("environment", "sandbox"),
            autonomy_level=AutonomyLevel[raw.get("autonomy_level", "A3")],
            constraints=constraints,
            acceptance_criteria=tuple(raw.get("acceptance_criteria") or ()),
        )

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "objective": self.objective,
            "execution_mode": self.execution_mode,
            "environment": self.environment,
            "autonomy_level": self.autonomy_level.name,
            "constraints": asdict(self.constraints),
            "acceptance_criteria": list(self.acceptance_criteria),
            "checks": [c.to_dict() for c in self.checks],
        }

    def request_hash(self) -> str:
        return sha(encoded(self.to_dict()))


# ---- Agent output contracts -------------------------------------------------

_PATH = {"type": "string", "minLength": 1, "maxLength": 400}

ARCHITECT_OUTPUT = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
        "root_cause_hypothesis": {"type": "string", "maxLength": 4000},
        "files": {"type": "array", "maxItems": 20, "items": _PATH},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "risks": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 1000}},
        "assumptions": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 1000}},
    },
    "required": ["summary", "files", "steps"],
    "additionalProperties": False,
}

ENGINEER_OUTPUT = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
        "edits": {
            "type": "array",
            "minItems": 1,
            "maxItems": 30,
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {
                            "op": {"const": "write"},
                            "path": _PATH,
                            "content": {"type": "string", "maxLength": 400000},
                        },
                        "required": ["op", "path", "content"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "op": {"const": "replace"},
                            "path": _PATH,
                            "old": {"type": "string", "minLength": 1, "maxLength": 100000},
                            "new": {"type": "string", "maxLength": 100000},
                        },
                        "required": ["op", "path", "old", "new"],
                        "additionalProperties": False,
                    },
                ]
            },
        },
    },
    "required": ["summary", "edits"],
    "additionalProperties": False,
}

DEBUGGER_OUTPUT = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string", "minLength": 1, "maxLength": 4000},
        "evidence": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 2000}},
        "next_step": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
    "required": ["root_cause", "next_step"],
    "additionalProperties": False,
}

REVIEWER_OUTPUT = {
    "type": "object",
    "properties": {
        "verdict": {"enum": ["approve", "request_changes"]},
        "findings": {
            "type": "array",
            "maxItems": 30,
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"enum": ["info", "warning", "blocking"]},
                    "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
                },
                "required": ["severity", "summary"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "findings"],
    "additionalProperties": False,
}
