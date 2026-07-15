"""One-shot migration: legacy MemoryProvider entries → typed Facts.

Phase D 续 #2 deleted the ``MemoryProvider`` abstraction. This
migration script reads any pre-existing entries that were stored
by the old MemoryProvider (via StorageBackend at
``agents/{agent_id}/memory/{memory_id}.json``) and projects each
one into a :class:`Fact` row inside the supplied :class:`FactsStore`.

Design properties:

* **Idempotent**: writes a ``_migrated_from_memory_provider.flag``
  file under the FactsStore directory after success. Re-running
  is a no-op.
* **No MemoryProvider dependency**: reads JSON paths directly via
  the ``StorageBackend.load_json`` interface, so the script keeps
  working after the abstraction itself is deleted.
* **Best-effort**: malformed entries are logged and skipped, never
  crash the migration. The expected steady-state is "0 entries to
  migrate" once everything has been moved.

Usage::

    from nexus_core.migrations.memory_to_facts import migrate
    n = await migrate(rune._backend, agent_id, facts_store)
    print(f"migrated {n} entries")

Server callers should invoke this once at startup before any
chat-time read paths run.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from ..core.backend import StorageBackend
from ..memory.facts import Fact, FactsStore

logger = logging.getLogger("nexus_core.migrations.memory_to_facts")


# Source-category → FactsStore-category mapping. Mirrors
# ``nexus.evolution.memory_evolver._MEMORY_TO_FACT_CATEGORY``
# but lives here separately so deleting that import doesn't
# break the migration.
_MEMORY_TO_FACT_CATEGORY: dict[str, str] = {
    "preference": "preference",
    "fact": "fact",
    "decision_pattern": "context",
    "style": "context",
    "skill": "context",         # Phase D 续: was None, now context
    "relationship": "context",
    "consolidated": "context",  # consolidation summaries
}


_FLAG_NAME = "_migrated_from_memory_provider.flag"


async def migrate(
    backend: StorageBackend,
    agent_id: str,
    facts_store: FactsStore,
    *,
    force: bool = False,
) -> int:
    """Migrate legacy MemoryProvider entries to FactsStore.

    Returns the number of facts written. ``0`` means either nothing
    to migrate or the migration has already run for this store.

    Args:
        backend: The same :class:`StorageBackend` the legacy
            MemoryProvider used. Migration reads from
            ``agents/{agent_id}/memory/`` paths via
            ``backend.list_paths`` + ``backend.load_json``.
        agent_id: Which agent's data to migrate.
        facts_store: Destination. Must be writable.
        force: When True, ignore the idempotency flag and migrate
            again. Useful for re-running after a partial failure.
    """
    flag_path = Path(facts_store.base_dir) / _FLAG_NAME
    if flag_path.exists() and not force:
        logger.debug(
            "memory_to_facts: skipping (already migrated for %s)", agent_id,
        )
        return 0

    prefix = f"agents/{_safe(agent_id)}/memory/"
    try:
        paths = await backend.list_paths(prefix)
    except Exception as e:
        logger.warning(
            "memory_to_facts: list_paths(%s) failed: %s", prefix, e,
        )
        return 0

    facts: list[Fact] = []
    skipped = 0
    for path in paths:
        # Skip the index file — only individual entries hold content
        if path.endswith("/index.json") or path.endswith("\\index.json"):
            continue
        try:
            entry = await backend.load_json(path)
        except Exception as e:
            logger.warning(
                "memory_to_facts: load_json(%s) failed: %s", path, e,
            )
            skipped += 1
            continue
        if not isinstance(entry, dict) or not entry.get("content"):
            skipped += 1
            continue

        fact = _project(entry)
        if fact is None:
            skipped += 1
            continue
        facts.append(fact)

    if facts:
        try:
            facts_store.bulk_add(facts)
            facts_store.commit()
        except Exception as e:
            logger.error(
                "memory_to_facts: facts_store write failed for %s: %s",
                agent_id, e,
            )
            return 0

    flag_path.write_text(
        f"migrated {len(facts)} entries (skipped {skipped}) at {time.time():.0f}\n"
        f"agent_id={agent_id}\n",
        encoding="utf-8",
    )
    logger.info(
        "memory_to_facts: migrated %d entries for %s (skipped %d malformed)",
        len(facts), agent_id, skipped,
    )
    return len(facts)


def _project(entry: dict[str, Any]) -> Fact | None:
    """Map a single legacy entry dict → typed :class:`Fact`."""
    content = entry.get("content", "")
    if not content:
        return None
    md = entry.get("metadata") or {}
    src_cat = md.get("category", "fact")
    mapped = _MEMORY_TO_FACT_CATEGORY.get(src_cat, "fact")
    raw_importance = md.get("importance", 3)
    if raw_importance is None:
        raw_importance = 3
    importance = max(1, min(5, int(raw_importance)))
    return Fact(
        content=str(content),
        category=mapped,  # type: ignore[arg-type]
        importance=importance,
        access_count=int(entry.get("access_count", 0) or 0),
        last_used_at=float(entry.get("last_accessed", 0.0) or 0.0),
        created_at=float(entry.get("created_at", 0.0) or 0.0) or time.time(),
        extra={
            "source": "migration_from_memory_provider",
            "legacy_memory_id": str(entry.get("memory_id", "")),
            "original_category": src_cat,
            **{k: v for k, v in md.items() if k not in {"category", "importance"}},
        },
    )


def _safe(value: str) -> str:
    """Same sanitiser the legacy MemoryProvider used for path components."""
    return value.replace("/", "__").replace("\\", "__").replace("..", "__")


__all__ = ["migrate"]
