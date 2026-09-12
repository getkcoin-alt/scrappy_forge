from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable

from .util import ForgeError, sha

EmbeddingFn = Callable[[str], list[float]]


class MemoryClass(str, Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    FAILURE = "failure"
    ENTITY = "entity"
    PREFERENCE = "preference"


@dataclass(frozen=True)
class MemoryQuery:
    text: str
    scope: dict | None = None
    entity_keys: tuple[str, ...] = ()
    memory_classes: tuple[MemoryClass, ...] = ()
    limit: int = 8
    include_stale: bool = False


@dataclass(frozen=True)
class MemoryHit:
    item: dict
    score: float
    channels: tuple[str, ...]
    stale: bool


class VaultMemory:
    """Controller-owned hybrid project memory.

    Retrieval is informational only. This object never grants permissions and callers
    must not translate a recalled preference or procedure into authorization.
    """

    def __init__(self, store, project: str, workspace, *, embed: EmbeddingFn | None = None):
        self.store = store
        self.project = project
        self.workspace = workspace
        self.embed = embed

    def _fingerprint(self, source: str | None) -> str | None:
        if not source:
            return None
        return sha(self.workspace.path(source).read_bytes())

    def add(
        self,
        content: str,
        *,
        memory_class: MemoryClass = MemoryClass.SEMANTIC,
        provenance: dict | None = None,
        scope: dict | None = None,
        confidence: float = 1.0,
        source: str | None = None,
        entity_keys: Iterable[str] = (),
        origin: str = "agent",
    ) -> dict:
        if memory_class == MemoryClass.PREFERENCE and origin != "user":
            raise ForgeError("Durable preferences require user-approved origin")
        fingerprint = self._fingerprint(source)
        embedding = self.embed(content) if self.embed else None
        mid = self.store.memory_item_add(
            project=self.project,
            memory_class=memory_class.value,
            content=content,
            provenance=provenance or {"origin": origin},
            scope=scope or {"project": self.project},
            confidence=confidence,
            source=source,
            source_fingerprint=fingerprint,
            entity_keys=list(entity_keys),
            embedding=embedding,
            origin=origin,
        )
        return {"id": mid}

    def _stale(self, row: dict) -> bool:
        if row.get("invalidated_at"):
            return True
        source = row.get("source")
        if not source:
            return False
        try:
            return self._fingerprint(source) != row.get("source_fingerprint")
        except (OSError, ForgeError):
            return True

    @staticmethod
    def _cosine(left: list[float] | None, right: list[float] | None) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        ln = math.sqrt(sum(a * a for a in left))
        rn = math.sqrt(sum(b * b for b in right))
        if not ln or not rn:
            return 0.0
        return dot / (ln * rn)

    @staticmethod
    def _scope_score(requested: dict | None, actual: dict) -> float:
        if not requested:
            return 0.0
        if all(actual.get(k) == v for k, v in requested.items()):
            return 1.0
        overlap = sum(actual.get(k) == v for k, v in requested.items())
        return overlap / max(len(requested), 1)

    @staticmethod
    def _rrf(ranks: dict[int, int], k: int = 60) -> dict[int, float]:
        return {mid: 1.0 / (k + rank) for mid, rank in ranks.items()}

    def search(self, query: MemoryQuery) -> list[MemoryHit]:
        if not query.text.strip() and not query.entity_keys:
            raise ForgeError("Memory query requires text or entity keys")
        if not 1 <= query.limit <= 50:
            raise ForgeError("Memory query limit must be between 1 and 50")

        pool: dict[int, dict] = {}
        lexical = self.store.memory_items_fts(self.project, query.text, 96)
        lexical_ranks: dict[int, int] = {}
        for rank, row in enumerate(lexical, start=1):
            pool[row["id"]] = row
            lexical_ranks[row["id"]] = rank

        recent = self.store.memory_items_recent(self.project, 128)
        for row in recent:
            pool.setdefault(row["id"], row)

        query_embedding = self.embed(query.text) if self.embed and query.text.strip() else None
        semantic_pairs = []
        if query_embedding:
            for row in pool.values():
                score = self._cosine(query_embedding, row.get("embedding"))
                if score > 0:
                    semantic_pairs.append((row["id"], score))
        semantic_pairs.sort(key=lambda pair: pair[1], reverse=True)
        semantic_ranks = {mid: rank for rank, (mid, _) in enumerate(semantic_pairs, start=1)}

        lexical_rrf = self._rrf(lexical_ranks)
        semantic_rrf = self._rrf(semantic_ranks)
        now = time.time()
        wanted_classes = {value.value for value in query.memory_classes}
        wanted_entities = set(query.entity_keys)
        hits = []
        for mid, row in pool.items():
            if wanted_classes and row["memory_class"] not in wanted_classes:
                continue
            stale = self._stale(row)
            if stale and not query.include_stale:
                continue

            channels = []
            score = 0.0
            if mid in lexical_rrf:
                score += lexical_rrf[mid]
                channels.append("lexical")
            if mid in semantic_rrf:
                score += semantic_rrf[mid]
                channels.append("embedding")

            entity_overlap = len(wanted_entities & set(row.get("entity_keys") or []))
            if entity_overlap:
                score += 0.02 * entity_overlap
                channels.append("entity")

            scope_score = self._scope_score(query.scope, row.get("scope") or {})
            if scope_score:
                score += 0.015 * scope_score
                channels.append("scope")

            age_days = max(0.0, (now - row["created_at"]) / 86400)
            recency = 1.0 / (1.0 + age_days / 30.0)
            score += 0.01 * recency
            channels.append("recency")

            score *= max(0.05, float(row.get("confidence", 1.0)))
            if stale:
                score *= 0.25
                channels.append("stale_penalty")

            if score > 0:
                item = dict(row)
                item["note"] = item["content"]  # compatibility for existing context builder
                item["stale"] = stale
                hits.append(MemoryHit(item=item, score=score, channels=tuple(channels), stale=stale))

        hits.sort(key=lambda hit: (-hit.score, -hit.item["created_at"], hit.item["id"]))
        return hits[: query.limit]

    def reconcile_source(self, source: str) -> list[int]:
        """Downgrade source-derived memories when their source fingerprint changes."""
        current = None
        try:
            current = self._fingerprint(source)
        except (OSError, ForgeError):
            pass
        invalidated = []
        for row in self.store.memory_items_for_source(self.project, source):
            if row.get("invalidated_at"):
                continue
            if current != row.get("source_fingerprint"):
                self.store.memory_item_invalidate(
                    self.project,
                    row["id"],
                    "source fingerprint changed or source became unavailable",
                )
                invalidated.append(row["id"])
        return invalidated

    def consolidate(self, events: list[dict], *, mission_id: str, verified: bool) -> list[dict]:
        """Derive small reusable records from structured mission events.

        Raw transcripts are intentionally ignored. Only structured verification, failure,
        correction and explicit memory-candidate events are eligible.
        """
        created = []
        for event in events[-200:]:
            kind = event.get("kind")
            data = event.get("data") or {}
            if kind == "memory_candidate":
                text = str(data.get("content", "")).strip()
                raw_class = data.get("memory_class", MemoryClass.SEMANTIC.value)
                try:
                    memory_class = MemoryClass(raw_class)
                except ValueError:
                    continue
                if text:
                    created.append(
                        self.add(
                            text[:4000],
                            memory_class=memory_class,
                            provenance={"mission_id": mission_id, "event": kind},
                            confidence=float(data.get("confidence", 0.7)),
                            source=data.get("source"),
                            entity_keys=data.get("entity_keys", ()),
                            origin=data.get("origin", "agent"),
                        )
                    )
            elif kind in {"verification", "mission_verified"} and verified:
                name = data.get("name") or data.get("check")
                if name and data.get("passed", True):
                    created.append(
                        self.add(
                            f"Mission {mission_id}: verification '{name}' passed.",
                            memory_class=MemoryClass.EPISODIC,
                            provenance={"mission_id": mission_id, "event": kind},
                            confidence=0.95,
                            origin="controller",
                        )
                    )
            elif kind in {"failure", "task_failed"}:
                category = str(data.get("category", "unknown_failure"))[:80]
                summary = str(data.get("summary") or data.get("error") or "failure")[:1000]
                created.append(
                    self.add(
                        f"{category}: {summary}",
                        memory_class=MemoryClass.FAILURE,
                        provenance={"mission_id": mission_id, "event": kind},
                        confidence=0.8,
                        entity_keys=data.get("entity_keys", ()),
                        origin="controller",
                    )
                )
        return created
