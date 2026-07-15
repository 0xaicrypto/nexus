"""FactsStore — declarative-fact namespace (Phase J).

Facts are atomic, citable claims the agent has learned about its
user / world: "user is allergic to peanuts", "user lives in
Singapore", "the deadline for project X is May 12". Each fact
carries a category, importance score, and citations back to the
events it was extracted from — those citations let the chat
projection say *"I remember from our 2026-04-15 conversation that
you mentioned …"* with attribution.

This is the workhorse namespace that MemoryEvolver writes to most
often. The flat ``CuratedMemory`` is the legacy version; FactsStore
is the Phase J replacement, adding:

* per-namespace versioning (so verdict scorer can roll back a
  fact-add that turned out to cause a regression),
* category / importance indexes for fast filtering at projection
  time,
* a usage counter (``last_used_at``) so the projection can prefer
  recently-cited facts.

Storage model is identical to :class:`EpisodesStore` — working
file + ``VersionedStore`` for committed snapshots — see that
module's docstring for the full pattern.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

from ..versioned import VersionedStore

if TYPE_CHECKING:
    from ..core.backend import StorageBackend


logger = logging.getLogger("nexus_core.memory.facts")

#: Schema version for the working file.
SCHEMA = "nexus.memory.facts.v1"

#: Fact category — drives projection filtering and TTL behaviour.
FactCategory = Literal["preference", "fact", "constraint", "goal", "context"]

_VALID_CATEGORIES: set[str] = {"preference", "fact", "constraint", "goal", "context"}


@dataclasses.dataclass
class Fact:
    """One atomic, citable claim.

    A fact's ``key`` is a UUID; the same logical claim may be
    upserted multiple times (the latest extraction wins, but the
    history of versions on disk preserves the prior ones for audit).

    Operational fields (Phase D 续 / #157)
    --------------------------------------
    ``access_count`` is bumped each time the fact is read at chat
    time (via ``touch`` or the ``mark_accessed`` helper). It drives
    least-accessed eviction during MemoryEvolver consolidation —
    facts the agent never re-cites can be merged into summaries.
    """
    key: str = dataclasses.field(default_factory=lambda: str(uuid.uuid4()))
    content: str = ""
    category: FactCategory = "fact"
    importance: int = 3            # 1-5 scale; 5 = critical (e.g. allergy)
    citation_event_ids: list[int] = dataclasses.field(default_factory=list)
    created_at: float = dataclasses.field(default_factory=time.time)
    last_used_at: float = 0.0
    access_count: int = 0
    ttl: Optional[float] = None    # POSIX timestamp; None = no expiry
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.category not in _VALID_CATEGORIES:
            raise ValueError(
                f"Fact.category must be one of {_VALID_CATEGORIES}, "
                f"got {self.category!r}"
            )
        if not (1 <= self.importance <= 5):
            raise ValueError(
                f"Fact.importance must be in [1, 5], got {self.importance}"
            )

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        if not d["extra"]:
            del d["extra"]
        if d["ttl"] is None:
            del d["ttl"]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Fact":
        d = dict(d)
        d.setdefault("extra", {})
        # Forward-compat: stuff unknown fields into extra
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = {k: v for k, v in d.items() if k not in known}
        for k in unknown:
            d.pop(k)
        if unknown:
            d["extra"] = {**d["extra"], **unknown}
        return cls(**d)

    def is_expired(self, *, now: Optional[float] = None) -> bool:
        if self.ttl is None:
            return False
        return (now or time.time()) > self.ttl


# ── FactsStore ──────────────────────────────────────────────────────


_WORKING_FILE = "_working.json"


class FactsStore:
    """Versioned store for declarative facts.

    Layout::

        {base_dir}/facts/
        ├── _working.json
        ├── _current.json
        ├── v0001.json
        └── ...
    """

    def __init__(
        self,
        base_dir: str | Path,
        *,
        chain_backend: Optional["StorageBackend"] = None,
    ):
        self._dir = Path(base_dir).resolve() / "facts"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._versioned = VersionedStore(
            self._dir,
            chain_backend=chain_backend,
            chain_namespace="facts" if chain_backend is not None else None,
        )
        if not self._working_path().exists():
            committed = self._versioned.current()
            if committed is not None:
                self._write_working(committed)

    async def recover_from_chain(self) -> int:
        n = await self._versioned.recover_from_chain()
        committed = self._versioned.current()
        if committed is not None:
            self._write_working(committed)
        return n

    # ── Read API ─────────────────────────────────────────────────

    def all(self, *, include_expired: bool = False) -> list[Fact]:
        """All facts in the working state."""
        facts = [Fact.from_dict(d) for d in self._read_working()["facts"]]
        if include_expired:
            return facts
        now = time.time()
        return [f for f in facts if not f.is_expired(now=now)]

    def get(self, key: str) -> Optional[Fact]:
        for f in self.all(include_expired=True):
            if f.key == key:
                return f
        return None

    def by_category(
        self, category: FactCategory, *, include_expired: bool = False,
    ) -> list[Fact]:
        return [f for f in self.all(include_expired=include_expired) if f.category == category]

    def by_importance(
        self, min_importance: int = 1, *, include_expired: bool = False,
    ) -> list[Fact]:
        """Facts with ``importance >= min_importance``, sorted desc by importance."""
        if not (1 <= min_importance <= 5):
            raise ValueError(f"min_importance must be in [1,5], got {min_importance}")
        facts = [
            f for f in self.all(include_expired=include_expired)
            if f.importance >= min_importance
        ]
        facts.sort(key=lambda f: (-f.importance, -f.last_used_at, -f.created_at))
        return facts

    def search(self, query: str) -> list[Fact]:
        """Substring match against content. Cheap good-enough; an
        FTS-backed index can replace this later without changing
        the API."""
        if not query:
            return []
        q = query.lower()
        return [f for f in self.all() if q in f.content.lower()]

    def search_compact(
        self,
        query: str,
        top_k: int = 20,
    ) -> list[dict[str, Any]]:
        """TF-style ranked search returning lightweight summaries.

        Mirrors :meth:`MemoryProvider.search_compact` so the
        Phase D 续 migration can swap stores without changing
        evolver code shape. Each result is a dict with::

            {
              "key": "<fact key>",
              "category": "fact",
              "importance": 3,
              "preview": "first 80 chars of content",
              "score": 0.42,
            }

        Scoring: token-overlap count between query and content,
        boosted by importance (×0.1 per importance point) and
        last-used recency (×0.05 if accessed recently). No
        external dependencies; an FTS-backed index can replace
        this later without changing the API.
        """
        if not query:
            return []
        q_tokens = {t for t in re.findall(r"\w+", query.lower()) if len(t) > 1}
        if not q_tokens:
            return []
        now = time.time()
        scored: list[tuple[float, Fact]] = []
        for f in self.all():
            content_tokens = set(
                t for t in re.findall(r"\w+", f.content.lower()) if len(t) > 1
            )
            overlap = len(q_tokens & content_tokens)
            if overlap == 0:
                continue
            score = float(overlap)
            score += 0.1 * f.importance
            if f.last_used_at and (now - f.last_used_at) < 7 * 86400:
                score += 0.05
            scored.append((score, f))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = []
        for score, f in scored[:max(0, top_k)]:
            results.append({
                "key": f.key,
                "category": f.category,
                "importance": f.importance,
                "preview": f.content[:80],
                "score": round(score, 4),
            })
        return results

    def __len__(self) -> int:
        return len(self.all(include_expired=True))

    def count(self, *, include_expired: bool = False) -> int:
        """Number of facts in the working state. Equivalent to
        ``len(store)`` when ``include_expired=True`` (the default
        for ``__len__`` to preserve backward-compat)."""
        return len(self.all(include_expired=include_expired))

    def get_least_accessed(
        self,
        limit: int = 5,
        *,
        include_expired: bool = False,
    ) -> list[Fact]:
        """Facts ordered by lowest ``access_count`` first, then
        oldest ``created_at``. Used by MemoryEvolver's consolidation
        pass to pick eviction candidates: facts the agent never re-
        cites can be merged into summaries.

        Mirrors :meth:`MemoryProvider.get_least_accessed` so the
        Phase D 续 migration can swap stores without changing
        evolver code shape.
        """
        facts = self.all(include_expired=include_expired)
        facts.sort(key=lambda f: (f.access_count, f.created_at))
        return facts[:max(0, limit)]

    # ── Mutating API (cheap, no version bump) ────────────────────

    def upsert(self, fact: Fact) -> None:
        """Add or replace a fact by ``key``. No version bump."""
        data = self._read_working()
        existing = data["facts"]
        for i, fd in enumerate(existing):
            if fd.get("key") == fact.key:
                existing[i] = fact.to_dict()
                self._write_working(data)
                return
        existing.append(fact.to_dict())
        self._write_working(data)

    def bulk_add(self, facts: list[Fact]) -> list[str]:
        """Add many facts in one working-file write. Returns the
        new keys in order. Mirrors :meth:`MemoryProvider.bulk_add`.

        Existing keys are *replaced* — same upsert semantics as
        :meth:`upsert`. The single-write batching keeps a 50-fact
        extraction round from triggering 50 disk writes.
        """
        if not facts:
            return []
        data = self._read_working()
        existing_index = {fd.get("key"): i for i, fd in enumerate(data["facts"])}
        keys = []
        for fact in facts:
            if fact.key in existing_index:
                data["facts"][existing_index[fact.key]] = fact.to_dict()
            else:
                existing_index[fact.key] = len(data["facts"])
                data["facts"].append(fact.to_dict())
            keys.append(fact.key)
        self._write_working(data)
        return keys

    def remove(self, key: str) -> bool:
        data = self._read_working()
        before = len(data["facts"])
        data["facts"] = [f for f in data["facts"] if f.get("key") != key]
        if len(data["facts"]) < before:
            self._write_working(data)
            return True
        return False

    def bulk_delete(self, keys: list[str]) -> int:
        """Remove many facts in one working-file write. Returns the
        number actually removed (keys not present are silently
        skipped). Mirrors :meth:`MemoryProvider.bulk_delete`.
        """
        if not keys:
            return 0
        target = set(keys)
        data = self._read_working()
        before = len(data["facts"])
        data["facts"] = [f for f in data["facts"] if f.get("key") not in target]
        removed = before - len(data["facts"])
        if removed > 0:
            self._write_working(data)
        return removed

    def touch(self, key: str, *, now: Optional[float] = None) -> bool:
        """Mark a fact as recently used. Updates ``last_used_at``
        AND bumps ``access_count`` so the projection ranking
        prefers it AND consolidation deprioritises it. Returns
        True if found.
        """
        data = self._read_working()
        ts = now or time.time()
        for fd in data["facts"]:
            if fd.get("key") == key:
                fd["last_used_at"] = ts
                fd["access_count"] = int(fd.get("access_count", 0)) + 1
                self._write_working(data)
                return True
        return False

    def touch_many(self, keys: list[str], *, now: Optional[float] = None) -> int:
        """Bulk variant of :meth:`touch`. Single working-file write.
        Returns the number of facts actually found and bumped."""
        if not keys:
            return 0
        target = set(keys)
        data = self._read_working()
        ts = now or time.time()
        bumped = 0
        for fd in data["facts"]:
            if fd.get("key") in target:
                fd["last_used_at"] = ts
                fd["access_count"] = int(fd.get("access_count", 0)) + 1
                bumped += 1
        if bumped > 0:
            self._write_working(data)
        return bumped

    def prune_expired(self, *, now: Optional[float] = None) -> int:
        """Remove expired facts from working state. Returns the
        number removed. Doesn't bump version — call commit() if
        you want the prune to be a falsifiable edit."""
        data = self._read_working()
        ts = now or time.time()
        before = len(data["facts"])
        data["facts"] = [
            f for f in data["facts"]
            if Fact.from_dict(f).is_expired(now=ts) is False
        ]
        removed = before - len(data["facts"])
        if removed > 0:
            self._write_working(data)
        return removed

    # ── Versioning ───────────────────────────────────────────────

    def commit(self) -> str:
        snapshot = self._read_working()
        version = self._versioned.propose(snapshot)
        logger.info(
            "facts.commit: %d facts pinned at %s",
            len(snapshot["facts"]), version,
        )
        return version

    def current_version(self) -> Optional[str]:
        return self._versioned.current_version()

    def rollback(self, version: str) -> str:
        prev = self._versioned.rollback(version)
        restored = self._versioned.current()
        self._write_working(restored if restored is not None else self._empty_working())
        return prev

    def history(self, limit: Optional[int] = None) -> list[dict]:
        records = self._versioned.history(limit=limit)
        out = []
        for r in records:
            data = self._versioned.get(r.version) or {}
            out.append({
                "version": r.version,
                "created_at": r.created_at,
                "fact_count": len(data.get("facts", [])),
            })
        return out

    def get_version(self, version: str) -> Optional[list[Fact]]:
        data = self._versioned.get(version)
        if data is None:
            return None
        return [Fact.from_dict(d) for d in data.get("facts", [])]

    # ── Internals ────────────────────────────────────────────────

    @property
    def base_dir(self) -> Path:
        return self._dir

    def _working_path(self) -> Path:
        return self._dir / _WORKING_FILE

    def _empty_working(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "facts": []}

    def _read_working(self) -> dict[str, Any]:
        p = self._working_path()
        if not p.exists():
            return self._empty_working()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("facts: working file unreadable: %s", e)
            return self._empty_working()
        data.setdefault("schema", SCHEMA)
        data.setdefault("facts", [])
        return data

    def _write_working(self, data: dict[str, Any]) -> None:
        p = self._working_path()
        tmp = p.with_suffix(".tmp")
        data.setdefault("schema", SCHEMA)
        data.setdefault("facts", [])
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(p)


__all__ = ["Fact", "FactsStore", "FactCategory", "SCHEMA"]
