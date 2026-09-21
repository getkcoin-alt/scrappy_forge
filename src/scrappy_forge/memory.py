from __future__ import annotations

from .util import ForgeError, sha
from .vault_memory import MemoryClass, MemoryQuery, VaultMemory


class Memory:
    """Compatibility facade used by the existing Engine.

    New writes go to typed Vault Zeta memory. Legacy rows remain searchable so existing
    installations do not lose useful notes during migration.
    """

    def __init__(self, store, project: str, workspace, *, embed=None):
        self.store, self.project, self.workspace = store, project, workspace
        self.vault = VaultMemory(store, project, workspace, embed=embed)

    def add(self, note: str, source: str | None = None, origin="agent"):
        return self.vault.add(
            note,
            memory_class=MemoryClass.SEMANTIC,
            provenance={"origin": origin, "compatibility_api": True},
            source=source,
            origin=origin,
        )

    def search(self, query="", *, include_stale=False):
        results = []
        seen = set()

        if query.strip():
            for hit in self.vault.search(MemoryQuery(text=query, limit=8, include_stale=include_stale)):
                row = dict(hit.item)
                row["retrieval_score"] = hit.score
                row["retrieval_channels"] = list(hit.channels)
                results.append(row)
                seen.add(("v2", row["id"]))
        else:
            for row in self.store.memory_items_recent(self.project, 8):
                stale = self.vault._stale(row)
                if stale and not include_stale:
                    continue
                row = dict(row)
                row["note"] = row["content"]
                row["stale"] = stale
                results.append(row)
                seen.add(("v2", row["id"]))

        # Preserve older note rows until an explicit migration is introduced.
        for row in self.store.memories(self.project, query, limit=max(0, 8 - len(results))):
            stale = False
            if row["source"]:
                try:
                    stale = sha(self.workspace.path(row["source"]).read_bytes()) != row["source_hash"]
                except (OSError, ForgeError):
                    stale = True
            row["stale"] = stale
            row["memory_class"] = MemoryClass.SEMANTIC.value
            row["provenance"] = {"legacy": True, "origin": row.get("origin")}
            if include_stale or not stale:
                results.append(row)
        return results[:8]

    def search_hybrid(self, query: MemoryQuery):
        return self.vault.search(query)

    def reconcile_source(self, source: str):
        return self.vault.reconcile_source(source)

    def consolidate(self, events: list[dict], *, mission_id: str, verified: bool):
        return self.vault.consolidate(events, mission_id=mission_id, verified=verified)
