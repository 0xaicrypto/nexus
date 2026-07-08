"""Memory module — DPM (Deterministic Projection Memory) + Phase J namespaces.

DPM primitives (the canonical event-log + projection layer):

* :class:`EventLog` — append-only event log (SQLite + FTS5, snapshots via the chain backend)
* :class:`CuratedMemory` — derived view (MEMORY.md + USER.md), the original
  flat curated store
* :class:`EventLogCompactor` — auto-compact event log → CuratedMemory

**Phase J namespace stores** (per BEP-Nexus v0.2 §3.3) — five
parallel stores, one per memory category, each built on
:class:`nexus_core.versioned.VersionedStore`:

* :class:`EpisodesStore` — session-level autobiographical memory
* :class:`FactsStore` — atomic, citable claims
* :class:`SkillsStore` — learned strategies per task_kind
* :class:`PersonaStore` — agent identity / style (every update is
  a new version; no working file — every change is auditable)
* :class:`KnowledgeStore` — compiled long-form distillations

All five stores share the same versioning model: ``commit`` snapshots
working state into an immutable version, ``rollback`` flips the
pointer back. Phase O's falsifiable evolution loop uses these
hooks for verdict-driven rollback.

Architecture based on: "Stateless Decision Memory for Enterprise AI Agents"
(arXiv:2604.20158) and AHE (arXiv:2604.25850).
"""

from .event_log import EventLog, Event
from .curated import CuratedMemory
from .compactor import EventLogCompactor
from .episodes import Episode, EpisodesStore, EpisodeOutcome
from .facts import Fact, FactsStore, FactCategory
from .skills import LearnedSkill, SkillsStore
from .persona import PersonaVersion, PersonaStore
from .knowledge import KnowledgeArticle, KnowledgeStore

__all__ = [
    # DPM canonical
    "EventLog", "Event", "CuratedMemory", "EventLogCompactor",
    # Phase J namespaces
    "Episode", "EpisodesStore", "EpisodeOutcome",
    "Fact", "FactsStore", "FactCategory",
    "LearnedSkill", "SkillsStore",
    "PersonaVersion", "PersonaStore",
    "KnowledgeArticle", "KnowledgeStore",
]
