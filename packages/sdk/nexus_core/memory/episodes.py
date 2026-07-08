"""EpisodesStore — session-level episodic memory.

An *episode* in Nexus is a structured summary of one conversation
session — what topics it covered, key events that defined it, the
outcome, and a short prose summary. Episodes accumulate over time
into the agent's autobiographical memory: "what conversations have
I had with this user, and how did they go?"

This is one of the 5 curated-memory namespaces specified in
BEP-Nexus v0.2 §3.3. The other four (facts, skills, persona,
knowledge) follow the same pattern — this module is the reference
implementation for that pattern.

Storage model
=============

Built on :class:`nexus_core.versioned.VersionedStore`. Two writing
modes:

* **Working mode** (cheap appends). Within a compaction window,
  ``add()`` mutates the working JSON file directly. No new version
  is committed, so the version chain stays bounded.
* **Commit mode** (snapshot). At compaction boundary or when the
  Phase O coordinator wants a falsifiable rollback point,
  :meth:`commit` snapshots the working state into a new immutable
  version of the underlying VersionedStore.

Reads always go through the working file first, falling back to
the current committed version. After a :meth:`rollback`, the
working file is restored to match the rolled-back version.

Schema
======

Each episode is a :class:`Episode` dataclass — see the docstring.
On disk, the working file looks like::

    {
      "schema": "nexus.memory.episodes.v1",
      "episodes": [
        {
          "session_id": "session_2026-04-28",
          "started_at": 1730000000.0,
          "ended_at": 1730003600.0,
          "summary": "User asked about Tokyo restaurants and...",
          "topics": ["travel", "food", "japan"],
          "key_event_ids": [42, 47, 51],
          "outcome": "success",
          "mood": "engaged"
        },
        ...
      ]
    }

Schema version pinned in the file lets future-compatible readers
detect which schema they're parsing.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Iterable, Literal, Optional, TYPE_CHECKING

from ..versioned import VersionedStore

if TYPE_CHECKING:
    from ..core.backend import StorageBackend


logger = logging.getLogger("nexus_core.memory.episodes")

#: Schema version for the working file format. Bump when schema
#: changes incompatibly; readers MUST check.
SCHEMA = "nexus.memory.episodes.v1"

#: Episode outcome — what kind of result the session ended in.
#: ``None`` means "not yet classified" (still active or
#: pre-classifier); the verdict scorer can revisit.
EpisodeOutcome = Literal["success", "abandoned", "continued", "failed"]


@dataclasses.dataclass
class Episode:
    """One session's distilled record.

    Episodes are keyed by ``session_id`` — adding an episode with
    an existing session_id replaces the prior one (the latest
    snapshot wins, since sessions extend over time). Use
    :meth:`upsert` rather than worrying about replace semantics.
    """
    session_id: str
    started_at: float = 0.0
    ended_at: Optional[float] = None    # None = still in progress
    summary: str = ""
    topics: list[str] = dataclasses.field(default_factory=list)
    key_event_ids: list[int] = dataclasses.field(default_factory=list)
    outcome: Optional[EpisodeOutcome] = None
    mood: str = ""                      # free-form, e.g. "engaged" / "frustrated"
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        # Drop empty extras for cleaner serialised form
        if not d["extra"]:
            del d["extra"]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Episode":
        # Defensive copy — don't mutate caller's dict
        d = dict(d)
        d.setdefault("extra", {})
        # Tolerate unknown fields (forward compat) by stuffing into extra
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = {k: v for k, v in d.items() if k not in known}
        for k in unknown:
            d.pop(k)
        if unknown:
            d["extra"] = {**d["extra"], **unknown}
        return cls(**d)

    def is_active(self) -> bool:
        """True if the session hasn't ended yet (``ended_at is None``)."""
        return self.ended_at is None


# ── EpisodesStore ────────────────────────────────────────────────────


_WORKING_FILE = "_working.json"


class EpisodesStore:
    """Versioned store for episodic memory.

    Layout::

        {base_dir}/episodes/
        ├── _working.json         working state, mutable
        ├── _current.json         pointer (managed by VersionedStore)
        ├── v0001.json            committed snapshot
        ├── v0002.json
        └── ...
    """

    def __init__(
        self,
        base_dir: str | Path,
        *,
        chain_backend: Optional["StorageBackend"] = None,
    ):
        """
        Args:
            base_dir: Parent directory. The store creates / uses
                ``{base_dir}/episodes/`` for its files.
            chain_backend: Optional storage backend used by the
                underlying ``VersionedStore`` to mirror committed
                versions. See ``VersionedStore`` docstring.
        """
        self._dir = Path(base_dir).resolve() / "episodes"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._versioned = VersionedStore(
            self._dir,
            chain_backend=chain_backend,
            chain_namespace="episodes" if chain_backend is not None else None,
        )
        # Bootstrap: if no working file but a committed version exists,
        # seed working from current. (First-time open after restart.)
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

    def all(self) -> list[Episode]:
        """All episodes in working state (newest snapshot wins per
        session_id; chronological order preserved)."""
        return [Episode.from_dict(d) for d in self._read_working()["episodes"]]

    def get_by_session(self, session_id: str) -> Optional[Episode]:
        """Lookup by session_id — returns the latest record for
        that session, or ``None``."""
        for ep in self.all():
            if ep.session_id == session_id:
                return ep
        return None

    def recent(self, limit: int = 10) -> list[Episode]:
        """Episodes ordered by ``started_at`` descending (most
        recent first)."""
        episodes = self.all()
        episodes.sort(key=lambda e: e.started_at, reverse=True)
        return episodes[:limit]

    def search(self, query: str) -> list[Episode]:
        """Substring match against summary / topics / mood. Cheap
        and good-enough for the CLI ``/memory recall`` flow; a
        proper FTS-backed index can replace this later."""
        if not query:
            return []
        q = query.lower()
        hits: list[Episode] = []
        for ep in self.all():
            haystack = " ".join([
                ep.summary,
                " ".join(ep.topics),
                ep.mood,
            ]).lower()
            if q in haystack:
                hits.append(ep)
        return hits

    def __len__(self) -> int:
        return len(self._read_working()["episodes"])

    # ── Mutating API (cheap, no version bump) ────────────────────

    def upsert(self, episode: Episode) -> None:
        """Add or replace an episode by ``session_id``. No version
        bump — call :meth:`commit` to snapshot the working state.

        The replace-by-session_id semantics matter because a
        session can be re-summarised as it grows: the agent might
        write a partial summary mid-session, then a final summary
        when the session ends.
        """
        data = self._read_working()
        existing = data["episodes"]
        # Find prior record for this session
        for i, ep_d in enumerate(existing):
            if ep_d.get("session_id") == episode.session_id:
                existing[i] = episode.to_dict()
                self._write_working(data)
                return
        existing.append(episode.to_dict())
        self._write_working(data)

    def remove(self, session_id: str) -> bool:
        """Delete an episode by session_id from working state.
        Returns True if removed, False if it didn't exist.

        Note: this only removes from working. Already-committed
        snapshots that contain this episode are unaffected (which
        means an audit can still find it).
        """
        data = self._read_working()
        before = len(data["episodes"])
        data["episodes"] = [
            e for e in data["episodes"]
            if e.get("session_id") != session_id
        ]
        if len(data["episodes"]) < before:
            self._write_working(data)
            return True
        return False

    # ── Versioning (BEP-Nexus §3.3 / Phase O) ────────────────────

    def commit(self) -> str:
        """Snapshot working state as a new immutable version.

        Called by the compactor at compaction boundaries, or by
        the falsifiable-evolution coordinator before/after an
        evolver edit (so a verdict can roll back to this point).

        Returns the new version label (e.g. ``"v0042"``).
        """
        snapshot = self._read_working()
        version = self._versioned.propose(snapshot)
        logger.info(
            "episodes.commit: %d episodes pinned at %s",
            len(snapshot["episodes"]), version,
        )
        return version

    def current_version(self) -> Optional[str]:
        """Latest committed version label (NOT the working state)."""
        return self._versioned.current_version()

    def rollback(self, version: str) -> str:
        """Restore working state from a prior committed version.

        Flips the underlying ``_current`` pointer AND overwrites
        the working file so subsequent reads see the rolled-back
        state.

        Returns the version we rolled back FROM.
        """
        prev = self._versioned.rollback(version)
        restored = self._versioned.current()
        if restored is None:
            # Rollback target was empty — clear working too.
            self._write_working(self._empty_working())
        else:
            self._write_working(restored)
        return prev

    def history(self, limit: Optional[int] = None) -> list[dict]:
        """List all committed versions for the audit / Evolution
        timeline UI. Returns a slim summary, not full episode
        contents — callers can `get_version(label)` for that."""
        records = self._versioned.history(limit=limit)
        out = []
        for r in records:
            data = self._versioned.get(r.version) or {}
            ep_count = len(data.get("episodes", []))
            out.append({
                "version": r.version,
                "created_at": r.created_at,
                "episode_count": ep_count,
            })
        return out

    def get_version(self, version: str) -> Optional[list[Episode]]:
        """Read the episodes that existed at a specific committed
        version. Useful for audit / "what did the agent remember
        on April 28?" queries."""
        data = self._versioned.get(version)
        if data is None:
            return None
        return [Episode.from_dict(d) for d in data.get("episodes", [])]

    # ── Internals ────────────────────────────────────────────────

    @property
    def base_dir(self) -> Path:
        """Where episodes are stored. Useful for tests and
        backend mirroring."""
        return self._dir

    def _working_path(self) -> Path:
        return self._dir / _WORKING_FILE

    def _empty_working(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "episodes": []}

    def _read_working(self) -> dict[str, Any]:
        p = self._working_path()
        if not p.exists():
            return self._empty_working()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("episodes: working file unreadable (%s): %s", p, e)
            return self._empty_working()
        # Sanity: ensure required keys
        data.setdefault("schema", SCHEMA)
        data.setdefault("episodes", [])
        return data

    def _write_working(self, data: dict[str, Any]) -> None:
        p = self._working_path()
        tmp = p.with_suffix(".tmp")
        # Belt: stamp schema version in case caller passed plain
        # data via VersionedStore.current().
        data.setdefault("schema", SCHEMA)
        data.setdefault("episodes", [])
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(p)


__all__ = ["Episode", "EpisodesStore", "EpisodeOutcome", "SCHEMA"]
