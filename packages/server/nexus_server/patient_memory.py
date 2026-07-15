"""LEGACY — superseded by the Layer 1 ClinicalGraph projections (Rev-8).

Per design v3 §16 the medic's per-patient memory IS the ClinicalGraph
projection now. ``patient_memory.md_text`` will be regenerated as a
projection view (markdown render of active findings + meds + plan)
once U3.1 ships. Until then this module + its sessions.patient_hash
column survives so the existing desktop's "Memory" tab keeps working.

**DO NOT add new features here.** The replacement is:
* Layer 1 graph reads → `memory_router_v2.GET /memory/patient/{hash}/projection`
* Markdown view rendering → `cached_views.build_view(view_kind='patient_summary')`

Migration path (U3 cutover): drop the markdown blob, regenerate Memory
mode from graph projections; sessions.patient_hash column stays
(it's how chat sessions are scoped to a patient).

────────────────────────────────────────────────────────────────────────
Original docstring (#176):

Per-patient MEMORY.md.

Today twin.curated_memory holds ONE MEMORY.md per user — fine for
single-tenant agent products but wrong for clinicians who handle
many patients. Doctor A's notes about Patient X shouldn't bleed
into Patient Y's chat.

This module persists a `patient_memory` table:
    (user_id, patient_hash, md_text, updated_at)

And gives twin.chat a way to load + append per-patient memory
alongside the user-level MEMORY.md. Sessions also gain a
``patient_hash`` column so the PatientNavigator can filter chats
to one patient.

The schema migration is idempotent — adding the column on existing
sessions tables is an ALTER TABLE that no-ops when already present.

API surface:
  GET /api/v1/patients/{hash}/memory       → returns md_text
  PUT /api/v1/patients/{hash}/memory       → replaces md_text
  POST /api/v1/patients/{hash}/memory/append (body: {entry: str}) → appends a line
"""

from __future__ import annotations

import logging
import sqlite3
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from nexus_server.auth import get_current_user
from nexus_server.database import get_db_connection

logger = logging.getLogger(__name__)


def _ensure_schema() -> None:
    """Idempotent create + migration. Adds:
      - patient_memory table (new)
      - sessions.patient_hash column (new — on existing sessions table)
    """
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS patient_memory (
                user_id      TEXT NOT NULL,
                patient_hash TEXT NOT NULL,
                md_text      TEXT NOT NULL DEFAULT '',
                updated_at   INTEGER NOT NULL,
                PRIMARY KEY (user_id, patient_hash)
            )
        """)
        # Sessions migration — add patient_hash if it doesn't exist.
        # ALTER TABLE ... ADD COLUMN raises on duplicate; we swallow.
        try:
            conn.execute(
                "ALTER TABLE nexus_sessions ADD COLUMN "
                "patient_hash TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                logger.debug("sessions.patient_hash migrate: %s", e)
        conn.commit()


def get_patient_memory(user_id: str, patient_hash: str) -> str:
    """Return the markdown body of a patient's memory, or empty
    string when none exists yet."""
    if not patient_hash:
        return ""
    _ensure_schema()
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT md_text FROM patient_memory "
            "WHERE user_id = ? AND patient_hash = ?",
            (user_id, patient_hash),
        ).fetchone()
    return (row[0] if row else "") or ""


def set_patient_memory(
    user_id: str, patient_hash: str, md_text: str,
) -> None:
    """Replace the patient's memory body wholesale. UPSERT pattern
    so first write creates the row.

    Called by the medic via the Memory tab UI, OR by the agent's
    own memory evolver when it learns a stable fact about the
    patient ("This patient has GLP-1 history → mention in any
    cardiac decisions"). 1 MB cap prevents runaway growth."""
    if not patient_hash:
        return
    if len(md_text) > 1_000_000:
        md_text = md_text[:1_000_000]
    _ensure_schema()
    now = int(time.time())
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO patient_memory "
            "(user_id, patient_hash, md_text, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, patient_hash) DO UPDATE "
            "SET md_text = excluded.md_text, "
            "    updated_at = excluded.updated_at",
            (user_id, patient_hash, md_text, now),
        )
        conn.commit()


def append_patient_memory(
    user_id: str, patient_hash: str, entry: str,
) -> None:
    """Append one line to the patient's memory, deduped against the
    most-recent N lines (cheap regex against the last 50 lines).
    Mirrors CuratedMemory.add_memory's idempotency contract."""
    if not patient_hash or not entry.strip():
        return
    current = get_patient_memory(user_id, patient_hash)
    recent = "\n".join(current.splitlines()[-50:])
    if entry.strip() in recent:
        return   # already there
    sep = "\n" if current and not current.endswith("\n") else ""
    new_text = current + sep + entry.rstrip("\n") + "\n"
    set_patient_memory(user_id, patient_hash, new_text)


# ── HTTP surface ───────────────────────────────────────────────────


router = APIRouter(prefix="/api/v1/patients", tags=["patient-memory"])


class PatientMemoryResponse(BaseModel):
    patient_hash: str
    md_text:      str
    updated_at:   int


class SetPatientMemoryRequest(BaseModel):
    md_text: str


class AppendPatientMemoryRequest(BaseModel):
    entry: str


@router.get("/{patient_hash}/memory",
            response_model=PatientMemoryResponse)
async def get_memory(
    patient_hash: str,
    current_user: str = Depends(get_current_user),
) -> PatientMemoryResponse:
    text = get_patient_memory(current_user, patient_hash)
    _ensure_schema()
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT updated_at FROM patient_memory "
            "WHERE user_id = ? AND patient_hash = ?",
            (current_user, patient_hash),
        ).fetchone()
    return PatientMemoryResponse(
        patient_hash=patient_hash,
        md_text=text,
        updated_at=int(row[0]) if row else 0,
    )


@router.put("/{patient_hash}/memory",
            response_model=PatientMemoryResponse)
async def put_memory(
    patient_hash: str,
    req: SetPatientMemoryRequest,
    current_user: str = Depends(get_current_user),
) -> PatientMemoryResponse:
    set_patient_memory(current_user, patient_hash, req.md_text)
    return await get_memory(patient_hash, current_user)


@router.post("/{patient_hash}/memory/append",
             response_model=PatientMemoryResponse)
async def append_memory(
    patient_hash: str,
    req: AppendPatientMemoryRequest,
    current_user: str = Depends(get_current_user),
) -> PatientMemoryResponse:
    append_patient_memory(current_user, patient_hash, req.entry)
    return await get_memory(patient_hash, current_user)


# ── Twin integration helper ────────────────────────────────────────


def build_patient_memory_block(
    user_id: str, patient_hash: str,
) -> str:
    """Format the patient memory as a system-prompt fragment for
    twin.chat to inject before the user's message. Empty string
    when no memory exists yet.

    Format mirrors the existing curated_memory block so the agent
    sees a familiar shape:

        [Patient Memory — PHI-hash:abc12345]
        <md_text>
        [/Patient Memory]
    """
    text = get_patient_memory(user_id, patient_hash)
    if not text.strip():
        return ""
    short_hash = patient_hash[:12] if patient_hash else "(no-hash)"
    return (
        f"[Patient Memory — PHI-hash:{short_hash}]\n"
        f"{text.strip()}\n"
        f"[/Patient Memory]"
    )
