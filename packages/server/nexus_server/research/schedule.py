"""Schedule expansion + auto-link (design §5.2 / §5.3).

When a patient is enrolled, ``expand_schedule`` turns the study's
``schedule_json`` template into concrete planned ``study_assessments``
rows with absolute ``due_at`` timestamps. The scheduler.py loop later
fires reminders for rows where ``status='planned' AND due_at<=now``.

When patient state changes (NODE_ADDED with a matching kind, manual
SOAP save), ``link_node_to_assessment`` finds any planned assessment
in the ±N day window and marks it completed.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Optional

from nexus_server.database import get_db_connection
from nexus_server.event_sourcing import EventKind, Store

logger = logging.getLogger(__name__)


DAY_MS = 86_400_000
DEFAULT_LINK_WINDOW_DAYS = 7


# ─────────────────────────────────────────────────────────────────────
# Expansion
# ─────────────────────────────────────────────────────────────────────


def expand_schedule(
    user_id: str, study_id: str, patient_hash: str,
    enrolled_at_ms: int,
) -> int:
    """Materialize the study's schedule_json into study_assessments
    rows for this patient. Returns count of rows inserted.

    Idempotent: re-running won't duplicate rows (UNIQUE PK on
    (user_id, study_id, patient_hash, visit_id, assessment_kind)).
    """
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT schedule_json FROM research_studies "
            "WHERE user_id = ? AND study_id = ? AND archived_at IS NULL",
            (user_id, study_id),
        ).fetchone()
        if not row:
            return 0
        try:
            schedule = json.loads(row[0] or "[]")
        except json.JSONDecodeError:
            return 0

        n = 0
        for v in schedule:
            label  = v.get("label") or "visit"
            offset = int(v.get("offset_days") or 0)
            assesses = v.get("assessments") or []
            repeat_every = v.get("repeat_every_days")
            repeat_until = v.get("repeat_until_days") or 365

            offsets = [offset]
            if repeat_every:
                cur = offset + int(repeat_every)
                while cur <= int(repeat_until):
                    offsets.append(cur)
                    cur += int(repeat_every)

            for off in offsets:
                visit_id = f"{label}+{off}d"
                due_at = enrolled_at_ms + off * DAY_MS
                for a in assesses:
                    try:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO study_assessments
                            (user_id, study_id, patient_hash, visit_id,
                             assessment_kind, status, due_at)
                            VALUES (?, ?, ?, ?, ?, 'planned', ?)
                            """,
                            (user_id, study_id, patient_hash, visit_id, a, due_at),
                        )
                        n += conn.total_changes  # approximate; sqlite cursor lastrowid==0 on IGNORE
                    except sqlite3.Error as exc:
                        logger.warning("schedule insert failed: %s", exc)
                    try:
                        store = Store(conn)
                        store.emit_and_apply(
                            kind=EventKind.STUDY_ASSESSMENT_PLANNED,
                            payload={
                                "study_id": study_id,
                                "visit_id": visit_id,
                                "assessment_kind": a,
                                "due_at": due_at,
                            },
                            apply_fn=lambda c, e: None,
                            user_id=user_id,
                            patient_hash=patient_hash,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("STUDY_ASSESSMENT_PLANNED emit failed: %s", exc)
        conn.commit()
    return n


# ─────────────────────────────────────────────────────────────────────
# Auto-link (NODE_ADDED → assessment completed)
# ─────────────────────────────────────────────────────────────────────


# Map clinical_graph_nodes.node_type → set of assessment_kind hints.
# Coarse — UI gives medic a "re-link / unlink" button to correct.
NODE_TO_ASSESSMENT_KIND: dict[str, set[str]] = {
    "key_image":    {"chest_ct", "chest_ct_contrast", "chest_ct_plain",
                     "pet_ct", "imaging", "imaging_ct"},
    "lab":          {"lab_panel", "labs", "blood_panel", "cardiac_enzymes",
                     "tft", "adrenal_panel"},
    "finding":      {"imaging_finding"},
    "encounter":    {"soap", "visit"},
    "ecog":         {"ecog"},
    "measurement":  {"measurement"},
}


def link_node_to_assessment(
    user_id: str, patient_hash: str,
    node_id: str, node_type: str,
    *, window_days: int = DEFAULT_LINK_WINDOW_DAYS,
) -> int:
    """Search for planned assessments of this patient (in any study)
    whose due_at is within ±window_days of now AND whose
    assessment_kind matches the node_type. Mark them completed.

    Returns count of assessments completed.
    """
    candidate_kinds = NODE_TO_ASSESSMENT_KIND.get(node_type, set())
    if not candidate_kinds:
        return 0
    now = int(time.time() * 1000)
    lo, hi = now - window_days * DAY_MS, now + window_days * DAY_MS
    matched: list[tuple[str, str, str]] = []
    with get_db_connection() as conn:
        placeholders = ",".join("?" for _ in candidate_kinds)
        rows = conn.execute(
            f"""
            SELECT study_id, visit_id, assessment_kind
            FROM study_assessments
            WHERE user_id = ? AND patient_hash = ?
              AND status = 'planned'
              AND due_at BETWEEN ? AND ?
              AND assessment_kind IN ({placeholders})
            """,
            (user_id, patient_hash, lo, hi, *candidate_kinds),
        ).fetchall()
        for sid, vid, akind in rows:
            r = conn.execute(
                "SELECT source_node_ids_json FROM study_assessments "
                "WHERE user_id=? AND study_id=? AND patient_hash=? "
                "  AND visit_id=? AND assessment_kind=?",
                (user_id, sid, patient_hash, vid, akind),
            ).fetchone()
            existing = json.loads(r[0]) if r and r[0] else []
            if node_id not in existing:
                existing.append(node_id)
            conn.execute(
                """
                UPDATE study_assessments SET
                  status = 'completed',
                  completed_at = ?,
                  source_node_ids_json = ?
                WHERE user_id=? AND study_id=? AND patient_hash=?
                  AND visit_id=? AND assessment_kind=?
                """,
                (now, json.dumps(existing), user_id, sid, patient_hash,
                 vid, akind),
            )
            matched.append((sid, vid, akind))
            try:
                store = Store(conn)
                store.emit_and_apply(
                    kind=EventKind.STUDY_ASSESSMENT_COMPLETED,
                    payload={
                        "study_id": sid,
                        "visit_id": vid,
                        "assessment_kind": akind,
                        "source_node_ids": existing,
                    },
                    apply_fn=lambda c, e: None,
                    user_id=user_id,
                    patient_hash=patient_hash,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("STUDY_ASSESSMENT_COMPLETED emit failed: %s", exc)
        conn.commit()
    return len(matched)


# ─────────────────────────────────────────────────────────────────────
# Overdue scan — for the scheduler.py loop
# ─────────────────────────────────────────────────────────────────────


def scan_overdue_assessments(
    user_id: Optional[str] = None,
    *, now_ms: Optional[int] = None,
    grace_days: int = 3,
) -> list[dict]:
    """Find assessments whose due_at + grace_days has passed and still
    status='planned'. Returns row dicts; caller (scheduler) is
    responsible for sending the reminder + flipping status='missed'."""
    now = now_ms or int(time.time() * 1000)
    cutoff = now - grace_days * DAY_MS
    sql = (
        "SELECT user_id, study_id, patient_hash, visit_id, "
        "assessment_kind, due_at FROM study_assessments "
        "WHERE status = 'planned' AND due_at < ?"
    )
    args: list = [cutoff]
    if user_id:
        sql += " AND user_id = ?"; args.append(user_id)
    with get_db_connection() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [
        dict(user_id=r[0], study_id=r[1], patient_hash=r[2],
             visit_id=r[3], assessment_kind=r[4], due_at=r[5])
        for r in rows
    ]


def mark_missed(
    user_id: str, study_id: str, patient_hash: str,
    visit_id: str, assessment_kind: str,
) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE study_assessments SET status = 'missed' "
            "WHERE user_id=? AND study_id=? AND patient_hash=? "
            "  AND visit_id=? AND assessment_kind=?",
            (user_id, study_id, patient_hash, visit_id, assessment_kind),
        )
        try:
            store = Store(conn)
            store.emit_and_apply(
                kind=EventKind.STUDY_ASSESSMENT_MISSED,
                payload={
                    "study_id": study_id,
                    "visit_id": visit_id,
                    "assessment_kind": assessment_kind,
                },
                apply_fn=lambda c, e: None,
                user_id=user_id,
                patient_hash=patient_hash,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("STUDY_ASSESSMENT_MISSED emit failed: %s", exc)
        conn.commit()
