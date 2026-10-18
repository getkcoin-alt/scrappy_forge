from pathlib import Path

import pytest

from scrappy_forge.omni_city import ActionRejected, OmniCity, load_scenario, run_sequence

SCENARIOS = Path(__file__).parents[1] / "benchmarks" / "omni_city_v0" / "scenarios"


def scenario(name: str):
    return load_scenario(SCENARIOS / name)


def test_water_continuity_requires_safe_order() -> None:
    city = OmniCity(scenario("water_continuity.json"))

    with pytest.raises(ActionRejected, match="preconditions not met"):
        city.apply("activate-backup")

    city.apply("isolate-primary")
    city.apply("activate-backup")
    score = city.score()

    assert score["complete"] is True
    assert score["goal_score"] == 1.0
    assert score["spent"] == 3.0


def test_hospital_power_plan_reaches_all_goals() -> None:
    result = run_sequence(
        scenario("hospital_power.json"),
        ["isolate-feeder", "start-generator", "restore-coldstore"],
    )

    assert result["score"]["complete"] is True
    assert result["score"]["goal_score"] == 1.0
    assert len(result["events"]) == 3
    assert all(event["simulated"] is True for event in result["events"])


def test_logistics_plan_is_scored_from_final_world_state() -> None:
    result = run_sequence(
        scenario("logistics_road_closure.json"),
        ["reroute-bypass", "complete-delivery"],
    )

    assert result["score"]["complete"] is True
    assert result["score"]["spent"] == 4.0
    assert result["final"]["simulated"] is True
    assert result["final"]["provenance"]["scope"] == "simulation_only"


def test_observation_is_deterministic_until_state_changes() -> None:
    city = OmniCity(scenario("water_continuity.json"))
    first = city.observe()
    second = city.observe()
    assert first["snapshot_id"] == second["snapshot_id"]

    city.apply("isolate-primary")
    assert city.observe()["snapshot_id"] != first["snapshot_id"]


def test_unknown_actions_never_become_implicit_capabilities() -> None:
    city = OmniCity(scenario("water_continuity.json"))
    with pytest.raises(ActionRejected, match="unknown simulated action"):
        city.apply("invent-a-new-tool")
