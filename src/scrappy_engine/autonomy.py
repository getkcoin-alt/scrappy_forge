"""Autonomy levels and the policy engine.

The model proposes; this module decides. A permission is granted only when BOTH the
mission's autonomy level and the acting agent role allow the scope. Prompts are not
part of the decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class AutonomyLevel(IntEnum):
    A0 = 0  # read and analyze
    A1 = 1  # edit isolated workspace
    A2 = 2  # run tests
    A3 = 3  # branches and commits (inside the mission worktree only)
    A4 = 4  # deploy to isolated staging
    A5 = 5  # approved production targets


# Minimum autonomy level required for each permission scope.
SCOPE_LEVEL: dict[str, AutonomyLevel] = {
    "fs.read": AutonomyLevel.A0,
    "fs.write": AutonomyLevel.A1,
    "test.run": AutonomyLevel.A2,
    "git.commit": AutonomyLevel.A3,
    "deploy.staging": AutonomyLevel.A4,
    "deploy.production": AutonomyLevel.A5,
}

# Separation of duties: what each role may do. The orchestrator ("director") runs
# integration; reviewers and architects are read-only; only QA runs checks so the
# engineer cannot be the sole producer of its own acceptance evidence.
ROLE_SCOPES: dict[str, frozenset[str]] = {
    "director": frozenset({"fs.read", "git.commit"}),
    "architect": frozenset({"fs.read"}),
    "engineer": frozenset({"fs.read", "fs.write"}),
    "debugger": frozenset({"fs.read"}),
    "qa": frozenset({"fs.read", "test.run"}),
    "reviewer": frozenset({"fs.read"}),
    "devops": frozenset({"fs.read", "deploy.staging"}),
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


class PolicyEngine:
    def __init__(self, level: AutonomyLevel):
        self.level = level

    def decide(self, role: str, scope: str) -> Decision:
        required = SCOPE_LEVEL.get(scope)
        if required is None:
            return Decision(False, f"unknown permission scope {scope!r}")
        if scope not in ROLE_SCOPES.get(role, frozenset()):
            return Decision(False, f"role {role!r} is not granted {scope!r}")
        if self.level < required:
            return Decision(False, f"{scope!r} requires {required.name}; mission runs at {self.level.name}")
        return Decision(True, f"{role} granted {scope} at {self.level.name}")
