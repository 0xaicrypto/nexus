"""PersonaStore — agent identity / style namespace (Phase J).

Persona is special among the 5 namespaces because there is **only
one active persona at a time** — the agent has one identity, not
a list of identities. The "store" is therefore directly a
:class:`VersionedStore` whose every version IS the persona at
that point in time.

This makes the API cleaner than the other namespaces' working+
commit dance: every persona update is implicitly a new version
(``propose_version``), and rollback is just ``VersionedStore.rollback``.

Why per-version is the right shape (vs. a working file):

* **Audit trail is the point.** PersonaEvolver's prose-level edits
  are the highest-risk evolution per the AHE paper (system-prompt-
  alone change measured as −2.3 pp on Terminal-Bench 2). Users
  and operators should be able to diff "v3 → v4" and roll back
  if a drift was introduced.
* **Frequency is low.** Default Phase O thresholds put PersonaEvolver
  at ≥30-day intervals, so version inflation isn't a concern.
* **There is no "in-flight working" state.** Persona updates
  happen in one shot — the LLM proposes a new persona text, we
  either keep it or reject it. No incremental upserts.

Each version carries metadata about how it came to be: what
events triggered it, what the diff is, what drift metrics looked
like at the time. Enough that "evolution timeline" UI can render
a meaningful narrative.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ..versioned import VersionedStore

if TYPE_CHECKING:
    from ..core.backend import StorageBackend


logger = logging.getLogger("nexus_core.memory.persona")
SCHEMA = "nexus.memory.persona.v1"


@dataclasses.dataclass
class PersonaVersion:
    """A single persona snapshot."""
    persona_text: str = ""
    changes_summary: str = ""           # what changed vs prev (free-form)
    triggered_by_event_ids: list[int] = dataclasses.field(default_factory=list)
    drift_metrics: dict[str, float] = dataclasses.field(default_factory=dict)
    confidence: float = 0.5              # evolver's self-rating
    version_notes: str = ""              # short author note
    created_at: float = dataclasses.field(default_factory=time.time)
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        if not d["extra"]:
            del d["extra"]
        d["schema"] = SCHEMA
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PersonaVersion":
        d = dict(d)
        d.pop("schema", None)            # not part of the dataclass
        d.setdefault("extra", {})
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = {k: v for k, v in d.items() if k not in known}
        for k in unknown:
            d.pop(k)
        if unknown:
            d["extra"] = {**d["extra"], **unknown}
        return cls(**d)


class PersonaStore:
    """Versioned store for the agent's persona prompt.

    Each version is the *whole* persona at that point. Rollback
    flips the active pointer; older versions are never destroyed.
    """

    def __init__(
        self,
        base_dir: str | Path,
        *,
        chain_backend: Optional["StorageBackend"] = None,
    ):
        self._dir = Path(base_dir).resolve() / "persona"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._versioned = VersionedStore(
            self._dir,
            chain_backend=chain_backend,
            chain_namespace="persona" if chain_backend is not None else None,
        )

    async def recover_from_chain(self) -> int:
        """Hydrate this store from chain. See
        ``VersionedStore.recover_from_chain``.
        """
        return await self._versioned.recover_from_chain()

    # ── Read API ─────────────────────────────────────────────────

    def current(self) -> Optional[PersonaVersion]:
        """The active persona, or ``None`` if never set."""
        d = self._versioned.current()
        if d is None:
            return None
        return PersonaVersion.from_dict(d)

    def current_version(self) -> Optional[str]:
        return self._versioned.current_version()

    def get_version(self, version: str) -> Optional[PersonaVersion]:
        d = self._versioned.get(version)
        if d is None:
            return None
        return PersonaVersion.from_dict(d)

    def history(self, limit: Optional[int] = None) -> list[dict]:
        """Audit-friendly summary of every persona version on disk
        (independent of which is currently active)."""
        records = self._versioned.history(limit=limit)
        out = []
        for r in records:
            data = self._versioned.get(r.version) or {}
            out.append({
                "version": r.version,
                "created_at": r.created_at,
                "changes_summary": data.get("changes_summary", ""),
                "confidence": data.get("confidence", 0.0),
                "version_notes": data.get("version_notes", ""),
            })
        return out

    def __len__(self) -> int:
        return len(self._versioned)

    # ── Mutate ──────────────────────────────────────────────────

    def propose_version(self, persona: PersonaVersion) -> str:
        """Append a new persona version, becomes active. Returns
        the new version label.

        Note: unlike the other namespaces, persona has no cheap
        upsert / commit split — every update IS a commit. This
        matches PersonaEvolver's actual behaviour (it runs ~monthly
        and produces one new persona per run).
        """
        version = self._versioned.propose(persona.to_dict())
        logger.info(
            "persona.propose_version: %s (confidence=%.2f, notes=%r)",
            version, persona.confidence, persona.version_notes[:50],
        )
        return version

    def rollback(self, version: str) -> str:
        prev = self._versioned.rollback(version)
        logger.info("persona.rollback: %s → %s", prev, version)
        return prev

    # ── Internals ───────────────────────────────────────────────

    @property
    def base_dir(self) -> Path:
        return self._dir


__all__ = ["PersonaVersion", "PersonaStore", "SCHEMA"]
