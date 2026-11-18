from scrappy_forge.compiled_intelligence import CompiledIntelligence


def test_compiler_groups_evidence_and_is_deterministic():
    compiler = CompiledIntelligence()
    state = {
        "status": "working",
        "steps": 4,
        "compactions": 2,
        "acceptance": ["unit tests pass"],
        "verification": [{"name": "unit", "passed": True}],
        "observations": [{"tool": "pytest", "error": None, "artifact": "abc"}],
    }
    memories = [
        {"id": 1, "content": "Always run unit tests before release", "memory_class": "procedural",
         "confidence": 0.95, "provenance": {"mission": "m1"}, "created_at": 10},
        {"id": 2, "content": "User prefers concise reports", "memory_class": "preference",
         "confidence": 1.0, "provenance": {"origin": "user"}, "created_at": 11},
        {"id": 3, "content": "Old stale note", "memory_class": "semantic",
         "confidence": 1.0, "stale": True, "created_at": 12},
    ]
    first = compiler.compile(task="release unit tests", state=state, memories=memories)
    second = compiler.compile(task="release unit tests", state=state, memories=memories)
    assert first == second
    assert first["procedures"][0]["id"] == 1
    assert first["preferences"][0]["id"] == 2
    assert "Old stale note" not in str(first)
    assert first["verification_evidence"] == [{"name": "unit", "passed": True}]
    assert len(first["digest"]) == 64


def test_compiler_preserves_untrusted_boundary_and_active_task():
    capsule = CompiledIntelligence().compile(
        task="Fix payment retry",
        state={"status": "working", "steps": 1},
        memories=[],
    )
    assert capsule["active_mission"]["task"] == "Fix payment retry"
    assert "untrusted evidence" in capsule["trust_boundary"]
