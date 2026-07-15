"""KnowledgeStore — distilled-article namespace (Phase J).

Where :class:`FactsStore` holds atomic claims, KnowledgeStore
holds *long-form distillations*: the agent's compiled
understanding of a topic, derived from clusters of related facts
+ episodes. Output of ``KnowledgeCompiler``.

Example article: ``"User's Travel Preferences"`` — a multi-paragraph
synthesis of "user prefers window seats", "has Global Entry",
"loves Tokyo", "values cultural experiences over tourist spots",
etc., compiled into a coherent narrative the chat projection can
inject when travel-related queries come up.

Articles have a richer structure than facts (title, summary,
content, key_facts) and are bigger (~5–20 KB each). They're the
agent's "what I know about you" reference material, kept distinct
from "atomic things I've learned" (facts).

Same Phase J pattern: working file + VersionedStore + cheap
``upsert`` / heavyweight ``commit``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ..versioned import VersionedStore

if TYPE_CHECKING:
    from ..core.backend import StorageBackend


logger = logging.getLogger("nexus_core.memory.knowledge")
SCHEMA = "nexus.memory.knowledge.v1"


@dataclasses.dataclass
class KnowledgeArticle:
    """A compiled long-form distillation."""
    article_id: str = dataclasses.field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    summary: str = ""
    content: str = ""                       # markdown allowed
    key_facts: list[str] = dataclasses.field(default_factory=list)
    tags: list[str] = dataclasses.field(default_factory=list)
    source_fact_keys: list[str] = dataclasses.field(default_factory=list)
    source_episode_ids: list[str] = dataclasses.field(default_factory=list)
    confidence: float = 0.5
    visibility: str = "private"             # "private" | "connections" | "public"
    created_at: float = dataclasses.field(default_factory=time.time)
    updated_at: float = dataclasses.field(default_factory=time.time)
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"KnowledgeArticle.confidence must be in [0,1], got {self.confidence}"
            )
        if self.visibility not in {"private", "connections", "public"}:
            raise ValueError(
                f"visibility must be private/connections/public, got {self.visibility!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        if not d["extra"]:
            del d["extra"]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "KnowledgeArticle":
        d = dict(d)
        d.setdefault("extra", {})
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = {k: v for k, v in d.items() if k not in known}
        for k in unknown:
            d.pop(k)
        if unknown:
            d["extra"] = {**d["extra"], **unknown}
        return cls(**d)


_WORKING_FILE = "_working.json"


class KnowledgeStore:
    """Versioned store for compiled knowledge articles."""

    def __init__(
        self,
        base_dir: str | Path,
        *,
        chain_backend: Optional["StorageBackend"] = None,
    ):
        self._dir = Path(base_dir).resolve() / "knowledge"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._versioned = VersionedStore(
            self._dir,
            chain_backend=chain_backend,
            chain_namespace="knowledge" if chain_backend is not None else None,
        )
        if not self._working_path().exists():
            committed = self._versioned.current()
            if committed is not None:
                self._write_working(committed)

    async def recover_from_chain(self) -> int:
        """Hydrate from chain. After hydrate, the working file is
        reseeded from ``current()``.
        """
        n = await self._versioned.recover_from_chain()
        committed = self._versioned.current()
        if committed is not None:
            self._write_working(committed)
        return n

    # ── Read ────────────────────────────────────────────────────

    def all(self) -> list[KnowledgeArticle]:
        return [KnowledgeArticle.from_dict(d) for d in self._read_working()["articles"]]

    def get(self, article_id: str) -> Optional[KnowledgeArticle]:
        for a in self.all():
            if a.article_id == article_id:
                return a
        return None

    def get_by_title(self, title: str) -> Optional[KnowledgeArticle]:
        for a in self.all():
            if a.title == title:
                return a
        return None

    def by_tag(self, tag: str) -> list[KnowledgeArticle]:
        return [a for a in self.all() if tag in a.tags]

    def search(self, query: str) -> list[KnowledgeArticle]:
        if not query:
            return []
        q = query.lower()
        return [
            a for a in self.all()
            if q in a.title.lower()
            or q in a.summary.lower()
            or q in a.content.lower()
            or any(q in t.lower() for t in a.tags)
        ]

    def __len__(self) -> int:
        return len(self.all())

    # ── Mutate ──────────────────────────────────────────────────

    def upsert(self, article: KnowledgeArticle) -> None:
        article.updated_at = time.time()
        data = self._read_working()
        for i, ad in enumerate(data["articles"]):
            if ad.get("article_id") == article.article_id:
                data["articles"][i] = article.to_dict()
                self._write_working(data)
                return
        data["articles"].append(article.to_dict())
        self._write_working(data)

    def remove(self, article_id: str) -> bool:
        data = self._read_working()
        before = len(data["articles"])
        data["articles"] = [
            a for a in data["articles"]
            if a.get("article_id") != article_id
        ]
        if len(data["articles"]) < before:
            self._write_working(data)
            return True
        return False

    # ── Versioning ──────────────────────────────────────────────

    def commit(self) -> str:
        snapshot = self._read_working()
        version = self._versioned.propose(snapshot)
        logger.info(
            "knowledge.commit: %d articles pinned at %s",
            len(snapshot["articles"]), version,
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
                "article_count": len(data.get("articles", [])),
            })
        return out

    def get_version(self, version: str) -> Optional[list[KnowledgeArticle]]:
        data = self._versioned.get(version)
        if data is None:
            return None
        return [KnowledgeArticle.from_dict(d) for d in data.get("articles", [])]

    # ── Internals ───────────────────────────────────────────────

    @property
    def base_dir(self) -> Path:
        return self._dir

    def _working_path(self) -> Path:
        return self._dir / _WORKING_FILE

    def _empty_working(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "articles": []}

    def _read_working(self) -> dict[str, Any]:
        p = self._working_path()
        if not p.exists():
            return self._empty_working()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("knowledge: working file unreadable: %s", e)
            return self._empty_working()
        data.setdefault("schema", SCHEMA)
        data.setdefault("articles", [])
        return data

    def _write_working(self, data: dict[str, Any]) -> None:
        p = self._working_path()
        tmp = p.with_suffix(".tmp")
        data.setdefault("schema", SCHEMA)
        data.setdefault("articles", [])
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(p)


__all__ = ["KnowledgeArticle", "KnowledgeStore", "SCHEMA"]
