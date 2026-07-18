"""SkillsStore — learned-strategy namespace (Phase J).

A *skill* in this namespace is a learned strategy — distilled
from completed tasks — for handling a class of work the agent has
done before. Distinct from :mod:`nexus_core.skills` (which manages
*external* skills installed from LobeHub / Binance Skills Hub /
GitHub); SkillsStore tracks the *agent's own* learnings.

Example: after the agent reviews three Solidity contracts, the
SkillEvolver writes a learned-strategy entry::

    skill_name="solidity_review"
    strategy="Always check reentrancy + gas optimisation; cite OZ
              patterns; flag unbounded loops."
    last_lesson="User cared most about gas; spent extra time on
                 storage-vs-memory call patterns."
    success_count=3, failure_count=0

When a future task arrives that matches `task_kind ∈ task_kinds`,
the chat projection looks up the strategy and injects it as
context — the agent benefits from past learnings without re-
deriving them.

Same Phase J pattern: working file + VersionedStore + cheap
``upsert`` / heavyweight ``commit``. See :class:`EpisodesStore`
for the canonical pattern docs.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ..versioned import VersionedStore

if TYPE_CHECKING:
    from ..core.backend import StorageBackend


logger = logging.getLogger("nexus_core.memory.skills")
SCHEMA = "nexus.memory.skills.v1"


@dataclasses.dataclass
class LearnedSkill:
    """One learned strategy. Keyed by ``skill_name``.

    Operational fields (Phase D)
    ----------------------------
    ``times_used`` and ``last_used`` are bumped each time the skill
    is actually applied at chat time (via ``record_outcome`` /
    ``mark_used``). The chat projection uses them to prefer
    recently-used skills when ranking.

    ``lessons`` is a bounded log (last 10) of structured lessons
    learned during application — distinct from the freeform
    ``last_lesson`` summary, this gives the audit trail something
    to render in the timeline.
    """
    skill_name: str
    description: str = ""
    strategy: str = ""
    last_lesson: str = ""
    confidence: float = 0.5
    success_count: int = 0
    failure_count: int = 0
    times_used: int = 0
    last_used: float = 0.0
    lessons: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    task_kinds: list[str] = dataclasses.field(default_factory=list)
    tags: list[str] = dataclasses.field(default_factory=list)
    created_at: float = dataclasses.field(default_factory=time.time)
    updated_at: float = dataclasses.field(default_factory=time.time)
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"LearnedSkill.confidence must be in [0,1], got {self.confidence}"
            )

    @property
    def total_invocations(self) -> int:
        return self.success_count + self.failure_count

    @property
    def success_rate(self) -> float:
        if self.total_invocations == 0:
            return 0.0
        return self.success_count / self.total_invocations

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        if not d["extra"]:
            del d["extra"]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LearnedSkill":
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


class SkillsStore:
    """Versioned store for learned strategies."""

    def __init__(
        self,
        base_dir: str | Path,
    ):
        self._dir = Path(base_dir).resolve() / "skills"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._versioned = VersionedStore(self._dir)
        if not self._working_path().exists():
            committed = self._versioned.current()
            if committed is not None:
                self._write_working(committed)

    # ── Read ────────────────────────────────────────────────────

    def all(self) -> list[LearnedSkill]:
        return [LearnedSkill.from_dict(d) for d in self._read_working()["skills"]]

    def get(self, skill_name: str) -> Optional[LearnedSkill]:
        for s in self.all():
            if s.skill_name == skill_name:
                return s
        return None

    def find_for_task_kind(self, task_kind: str) -> list[LearnedSkill]:
        """Skills whose ``task_kinds`` include the given kind, sorted
        by success_rate desc → confidence desc."""
        hits = [s for s in self.all() if task_kind in s.task_kinds]
        hits.sort(key=lambda s: (-s.success_rate, -s.confidence))
        return hits

    def search(self, query: str) -> list[LearnedSkill]:
        if not query:
            return []
        q = query.lower()
        return [
            s for s in self.all()
            if q in s.skill_name.lower()
            or q in s.description.lower()
            or q in s.strategy.lower()
            or any(q in t.lower() for t in s.tags)
        ]

    def __len__(self) -> int:
        return len(self.all())

    # ── Mutate ──────────────────────────────────────────────────

    def upsert(self, skill: LearnedSkill) -> None:
        """Add or replace by ``skill_name``. ``updated_at`` is
        bumped to ``time.time()`` if not already set to a different
        value."""
        skill.updated_at = time.time()
        data = self._read_working()
        for i, sd in enumerate(data["skills"]):
            if sd.get("skill_name") == skill.skill_name:
                data["skills"][i] = skill.to_dict()
                self._write_working(data)
                return
        data["skills"].append(skill.to_dict())
        self._write_working(data)

    def remove(self, skill_name: str) -> bool:
        data = self._read_working()
        before = len(data["skills"])
        data["skills"] = [
            s for s in data["skills"]
            if s.get("skill_name") != skill_name
        ]
        if len(data["skills"]) < before:
            self._write_working(data)
            return True
        return False

    def record_outcome(
        self, skill_name: str, *, success: bool, lesson: str = "",
    ) -> bool:
        """Increment success/failure counters and (optionally) update
        ``last_lesson``. Returns True if the skill exists.

        Also appends to ``lessons`` (bounded to the last 10) when
        ``lesson`` is non-empty, and bumps ``times_used`` /
        ``last_used`` since this is the canonical chat-time hook
        that an evolver invokes after a skill is applied.
        """
        data = self._read_working()
        now = time.time()
        for sd in data["skills"]:
            if sd.get("skill_name") == skill_name:
                if success:
                    sd["success_count"] = sd.get("success_count", 0) + 1
                else:
                    sd["failure_count"] = sd.get("failure_count", 0) + 1
                sd["times_used"] = sd.get("times_used", 0) + 1
                sd["last_used"] = now
                if lesson:
                    sd["last_lesson"] = lesson
                    lessons = sd.setdefault("lessons", [])
                    lessons.append({
                        "lesson": lesson,
                        "success": bool(success),
                        "timestamp": now,
                    })
                    sd["lessons"] = lessons[-10:]
                sd["updated_at"] = now
                self._write_working(data)
                return True
        return False

    def mark_used(self, skill_name: str) -> bool:
        """Bump ``times_used`` / ``last_used`` without recording an
        outcome — useful when the projection just *applies* a skill
        (e.g. retrieves its strategy) without yet knowing whether
        it succeeded."""
        data = self._read_working()
        for sd in data["skills"]:
            if sd.get("skill_name") == skill_name:
                sd["times_used"] = sd.get("times_used", 0) + 1
                sd["last_used"] = time.time()
                sd["updated_at"] = sd["last_used"]
                self._write_working(data)
                return True
        return False

    # ── Versioning ──────────────────────────────────────────────

    def commit(self) -> str:
        snapshot = self._read_working()
        version = self._versioned.propose(snapshot)
        logger.info(
            "skills.commit: %d skills pinned at %s",
            len(snapshot["skills"]), version,
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
                "skill_count": len(data.get("skills", [])),
            })
        return out

    def get_version(self, version: str) -> Optional[list[LearnedSkill]]:
        data = self._versioned.get(version)
        if data is None:
            return None
        return [LearnedSkill.from_dict(d) for d in data.get("skills", [])]

    # ── Internals ───────────────────────────────────────────────

    @property
    def base_dir(self) -> Path:
        return self._dir

    def _working_path(self) -> Path:
        return self._dir / _WORKING_FILE

    def _empty_working(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "skills": []}

    def _read_working(self) -> dict[str, Any]:
        p = self._working_path()
        if not p.exists():
            return self._empty_working()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("skills: working file unreadable: %s", e)
            return self._empty_working()
        data.setdefault("schema", SCHEMA)
        data.setdefault("skills", [])
        return data

    def _write_working(self, data: dict[str, Any]) -> None:
        p = self._working_path()
        tmp = p.with_suffix(".tmp")
        data.setdefault("schema", SCHEMA)
        data.setdefault("skills", [])
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(p)


__all__ = ["LearnedSkill", "SkillsStore", "SCHEMA"]
