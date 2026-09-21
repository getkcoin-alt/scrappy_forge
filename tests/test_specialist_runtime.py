import pytest

from scrappy_forge.model_router import ModelCapability, ModelRouter, PrivacyClass, TaskProfile
from scrappy_forge.reasoning_policy import ReasoningLevel
from scrappy_forge.specialist_runtime import (
    AgentBudget,
    AgentProposal,
    SpecialistRequest,
    SpecialistRole,
    SpecialistRuntime,
)
from scrappy_forge.util import ForgeError


def capability(provider="local", model="coder", reliability=1.0):
    return ModelCapability(
        provider=provider,
        model=model,
        context_tokens=64_000,
        output_tokens=16_000,
        structured_outputs=True,
        reasoning_levels=(ReasoningLevel.LOW, ReasoningLevel.MEDIUM, ReasoningLevel.HIGH),
        reliability=reliability,
        privacy=PrivacyClass.LOCAL_ONLY,
    )


def request(role=SpecialistRole.IMPLEMENTER, budget=None):
    return SpecialistRequest(
        mission_id="mission-1",
        task_id="task-1",
        role=role,
        objective="Produce a bounded proposal",
        context={"trust": "untrusted_data"},
        profile=TaskProfile(
            complexity=0.3,
            estimated_input_tokens=1000,
            estimated_output_tokens=500,
            privacy=PrivacyClass.PRIVATE,
        ),
        budget=budget or AgentBudget(),
    )


@pytest.mark.asyncio
async def test_specialist_returns_proposal_not_execution():
    router = ModelRouter([capability()])

    def invoke(req, route, turn):
        return AgentProposal(
            req.role,
            req.task_id,
            "patch proposal",
            operations=({"tool": "edit", "args": {}},),
        )

    result = await SpecialistRuntime(router, invoke).run(request())
    assert result.proposal.operations[0]["tool"] == "edit"
    assert not hasattr(result.proposal, "authorized")
    assert not hasattr(result.proposal, "verified")


@pytest.mark.asyncio
async def test_role_and_task_identity_are_enforced():
    router = ModelRouter([capability()])

    def invoke(req, route, turn):
        return AgentProposal(SpecialistRole.PLANNER, "other-task", "wrong identity")

    with pytest.raises(ForgeError):
        await SpecialistRuntime(router, invoke).run(request(budget=AgentBudget(max_turns=1)))


@pytest.mark.asyncio
async def test_context_budget_is_hard_bound():
    router = ModelRouter([capability()])

    def invoke(req, route, turn):
        raise AssertionError("must not invoke model")

    req = request(budget=AgentBudget(max_input_tokens=100, max_output_tokens=1000))
    with pytest.raises(ForgeError, match="input-token budget"):
        await SpecialistRuntime(router, invoke).run(req)


@pytest.mark.asyncio
async def test_failure_falls_back_to_another_model():
    router = ModelRouter([capability("a", "one", 1.0), capability("b", "two", 0.9)])
    seen = []

    def invoke(req, route, turn):
        seen.append(route.primary.key)
        if turn == 1:
            raise ForgeError("provider failed")
        return AgentProposal(req.role, req.task_id, "fallback proposal")

    result = await SpecialistRuntime(router, invoke).run(request())
    assert result.turns == 2
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_independent_reviewer_excludes_implementer_model():
    first = capability("a", "one", 1.0)
    second = capability("b", "two", 0.9)
    router = ModelRouter([first, second])

    def invoke(req, route, turn):
        return AgentProposal(req.role, req.task_id, "review findings")

    runtime = SpecialistRuntime(router, invoke)
    result = await runtime.run_reviewer(
        request(SpecialistRole.REVIEWER), implementer_model_key=first.key
    )
    assert result.route.primary.key == second.key


@pytest.mark.asyncio
async def test_journal_contains_metadata_not_hidden_reasoning():
    router = ModelRouter([capability()])
    events = []

    def invoke(req, route, turn):
        return AgentProposal(req.role, req.task_id, "bounded result")

    await SpecialistRuntime(
        router,
        invoke,
        journal=lambda kind, payload: events.append((kind, payload)),
    ).run(request())
    assert [kind for kind, _ in events] == ["specialist_started", "specialist_finished"]
    assert all(
        "reasoning" not in payload and "chain_of_thought" not in payload for _, payload in events
    )
