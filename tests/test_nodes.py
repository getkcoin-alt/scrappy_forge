from __future__ import annotations

import json
from pathlib import Path

import pytest

from scrappy_forge.nodes import get_node, intelligence_summary, list_nodes, load_registry
from scrappy_forge.util import ForgeError


def test_registry_contains_historical_and_current_nodes():
    registry = load_registry()
    ids = {node["id"] for node in registry["nodes"]}
    assert {
        "scrappy",
        "ssn-91x",
        "ssn-92c",
        "ssn-93l",
        "ssn-zeta-core",
        "vault-zeta",
        "scrappy-os-node",
        "scrappy-forge-node",
        "command-center",
        "syncbond",
        "world-model",
        "dreamcore",
        "forge",
        "mycelium",
        "nulllayer",
        "blackwind",
        "tri",
        "brahma",
        "vishnu",
        "mahesh",
        "omni-city",
        "superior-evaluator",
        "neural-canopy",
        "capability-broker",
    } <= ids


def test_historical_aliases_do_not_gain_authority():
    assert get_node("SSN-SOVEREIGN-001")["authority"] == "none"
    kalki = get_node("Kalki")
    assert kalki["id"] == "ssn-93l"
    assert kalki["authority"] == "observe-and-request-by-default"


def test_blackwind_is_permissioned_recon_not_bypass():
    node = get_node("blackwind")
    assert node["class"] == "recon-sensor"
    assert "authorized" in node["role"].lower()
    assert "authorized-testing-only" in node["authority"]


def test_brahma_vishnu_mahesh_have_no_tool_authority():
    for node_id in ("brahma", "vishnu", "mahesh"):
        assert get_node(node_id)["authority"] == "reasoning-only-no-tools"


def test_null_layer_encodes_uncertainty_not_invented_state():
    node = get_node("nulllayer")
    assert "unknown" in node["role"].lower()
    assert "invented certainty" in node["role"].lower()


def test_filters_and_summary_are_deterministic():
    vault = list_nodes(owner="vault-zeta")
    assert vault
    assert all(node["owner_component"] == "vault-zeta" for node in vault)
    summary = intelligence_summary()
    assert summary["node_count"] >= 24
    assert summary["mission"].startswith("Build intelligence that increases human capability")


def test_duplicate_node_ids_fail_closed(tmp_path: Path):
    registry = load_registry()
    registry["nodes"].append(dict(registry["nodes"][0]))
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(ForgeError, match="Duplicate node id"):
        load_registry(path)


def test_unknown_node_fails_closed():
    with pytest.raises(ForgeError, match="Unknown Scrappy node"):
        get_node("phantom-root-node")
