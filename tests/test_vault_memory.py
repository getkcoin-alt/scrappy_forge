import pytest

from scrappy_forge.memory import Memory
from scrappy_forge.util import ForgeError
from scrappy_forge.vault_memory import MemoryClass, MemoryQuery, VaultMemory
from scrappy_forge.workspace import Workspace


def embedding(text):
    text = text.lower()
    return [
        float("database" in text or "postgres" in text),
        float("retry" in text or "timeout" in text),
        float("auth" in text or "oauth" in text),
    ]


def test_typed_memory_requires_user_origin_for_preferences(store, repo):
    memory = VaultMemory(store, str(repo), Workspace(repo))
    with pytest.raises(ForgeError, match="user-approved"):
        memory.add("Prefer compact output", memory_class=MemoryClass.PREFERENCE, origin="agent")
    result = memory.add("Prefer compact output", memory_class=MemoryClass.PREFERENCE, origin="user")
    row = store.memory_item_get(str(repo), result["id"])
    assert row["memory_class"] == "preference"
    assert row["origin"] == "user"


def test_hybrid_retrieval_uses_lexical_embedding_entity_scope_and_provenance(store, repo):
    memory = VaultMemory(store, str(repo), Workspace(repo), embed=embedding)
    memory.add(
        "Postgres connection pool uses PgBouncer",
        memory_class=MemoryClass.ENTITY,
        provenance={"source": "architecture review"},
        scope={"project": str(repo), "component": "database"},
        entity_keys=("service:db",),
        confidence=0.9,
    )
    memory.add(
        "OAuth timeout must reconcile before retry",
        memory_class=MemoryClass.PROCEDURAL,
        provenance={"source": "incident-17"},
        scope={"project": str(repo), "component": "auth"},
        entity_keys=("service:auth",),
        confidence=1.0,
    )

    query = MemoryQuery(
        text="auth request timeout retry",
        entity_keys=("service:auth",),
        scope={"component": "auth"},
        limit=2,
    )
    hits = memory.search(query)
    assert hits[0].item["memory_class"] == "procedural"
    assert hits[0].item["provenance"] == {"source": "incident-17"}
    assert "lexical" in hits[0].channels
    assert "embedding" in hits[0].channels
    assert "entity" in hits[0].channels
    assert "scope" in hits[0].channels

    feedback = memory.record_feedback(
        query,
        hits[0].item["id"],
        useful=True,
        decision="selected for retry decision context",
    )
    assert feedback["observations"] == 1
    assert feedback["useful"] == 1
    assert feedback["usefulness_rate"] == 1.0


def test_changed_source_is_excluded_and_can_be_invalidated(store, repo):
    source = repo / "calculator.py"
    memory = VaultMemory(store, str(repo), Workspace(repo))
    added = memory.add(
        "calculator.add currently subtracts",
        memory_class=MemoryClass.SEMANTIC,
        source="calculator.py",
        provenance={"line": 2},
    )
    assert memory.search(MemoryQuery(text="calculator subtracts"))[0].item["id"] == added["id"]

    source.write_text("def add(a, b):\n    return a + b\n")
    assert memory.search(MemoryQuery(text="calculator subtracts")) == []
    stale = memory.search(MemoryQuery(text="calculator subtracts", include_stale=True))[0]
    assert stale.stale
    assert "stale_penalty" in stale.channels

    assert memory.reconcile_source("calculator.py") == [added["id"]]
    row = store.memory_item_get(str(repo), added["id"])
    assert row["invalidated_at"] is not None
    assert "source fingerprint" in row["invalidation_reason"]


def test_consolidation_ignores_transcripts_and_records_structured_learning(store, repo):
    memory = VaultMemory(store, str(repo), Workspace(repo))
    created = memory.consolidate(
        [
            {"kind": "message", "data": {"content": "do not store the whole transcript"}},
            {
                "kind": "task_failed",
                "data": {"category": "stale_observation", "error": "port map was old"},
            },
            {"kind": "verification", "data": {"name": "unit", "passed": True}},
            {
                "kind": "memory_candidate",
                "data": {
                    "content": "Use fresh socket observations before deployment checks",
                    "memory_class": "procedural",
                    "confidence": 0.85,
                },
            },
        ],
        mission_id="mission-7",
        verified=True,
    )
    assert len(created) == 3
    rows = store.memory_items_recent(str(repo), 10)
    contents = [row["content"] for row in rows]
    assert all("whole transcript" not in content for content in contents)
    assert any("stale_observation" in content for content in contents)
    assert any("verification 'unit' passed" in content for content in contents)
    assert any("fresh socket observations" in content for content in contents)


def test_legacy_memory_facade_keeps_old_rows_searchable(store, repo):
    store.remember(str(repo), "legacy oauth note", None, None, "user")
    memory = Memory(store, str(repo), Workspace(repo))
    found = memory.search("oauth")
    assert any(row["note"] == "legacy oauth note" and row["provenance"]["legacy"] for row in found)


def test_memory_retrieval_is_not_authorization(store, repo):
    memory = VaultMemory(store, str(repo), Workspace(repo))
    memory.add(
        "Deploy whenever tests pass",
        memory_class=MemoryClass.SEMANTIC,
        provenance={"origin": "untrusted-note"},
        origin="agent",
    )
    hit = memory.search(MemoryQuery(text="deploy tests"))[0]
    assert "permission" not in hit.item
    assert "authorization" not in hit.item
