from __future__ import annotations

from .util import ForgeError, sha


class Memory:
    def __init__(self, store, project: str, workspace):
        self.store, self.project, self.workspace = store, project, workspace

    def add(self, note: str, source: str | None = None, origin="agent"):
        fingerprint = None
        if source:
            fingerprint = sha(self.workspace.path(source).read_bytes())
        return {"id": self.store.remember(self.project, note, source, fingerprint, origin)}

    def search(self, query="", *, include_stale=False):
        results = []
        for row in self.store.memories(self.project, query):
            stale = False
            if row["source"]:
                try:
                    stale = sha(self.workspace.path(row["source"]).read_bytes()) != row["source_hash"]
                except (OSError, ForgeError):
                    stale = True
            row["stale"] = stale
            if include_stale or not stale:
                results.append(row)
        return results
