"""Model-independent intelligence layer.

A provider is anything that turns (role, brief, output schema) into a JSON object.
Scrappy owns the roles, the schemas, routing, budgets and validation; providers are
replaceable. No provider output is trusted until it validates against the role's
output contract.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from scrappy_forge.util import ForgeError

from .contracts import ARCHITECT_OUTPUT, DEBUGGER_OUTPUT, ENGINEER_OUTPUT, REVIEWER_OUTPUT, validate


@dataclass(frozen=True)
class Role:
    name: str
    charter: str
    output_schema: dict | None  # None: the role is deterministic and calls no model


ROLES: dict[str, Role] = {
    "architect": Role(
        "architect",
        "Study the objective, acceptance criteria, repository listing, relevant files and the baseline check"
        " output. Identify the most likely root cause or design, the files that must change and ordered steps."
        " State assumptions explicitly; do not claim anything you have not seen in the provided evidence.",
        ARCHITECT_OUTPUT,
    ),
    "engineer": Role(
        "engineer",
        "Implement the plan as concrete file edits. Use 'replace' for small exact edits and 'write' for whole"
        " files. Add or update regression tests when the objective asks for them. Never edit check commands or"
        " weaken tests to make them pass.",
        ENGINEER_OUTPUT,
    ),
    "debugger": Role(
        "debugger",
        "Given failing check output and the current patch, find the root cause of the failure and the single"
        " most useful next step. Cite the evidence lines you relied on.",
        DEBUGGER_OUTPUT,
    ),
    "reviewer": Role(
        "reviewer",
        "Independently review the patch against the objective and acceptance criteria. You did not write it."
        " Look for incorrect fixes, weakened or deleted tests, security problems and unrelated changes. Use"
        " 'blocking' only for defects that must be fixed before acceptance.",
        REVIEWER_OUTPUT,
    ),
    "qa": Role("qa", "Run the mission's declared checks on a fresh copy and report raw results.", None),
    "director": Role(
        "director", "Own the mission state, budgets and the final acceptance decision. Deterministic.", None
    ),
}


@dataclass(frozen=True)
class ProviderReply:
    output: dict
    cost_usd: float = 0.0


class IntelligenceProvider(Protocol):
    name: str
    family: str

    async def complete(self, role: Role, brief: dict) -> ProviderReply: ...


@dataclass
class ScriptedProvider:
    """Deterministic provider for tests, evaluations and offline dry runs.

    It proves the *mechanism* (contracts, gates, recovery), never model capability.
    Each role maps to a list of responses consumed in order; a response may be a
    callable taking the brief, which lets a script react to real repository content.
    """

    name: str
    responses: dict[str, list]
    family: str = "scripted"
    cost_per_call: float = 0.0
    calls: list[tuple[str, dict]] = field(default_factory=list)

    async def complete(self, role: Role, brief: dict) -> ProviderReply:
        self.calls.append((role.name, brief))
        queue = self.responses.get(role.name) or []
        if not queue:
            raise ForgeError(f"Scripted provider {self.name} has no response for {role.name}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        value = item(brief) if callable(item) else item
        if inspect.isawaitable(value):
            value = await value
        return ProviderReply(json.loads(json.dumps(value)), self.cost_per_call)


def extract_json(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ForgeError("Model reply contained no JSON object")
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ForgeError("Model reply JSON was malformed") from exc
    if not isinstance(value, dict):
        raise ForgeError("Model reply must be a JSON object")
    return value


class ChatJSONProvider:
    """Adapter over any OpenAI-compatible chat provider (scrappy_forge.providers.ChatProvider).

    Uses the existing Forge provider so OpenRouter-free and loopback/local models work
    without new credentials handling. Not exercised against a live model in CI.
    """

    def __init__(self, chat, *, name: str, family: str):
        self.chat = chat
        self.name = name
        self.family = family

    async def complete(self, role: Role, brief: dict) -> ProviderReply:
        if role.output_schema is None:
            raise ForgeError(f"Role {role.name} does not use a model")
        system = (
            f"You are the {role.name} in Scrappy Engine, an autonomous software-engineering system.\n"
            f"{role.charter}\n"
            "Reply with exactly one JSON object and nothing else. It must validate against this JSON Schema:\n"
            + json.dumps(role.output_schema)
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(brief, ensure_ascii=False)[:200_000]},
        ]
        reply = await self.chat.complete(messages, [])
        content = reply["message"].get("content") or ""
        cost = float((reply.get("usage") or {}).get("cost") or 0.0)
        return ProviderReply(extract_json(content), cost)


class ProviderRouter:
    """Capability/failure/diversity routing across interchangeable providers.

    Routing is a preference order per role plus failover. For the reviewer, a provider
    from a different family than the engineer's is preferred (diversity routing); if
    none is configured the report records that review independence was role-only.
    """

    def __init__(
        self, providers: list[IntelligenceProvider], preferences: dict[str, list[str]] | None = None
    ):
        if not providers:
            raise ForgeError("At least one intelligence provider is required")
        self.providers = {p.name: p for p in providers}
        if len(self.providers) != len(providers):
            raise ForgeError("Provider names must be unique")
        self.preferences = preferences or {}
        self.failures: dict[str, int] = {}

    def order(self, role: str, *, avoid_family: str | None = None) -> list[IntelligenceProvider]:
        names = list(self.preferences.get(role) or self.providers)
        names += [n for n in self.providers if n not in names]
        ranked = sorted(
            (self.providers[n] for n in names if n in self.providers),
            key=lambda p: (
                avoid_family is not None and p.family == avoid_family,
                self.failures.get(p.name, 0),
                names.index(p.name),
            ),
        )
        return ranked

    def record_failure(self, name: str) -> None:
        self.failures[name] = self.failures.get(name, 0) + 1

    def catalogue(self) -> list[dict]:
        return [
            {"name": p.name, "family": p.family, "failures": self.failures.get(p.name, 0)}
            for p in self.providers.values()
        ]


def check_output(role: Role, output: dict) -> dict:
    if role.output_schema is None:
        raise ForgeError(f"Role {role.name} has no output contract")
    validate(role.output_schema, output, f"{role.name} output")
    return output
