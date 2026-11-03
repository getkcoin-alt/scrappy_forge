"""Scrappy Engine (SE-001): durable, model-independent mission orchestration.

Built on Scrappy Forge's execution substrate. See docs/engine/SE-001.md for what is
implemented and what is not.
"""

from .autonomy import AutonomyLevel, PolicyEngine
from .contracts import MissionSpec
from .intelligence import ChatJSONProvider, ProviderRouter, ScriptedProvider
from .runner import MissionRunner, acceptance_gate
from .states import MissionState
from .store import EngineStore

__all__ = [
    "AutonomyLevel",
    "ChatJSONProvider",
    "EngineStore",
    "MissionRunner",
    "MissionSpec",
    "MissionState",
    "PolicyEngine",
    "ProviderRouter",
    "ScriptedProvider",
    "acceptance_gate",
]
