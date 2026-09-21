import pytest

from scrappy_forge.model_router import (
    ModelCapability,
    ModelRouter,
    PrivacyClass,
    TaskProfile,
)
from scrappy_forge.reasoning_policy import ReasoningLevel, ReasoningPolicy
from scrappy_forge.util import ForgeError


def model(provider, name, **kwargs):
    defaults = dict(
        context_tokens=128000,
        output_tokens=16000,
        tool_support=True,
        structured_outputs=True,
        reasoning_levels=(
            ReasoningLevel.LOW,
            ReasoningLevel.MEDIUM,
            ReasoningLevel.HIGH,
            ReasoningLevel.EXTRA_HIGH,
        ),
        reliability=0.95,
        latency_ms_p50=1000,
        privacy=PrivacyClass.PRIVATE,
    )
    defaults.update(kwargs)
    return ModelCapability(provider=provider, model=name, **defaults)


def test_router_prefers_reliable_cheap_fast_model():
    router = ModelRouter(
        [
            model("a", "slow", latency_ms_p50=8000, input_cost_per_million=10),
            model("b", "fast", latency_ms_p50=500, input_cost_per_million=0.1),
        ]
    )
    decision = router.route(TaskProfile(complexity=0.5, requires_tools=True, estimated_input_tokens=2000))
    assert decision.primary.model == "fast"
    assert decision.fallbacks[0].model == "slow"


def test_router_enforces_privacy_and_capabilities():
    router = ModelRouter(
        [
            model("cloud", "public", privacy=PrivacyClass.PUBLIC),
            model("local", "private", privacy=PrivacyClass.LOCAL_ONLY, computer_use=True),
        ]
    )
    decision = router.route(
        TaskProfile(
            complexity=0.4,
            privacy=PrivacyClass.LOCAL_ONLY,
            requires_computer_use=True,
        )
    )
    assert decision.primary.provider == "local"


def test_router_filters_models_that_cannot_fit_context():
    router = ModelRouter([model("a", "small", context_tokens=1000)])
    with pytest.raises(ForgeError, match="No model"):
        router.route(TaskProfile(complexity=0.4, estimated_input_tokens=2000))


def test_failure_history_causes_fallback_without_losing_task_state():
    primary = model("a", "primary", latency_ms_p50=100)
    fallback = model("b", "fallback", latency_ms_p50=500)
    router = ModelRouter([primary, fallback])
    task = TaskProfile(complexity=0.4)
    assert router.route(task).primary.key == primary.key
    for _ in range(3):
        router.mark_failure(primary.key)
    assert router.route(task).primary.key == fallback.key


def test_critical_task_selects_independent_reviewer():
    router = ModelRouter([model("a", "builder"), model("b", "reviewer", latency_ms_p50=1200)])
    decision = router.route(TaskProfile(complexity=0.9, uncertainty=0.8, critical=True))
    assert decision.reviewer is not None
    assert decision.reviewer.key != decision.primary.key


def test_reasoning_policy_escalates_after_failures():
    policy = ReasoningPolicy()
    initial = policy.choose(complexity=0.55, uncertainty=0.4, verification_failures=0)
    escalated = policy.choose(complexity=0.55, uncertainty=0.4, verification_failures=3)
    order = {
        ReasoningLevel.LOW: 0,
        ReasoningLevel.MEDIUM: 1,
        ReasoningLevel.HIGH: 2,
        ReasoningLevel.EXTRA_HIGH: 3,
    }
    assert order[escalated.level] > order[initial.level]
