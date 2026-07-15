"""P7 — research scope adapter for DigitalTwin memory (design §3.3.4
"与 DigitalTwin Memory 的集成").

The migration 0004 already added a ``scope_tags TEXT`` JSON-array
column to ``patient_memory`` (and is forward-compatible with
upcoming columns on episodes / skills / knowledge once those move
out of nexus_core into a managed projection).

This module provides:
  - tag helpers (build / read / merge scope_tag arrays)
  - patient_memory query with research scope filter
  - bookkeeping for an "episode visibility" rule (D17: Episodes/Skills
    are doctor-level and cross-scope by default; Facts are
    strictly scoped).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


def tag_study(study_id: str) -> str:
    return f"study:{study_id}"


def tag_patient(patient_hash: str) -> str:
    return f"patient:{patient_hash}"


def merge_scope_tags(existing_json: Optional[str], *new_tags: str) -> str:
    try:
        existing = json.loads(existing_json or "[]")
    except json.JSONDecodeError:
        existing = []
    if not isinstance(existing, list):
        existing = []
    s = list(dict.fromkeys([*existing, *new_tags]))
    return json.dumps(s, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────
# Patient memory: read with scope filter
# ─────────────────────────────────────────────────────────────────────


def read_patient_memory(
    conn: sqlite3.Connection,
    user_id: str,
    *, scope_kind: str = "patient",
    scope_id: str = "",
    patient_hashes: Optional[list[str]] = None,
    cohort_only: bool = False,
) -> list[dict]:
    """Return rows from ``patient_memory`` filtered by scope tags.

    Modes:
      - scope_kind='patient', scope_id=<hash>  → only rows tagged with
        ``patient:<hash>`` (or, when ``patient_memory.patient_hash`` is
        present in legacy rows, that one).
      - scope_kind='research', scope_id=<study_id> →
          cohort_only=True : only patient_hashes in ``patient_hashes``
                             AND no ``patient:`` tag exclusion (i.e.
                             we get research-level fact rows tagged
                             with the study OR rows for any cohort pt).
          cohort_only=False: union of those rows plus general
                             ``study:<id>`` tagged rows.
    """
    rows = []
    try:
        rs = conn.execute(
            "SELECT user_id, patient_hash, key, value_json, scope_tags "
            "FROM patient_memory WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("patient_memory read failed: %s", exc)
        return []

    cohort = set(patient_hashes or [])
    for user_id_, ph, k, v, tags_json in rs:
        try:
            tags = json.loads(tags_json or "[]")
        except json.JSONDecodeError:
            tags = []

        if scope_kind == "patient":
            if ph == scope_id or tag_patient(scope_id) in tags:
                rows.append(dict(patient_hash=ph, key=k, value=v, tags=tags))
        elif scope_kind == "research":
            study_tag_present = tag_study(scope_id) in tags
            in_cohort = ph in cohort if cohort else False
            if cohort_only and in_cohort:
                rows.append(dict(patient_hash=ph, key=k, value=v, tags=tags))
            elif (not cohort_only) and (study_tag_present or in_cohort):
                rows.append(dict(patient_hash=ph, key=k, value=v, tags=tags))
    return rows


def upsert_patient_memory_with_scope(
    conn: sqlite3.Connection,
    user_id: str, patient_hash: str,
    key: str, value: dict | str,
    *, additional_scope_tags: Iterable[str] = (),
) -> None:
    """Upsert a fact into patient_memory AND merge scope tags."""
    val = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    base_tags = [tag_patient(patient_hash)] + list(additional_scope_tags)

    row = conn.execute(
        "SELECT scope_tags FROM patient_memory "
        "WHERE user_id = ? AND patient_hash = ? AND key = ?",
        (user_id, patient_hash, key),
    ).fetchone()
    if row:
        new_tags = merge_scope_tags(row[0], *base_tags)
        conn.execute(
            "UPDATE patient_memory SET value_json = ?, scope_tags = ? "
            "WHERE user_id = ? AND patient_hash = ? AND key = ?",
            (val, new_tags, user_id, patient_hash, key),
        )
    else:
        new_tags = merge_scope_tags("[]", *base_tags)
        conn.execute(
            "INSERT INTO patient_memory "
            "(user_id, patient_hash, key, value_json, scope_tags) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, patient_hash, key, val, new_tags),
        )


# ─────────────────────────────────────────────────────────────────────
# Episode / Skill visibility (D17)
# ─────────────────────────────────────────────────────────────────────


def episode_visible_in_research_context(scope_tags_json: Optional[str]) -> bool:
    """Per D17: episodes are doctor-level and cross-scope by default.
    Always visible unless explicitly tagged 'private'."""
    try:
        tags = json.loads(scope_tags_json or "[]")
    except json.JSONDecodeError:
        return True
    return "private" not in tags


def skill_visible_in_research_context(scope_tags_json: Optional[str]) -> bool:
    """Skills follow the same rule as episodes (D17)."""
    return episode_visible_in_research_context(scope_tags_json)


def fact_visible_in_research_context(
    scope_tags_json: Optional[str],
    *, study_id: str,
    patient_hashes_in_cohort: set[str],
    fact_patient_hash: Optional[str] = None,
) -> bool:
    """Facts are strictly scoped (D17). Visible only if:
      - tagged with study:<study_id>, OR
      - the fact's patient is in the cohort
    """
    try:
        tags = json.loads(scope_tags_json or "[]")
    except json.JSONDecodeError:
        return False
    if tag_study(study_id) in tags:
        return True
    if fact_patient_hash and fact_patient_hash in patient_hashes_in_cohort:
        return True
    return False
