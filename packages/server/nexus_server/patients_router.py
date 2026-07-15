"""#180 — manual patient registration + unified patient roster.

The original PatientNavigator (#174) inferred patient cards purely from
the dicom_studies table — so a patient existed in the UI only after the
first DICOM study was uploaded. The medic asked for a different flow:

  1. "+ New patient" should open a *form* where the medic types basic
     demographics first (initials, age, sex, MRN, chief complaint),
     optionally attaching diagnostic files.
  2. The patient appears in the roster IMMEDIATELY (before any study)
     so the medic can keep working in the right per-patient context.
  3. There needs to be a place to view ALL patients with their full
     info, not just the left-rail summary.

This module adds:
  * a ``patients`` table (one row per manually-registered patient)
  * ``POST /api/v1/dicom/patients/register-manual`` — registers a
    patient + returns a stable ``patient_hash`` keyed off MRN or a
    deterministic hash of (initials, dob/age_group, sex). Same hash
    function as the DICOM ingest path so future PACS uploads of the
    same patient collide cleanly.
  * ``GET /api/v1/dicom/patients/full`` — full roster (manual rows
    UNION'd with DICOM-aggregated rows). Used by the new Patients
    main-canvas view.
  * ``GET /api/v1/dicom/patients/{patient_hash}/detail`` — single
    patient including all manually-entered fields + study summary.
"""
from __future__ import annotations

import logging
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus_server.auth import get_current_user
from nexus_server.dicom import _hash_patient_id, _index_db_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dicom", tags=["patients"])


# ── Schema ──────────────────────────────────────────────────────────


def _conn() -> sqlite3.Connection:
    """Connect to the same DICOM index DB so JOINs across patients +
    studies are cheap and consistent."""
    c = sqlite3.connect(_index_db_path())
    c.row_factory = sqlite3.Row
    return c


def init_patients_table() -> None:
    """F-merge-patients-db — ``patients`` table now lives in the
    SHARED nexus_server.db, not dicom_index.db.

    Why the move: ``patients`` is queried from FIVE places (this
    router, dicom_router, retrieval_tiers, session_takeaway,
    scheduler, research/patient_facts) — most opened the SHARED db
    connection and silently got "no such table" because the table
    was actually in dicom_index.db. F13 + F-roster-db-split were
    both one-off symptoms of the same architectural debt. This
    consolidation kills that whole class of bug.

    DICOM aggregate tables (dicom_studies / dicom_series /
    dicom_instances) stay in dicom_index.db — they're large bulk
    indexed data, and the separation is justified for them.

    One-shot migration:
      1. CREATE TABLE IF NOT EXISTS in the SHARED db (new home)
      2. If the OLD dicom_index.db still has a populated patients
         table → COPY rows into the new home (INSERT OR IGNORE
         so re-runs are no-ops) → DROP the old table
      3. Idempotent: safe to call every boot
    """
    from nexus_server.database import get_db_connection

    # Step 1 — create canonical table in the SHARED db.
    with get_db_connection() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS patients (
                patient_hash    TEXT NOT NULL,
                user_id         TEXT NOT NULL,
                -- PHI-safe display fields. We store the medic's
                -- input verbatim (it's their own private DB) but
                -- never round-trip the raw name back to the LLM —
                -- the agent only ever sees the hash + age band /
                -- sex / chief complaint.
                initials        TEXT NOT NULL DEFAULT '',
                mrn             TEXT NOT NULL DEFAULT '',
                age_group       TEXT NOT NULL DEFAULT '',  -- "50-59"
                age_value       INTEGER NOT NULL DEFAULT 0, -- raw years; 0 = unknown
                sex             TEXT NOT NULL DEFAULT '',  -- M / F / Other / ""
                chief_complaint TEXT NOT NULL DEFAULT '',
                notes           TEXT NOT NULL DEFAULT '',
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL,
                archived_at     INTEGER,
                PRIMARY KEY (user_id, patient_hash)
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_patients_user "
            "ON patients(user_id, created_at DESC)"
        )
        # Defensive: archived_at column ALTER for any pre-existing
        # SHARED-db patients table that was created before this
        # column existed (legacy intermediate states, including the
        # test fixture that hand-creates the table without it).
        # MUST run before the partial index — the index references
        # archived_at and will fail if the column doesn't exist yet.
        try:
            cols = {row[1] for row in c.execute(
                "PRAGMA table_info(patients)"
            ).fetchall()}
            if "archived_at" not in cols:
                c.execute(
                    "ALTER TABLE patients ADD COLUMN archived_at INTEGER"
                )
        except Exception as e:  # noqa: BLE001
            logger.debug("adding archived_at column failed: %s", e)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_patients_active "
            "ON patients(user_id, updated_at DESC) "
            "WHERE archived_at IS NULL"
        )
        c.commit()

    # Step 2 — one-shot data migration from dicom_index.db.
    # Safe to run every boot: INSERT OR IGNORE makes it idempotent
    # and the DROP only happens after the COPY commits.
    _migrate_patients_from_index_db()


def _migrate_patients_from_index_db() -> None:
    """One-shot copy of the legacy dicom_index.db.patients into
    nexus_server.db.patients. Drops the old table after successful
    copy. Idempotent — no-op once the migration has happened.

    Why a separate function: this is the kind of code I want to be
    able to test in isolation. If a future test fixture stuffs rows
    into the old dicom_index.db location to assert migration works,
    it can call this directly.
    """
    from nexus_server.database import get_db_connection

    # Quick check: does old table even exist?
    try:
        idx_conn = sqlite3.connect(_index_db_path())
    except Exception as exc:  # noqa: BLE001
        logger.warning("patients migration: can't open dicom_index.db: %s", exc)
        return
    try:
        legacy_present = bool(
            idx_conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='patients'"
            ).fetchone()
        )
        if not legacy_present:
            return  # nothing to migrate; common case after first boot
        try:
            legacy_rows = idx_conn.execute(
                "SELECT patient_hash, user_id, initials, mrn, age_group, "
                "       age_value, sex, chief_complaint, notes, "
                "       created_at, updated_at, "
                "       COALESCE(archived_at, NULL) "
                "FROM patients"
            ).fetchall()
        except sqlite3.Error:
            # Old schema without archived_at column — query without it.
            try:
                legacy_rows = [
                    (*r, None)
                    for r in idx_conn.execute(
                        "SELECT patient_hash, user_id, initials, mrn, "
                        "       age_group, age_value, sex, chief_complaint, "
                        "       notes, created_at, updated_at "
                        "FROM patients"
                    ).fetchall()
                ]
            except sqlite3.Error as exc:
                logger.warning(
                    "patients migration: legacy table unreadable: %s", exc,
                )
                return

        if not legacy_rows:
            # Empty table — just drop it.
            try:
                idx_conn.execute("DROP TABLE patients")
                idx_conn.commit()
            except sqlite3.Error as e:
                logger.debug("dropping legacy patients table failed: %s", e)
            return

        with get_db_connection() as shared:
            shared.executemany(
                "INSERT OR IGNORE INTO patients "
                "(patient_hash, user_id, initials, mrn, age_group, "
                " age_value, sex, chief_complaint, notes, "
                " created_at, updated_at, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                legacy_rows,
            )
            shared.commit()

        # COPY succeeded — drop the legacy table so future code
        # paths that accidentally open dicom_index.db get a clean
        # "no such table" rather than stale rows.
        try:
            idx_conn.execute("DROP TABLE patients")
            idx_conn.commit()
        except sqlite3.Error as exc:
            logger.warning(
                "patients migration: copy ok but DROP failed: %s — "
                "next boot will skip migration (table exists) and "
                "data is still safe", exc,
            )

        logger.info(
            "patients migration: moved %d rows from dicom_index.db "
            "to nexus_server.db; dropped legacy table.",
            len(legacy_rows),
        )
    finally:
        idx_conn.close()


def _age_to_group(age: int) -> str:
    """Convert raw age to 10-year band — matches the DICOM ingest
    path's grouping so the rail labels are consistent."""
    if age <= 0:
        return ""
    if age >= 90:
        return "90+"
    decade = (age // 10) * 10
    return f"{decade}-{decade + 9}"


# ── Models ──────────────────────────────────────────────────────────


class RegisterManualPatientRequest(BaseModel):
    """Body of the manual-registration POST. All fields optional except
    initials OR mrn (at least one is required so we have something to
    hash). The dialog UI enforces this client-side too."""
    initials:        str = Field("", max_length=64)
    mrn:             str = Field("", max_length=128)
    age:             int = Field(0, ge=0, le=130)
    sex:             str = Field("", max_length=8)
    chief_complaint: str = Field("", max_length=2000)
    notes:           str = Field("", max_length=5000)
    # #181 — when the desktop passes the active session_id we
    # also UPDATE sessions SET patient_hash, so subsequent file
    # uploads in this chat inherit the patient_hash automatically
    # (via the #178 session → uploads.patient_hash join).
    session_id:      str = Field("", max_length=128)


class RegisterManualPatientResponse(BaseModel):
    patient_hash: str
    age_group:    str


class PatientDetail(BaseModel):
    """Full per-patient view used by the Patients main canvas. Combines
    the manually-entered fields with derived study aggregates."""
    patient_hash:      str
    initials:          str
    mrn:               str
    age_value:         int
    age_group:         str
    sex:               str
    chief_complaint:   str
    notes:             str
    created_at:        int
    updated_at:        int
    study_count:       int
    latest_study_date: str
    latest_modality:   str
    last_seen_at:      int
    source:            str  # "manual" / "dicom" / "both"


# ── Endpoints ───────────────────────────────────────────────────────


@router.post(
    "/patients/register-manual",
    response_model=RegisterManualPatientResponse,
)
async def register_manual_patient(
    req: RegisterManualPatientRequest,
    current_user: str = Depends(get_current_user),
) -> RegisterManualPatientResponse:
    """Register a patient typed in by the medic (no DICOM yet).

    Hash rule:
      * If MRN is provided, hash it directly (same function as the
        DICOM PatientID path → future PACS uploads of the same MRN
        collide and merge automatically).
      * Else, hash a normalised concatenation of (initials | age |
        sex). This is deterministic so re-registering the same
        patient finds the existing row instead of creating a dup.

    Returns the patient_hash so the desktop can immediately bind the
    active session to it (so subsequent file uploads inherit the
    hash via the session→uploads.patient_hash join from #178).
    """
    init_patients_table()

    initials = (req.initials or "").strip()
    mrn      = (req.mrn or "").strip()
    sex      = (req.sex or "").strip().upper()[:1]  # M/F/O
    if sex not in ("M", "F", "O"):
        sex = ""

    if not initials and not mrn:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide initials or MRN (at least one is required).",
        )

    # Stable identifier for hashing. MRN wins if present.
    if mrn:
        identity_key = f"mrn:{mrn}"
    else:
        identity_key = (
            f"manual:{initials.upper()}|{req.age}|{sex}"
        )
    patient_hash = _hash_patient_id(identity_key)
    age_group = _age_to_group(req.age)
    now = int(time.time())

    # F-merge-patients-db — patients table now lives in nexus_server.db.
    from nexus_server.database import get_db_connection
    with get_db_connection() as c:
        # UPSERT — re-registering with new fields refreshes the row
        # rather than failing.
        existing = c.execute(
            "SELECT created_at FROM patients "
            "WHERE user_id = ? AND patient_hash = ?",
            (current_user, patient_hash),
        ).fetchone()
        if existing:
            c.execute(
                """
                UPDATE patients
                   SET initials = ?, mrn = ?, age_group = ?,
                       age_value = ?, sex = ?, chief_complaint = ?,
                       notes = ?, updated_at = ?
                 WHERE user_id = ? AND patient_hash = ?
                """,
                (initials, mrn, age_group, req.age, sex,
                 req.chief_complaint, req.notes, now,
                 current_user, patient_hash),
            )
        else:
            c.execute(
                """
                INSERT INTO patients
                  (patient_hash, user_id, initials, mrn,
                   age_group, age_value, sex, chief_complaint,
                   notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (patient_hash, current_user, initials, mrn,
                 age_group, req.age, sex, req.chief_complaint,
                 req.notes, now, now),
            )
        c.commit()

    # #181 — bind the session if one was passed. Best-effort; if
    # the sessions table doesn't have the row yet (synthetic default
    # thread) this UPDATE is a no-op and the next upload will still
    # work because the upload route falls back to "" patient_hash.
    if req.session_id:
        try:
            from nexus_server.database import get_db_connection
            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE sessions SET patient_hash = ? "
                    "WHERE user_id = ? AND session_id = ?",
                    (patient_hash, current_user, req.session_id),
                )
                conn.commit()
        except Exception as e:
            logger.warning("binding session to patient failed: %s", e)

    return RegisterManualPatientResponse(
        patient_hash=patient_hash,
        age_group=age_group,
    )


# ── Delete ──────────────────────────────────────────────────────────


class DeletePatientResponse(BaseModel):
    patient_hash: str
    deleted: dict[str, int]   # per-table row counts removed


# ───────────────────────────────────────────────────────────────────────────
# F-roster-active-only — Archive / Unarchive endpoints
# ───────────────────────────────────────────────────────────────────────────
#
# Archiving is a server-side soft hide. The patient row stays — all
# clinical_graph_nodes, chat history, takeaways are preserved exactly
# as-is — but every "list patients" / "patient roster" path filters
# ``WHERE archived_at IS NULL``. The medic can un-archive at any
# time to bring the patient back.
#
# This is what the user means by "active vs archive" in cross-patient
# chat: ARCHIVED patients should NEVER appear in the cross-patient
# system prompt's PATIENT ROSTER block. We achieve that by filtering
# at the gather function (retrieval_tiers._gather_patient_roster).

class _ArchiveResponse(BaseModel):
    patient_hash: str
    archived_at: int


@router.post(
    "/patients/{patient_hash}/archive",
    response_model=_ArchiveResponse,
)
async def archive_patient(
    patient_hash: str,
    current_user: str = Depends(get_current_user),
) -> _ArchiveResponse:
    """Soft-hide the patient. Picker + cross-patient roster stop
    showing them; DB rows are untouched."""
    import time as _time
    now_ms = int(_time.time() * 1000)
    # F-merge-patients-db — read from the SHARED db.
    from nexus_server.database import get_db_connection
    with get_db_connection() as c:
        cur = c.execute(
            "UPDATE patients SET archived_at = ?, updated_at = ? "
            "WHERE user_id = ? AND patient_hash = ? "
            "  AND archived_at IS NULL",
            (now_ms, now_ms, current_user, patient_hash),
        )
        c.commit()
        if cur.rowcount == 0:
            # Either already archived OR not found. Both => 404 so
            # the UI can refresh-and-retry cleanly.
            raise HTTPException(
                status_code=404,
                detail="patient not found or already archived",
            )
    logger.info(
        "archive_patient: user=%s patient=%s archived_at=%d",
        current_user, patient_hash[:12], now_ms,
    )
    return _ArchiveResponse(patient_hash=patient_hash, archived_at=now_ms)


@router.post(
    "/patients/{patient_hash}/unarchive",
    response_model=_ArchiveResponse,
)
async def unarchive_patient(
    patient_hash: str,
    current_user: str = Depends(get_current_user),
) -> _ArchiveResponse:
    """Restore an archived patient — flips ``archived_at`` back to
    NULL. The patient reappears in the picker + cross-patient roster
    immediately. Data was never touched, so all chat history /
    findings come back unchanged."""
    import time as _time
    now_ms = int(_time.time() * 1000)
    from nexus_server.database import get_db_connection
    with get_db_connection() as c:
        cur = c.execute(
            "UPDATE patients SET archived_at = NULL, updated_at = ? "
            "WHERE user_id = ? AND patient_hash = ? "
            "  AND archived_at IS NOT NULL",
            (now_ms, current_user, patient_hash),
        )
        c.commit()
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail="patient not found or not currently archived",
            )
    logger.info(
        "unarchive_patient: user=%s patient=%s",
        current_user, patient_hash[:12],
    )
    return _ArchiveResponse(patient_hash=patient_hash, archived_at=0)


@router.delete(
    "/patients/{patient_hash}",
    response_model=DeletePatientResponse,
)
async def delete_patient(
    patient_hash: str,
    current_user: str = Depends(get_current_user),
) -> DeletePatientResponse:
    # Diagnostic log: every "delete worked but the patient is still
    # there" report has historically been one of three things —
    # (a) user_id drift between upload-time and delete-time,
    # (b) row in dicom_index.db but DELETE was hitting nexus_server.db,
    # (c) stale sidecar binary still serving the old endpoint code.
    # Print enough at INFO to disambiguate (b) and (c) on the fly, and
    # enough at INFO + the row counts in the return to disambiguate (a).
    logger.info(
        "delete_patient: enter user_id=%s patient_hash=%s",
        current_user, patient_hash[:12],
    )
    """Forget a patient. Scoped to the calling user — we never cross the
    (user_id, patient_hash) tuple, so one medic deleting "patient #3"
    cannot affect another medic with the same hash.

    What we touch (each is best-effort + idempotent — missing tables /
    missing rows just count as 0):

      - ``patients``                  manual registration row
      - ``dicom_studies``             DICOM-derived aggregate rows
      - ``uploads``                   files bound to this patient_hash
      - ``patient_memory``            per-patient memory blob
      - ``clinical_graph_nodes``      M3 graph projection
      - ``sessions``                  un-bind (set patient_hash = "")
                                       rather than delete — the chat
                                       history outlives the patient
                                       record.

    Returns per-table counts so the UI can show a meaningful toast and
    so users debugging "why didn't my delete work?" have a paper trail.

    Note: the underlying ``twin_event_log`` is append-only and is NOT
    touched. The graph and other projections being deleted here are
    rebuildable by replaying the event log if the medic ever wants the
    record back. This is what makes "delete" recoverable in principle —
    we're forgetting from projections, not editing history.
    """
    deleted: dict[str, int] = {}

    def _delete(conn: sqlite3.Connection, table: str, where: str, params: tuple) -> int:
        try:
            cur = conn.execute(f"DELETE FROM {table} WHERE {where}", params)
            return cur.rowcount or 0
        except sqlite3.Error:
            # Table doesn't exist on this deployment yet — that's fine,
            # the user just hasn't generated any rows for it.
            return 0

    # ── Patients (now in SHARED db, F-merge-patients-db) ──
    from nexus_server.database import get_db_connection
    with get_db_connection() as conn:
        deleted["patients"] = _delete(
            conn, "patients",
            "user_id = ? AND patient_hash = ?",
            (current_user, patient_hash),
        )
        conn.commit()

    # ── DICOM index DB (dicom_studies + dicom_series + dicom_instances) ──
    # dicom_studies still lives in `_index_db_path()` (dicom_index.db).
    # The previous version of this function deleted `dicom_studies` from
    # the wrong file, so the row stayed visible in the list endpoint
    # forever — the medic saw "delete succeeded" but the patient never
    # disappeared. These three must hit dicom_index.db.
    with _conn() as conn:
        deleted["dicom_studies"] = _delete(
            conn, "dicom_studies",
            "user_id = ? AND patient_hash = ?",
            (current_user, patient_hash),
        )
        # Series + instances are FK-referenced to dicom_studies. We
        # walked dicom_studies above; any referenced rows are now
        # orphaned, so clean them up explicitly (SQLite doesn't enforce
        # FK cascades unless PRAGMA foreign_keys=ON, which we don't
        # rely on here).
        try:
            cur = conn.execute(
                "DELETE FROM dicom_series "
                "WHERE study_id NOT IN (SELECT study_id FROM dicom_studies)",
            )
            deleted["dicom_series_orphans"] = cur.rowcount or 0
        except sqlite3.Error:
            deleted["dicom_series_orphans"] = 0
        try:
            cur = conn.execute(
                "DELETE FROM dicom_instances "
                "WHERE study_id NOT IN (SELECT study_id FROM dicom_studies)",
            )
            deleted["dicom_instances_orphans"] = cur.rowcount or 0
        except sqlite3.Error:
            deleted["dicom_instances_orphans"] = 0
        conn.commit()

    # ── Shared DB (uploads, patient_memory, clinical_graph_nodes, sessions) ──
    try:
        with get_db_connection() as conn:
            deleted["uploads"] = _delete(
                conn, "uploads",
                "user_id = ? AND patient_hash = ?",
                (current_user, patient_hash),
            )
            deleted["patient_memory"] = _delete(
                conn, "patient_memory",
                "user_id = ? AND patient_hash = ?",
                (current_user, patient_hash),
            )
            deleted["clinical_graph_nodes"] = _delete(
                conn, "clinical_graph_nodes",
                "user_id = ? AND patient_hash = ?",
                (current_user, patient_hash),
            )
            # Layer 1 provenance — the audit edges that anchor each
            # node to its source quote. If we drop the nodes but
            # leave provenance behind, the next clinical_graph_nodes
            # for a re-registered patient with the same hash inherits
            # stale evidence pointers. Belt + braces.
            deleted["node_provenance"] = _delete(
                conn, "node_provenance",
                "user_id = ? AND patient_hash = ?",
                (current_user, patient_hash),
            )
            deleted["clinical_graph_edges"] = _delete(
                conn, "clinical_graph_edges",
                "user_id = ? AND patient_hash = ?",
                (current_user, patient_hash),
            )
            # Layer 2 — practitioner observations tied to this patient.
            # The aggregated practitioner_facts row is keyed on
            # (user_id, fact_kind, pattern_key) and intentionally
            # un-tied to a specific patient — that's the privacy
            # invariant of Layer 2 (patterns generalise across cases).
            # So we touch observations only. distinct_patient_count
            # will adjust at the next distill pass.
            deleted["practitioner_observations"] = _delete(
                conn, "practitioner_observations",
                "user_id = ? AND patient_hash = ?",
                (current_user, patient_hash),
            )
            # Layer 2b — chat takeaways scoped to this patient. After
            # the medic deletes the patient, these qualitative
            # insights are about a record they explicitly chose to
            # forget; keeping them would be confusing AND would
            # surface in cross-research chats as "Nexus learned X
            # about a patient who no longer exists." Drop them.
            deleted["chat_takeaways"] = _delete(
                conn, "chat_takeaways",
                "user_id = ? AND scope_kind = 'patient' AND scope_ref = ?",
                (current_user, patient_hash),
            )
            # Sessions: un-bind, don't delete — chat history is its own
            # source of record. Try both legacy `sessions` and
            # canonical `nexus_sessions` table names since the rename
            # is in flight.
            for table in ("sessions", "nexus_sessions"):
                try:
                    cur = conn.execute(
                        f"UPDATE {table} SET patient_hash = '' "
                        f"WHERE user_id = ? AND patient_hash = ?",
                        (current_user, patient_hash),
                    )
                    deleted[f"{table}_unbound"] = cur.rowcount or 0
                except sqlite3.Error as e:
                    logger.debug("clearing patient_hash in %s failed: %s", table, e)  # table doesn't exist on this deployment
            conn.commit()
    except Exception as exc:
        # Shared DB unavailable / schema mismatch / etc. Log instead of
        # silently swallowing — the previous `pass` made every "delete
        # found nothing" bug look identical to "DB literally fine but
        # no rows for this user", and the UI got a 404 with no clue.
        logger.warning(
            "delete_patient: shared DB block raised for user=%s phash=%s: %s",
            current_user, patient_hash[:8], exc,
        )

    # Idempotent delete: returning 404 when no projection rows match
    # confused the UI in the common case where a patient appeared in
    # the sidebar via a stale projection but the DELETE couldn't see
    # the row (e.g. user_id drift across rebuilds, or the row only
    # lives in twin_event_log which we intentionally don't touch).
    # "Forget this patient" is a soft-projection-delete contract; if
    # the projection is already absent, that's success, not failure.
    # The UI re-fetches the list right after and the patient is gone.
    return DeletePatientResponse(
        patient_hash=patient_hash,
        deleted=deleted,
    )


@router.get(
    "/patients/full",
    response_model=list[PatientDetail],
)
async def list_patients_full(
    current_user: str = Depends(get_current_user),
) -> list[PatientDetail]:
    """Full roster for the Patients main-canvas view.

    UNIONs manual entries with DICOM-derived aggregates so a patient
    typed in the New Patient dialog shows up immediately, AND a
    patient who only exists via PACS uploads shows up too. Where the
    same patient_hash appears in both sources (medic typed them in
    AND later uploaded their study), we merge the rows — manual
    fields win for demographics, DICOM aggregates win for study
    counts.
    """
    init_patients_table()

    # F-merge-patients-db — patients now lives in the shared db, while
    # dicom_studies stays in dicom_index.db. Open one conn per file and
    # merge in Python (we already did Python aggregation for dicom; the
    # only "loss" vs. a SQL JOIN is one extra round-trip on a 2-table
    # call, which is fine here).
    from nexus_server.database import get_db_connection
    with get_db_connection() as c:
        # F-roster-active-only — default filter to active patients.
        # Archived ones live in DB (recoverable) but don't surface in
        # the picker / cross-patient chat / Today bar.
        manual_rows = c.execute(
            "SELECT * FROM patients "
            "WHERE user_id = ? AND archived_at IS NULL",
            (current_user,),
        ).fetchall()

    with _conn() as c:
        # #190/#193 — pull raw rows + aggregate in Python.
        # SQLite doesn't expose outer SELECT aliases inside subqueries
        # so the previous `(SELECT … WHERE … = phash)` correlated
        # subquery threw "no such column: phash" and the whole endpoint
        # 500'd. Python aggregation is simpler + provably correct.
        raw_dicom = c.execute(
            """
            SELECT
                patient_hash, patient_age_group, patient_sex,
                study_date, modality, created_at
            FROM dicom_studies
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (current_user,),
        ).fetchall()

    # Aggregate dicom by phash. raw_dicom is newest-first so the first
    # row we see per hash IS the latest study.
    dicom_rows: list = []
    seen_hash: dict[str, dict] = {}
    for r in raw_dicom:
        phash = r["patient_hash"] if r["patient_hash"] else "_anonymous"
        if phash not in seen_hash:
            seen_hash[phash] = {
                "phash":       phash,
                "age_group":   r["patient_age_group"] or "",
                "sex":         r["patient_sex"] or "",
                "study_count": 1,
                "latest_date": r["study_date"] or "",
                "latest_mod":  r["modality"] or "",
                "last_seen":   int(r["created_at"] or 0),
            }
        else:
            d = seen_hash[phash]
            d["study_count"] += 1
            if not d["age_group"] and r["patient_age_group"]:
                d["age_group"] = r["patient_age_group"]
            if not d["sex"] and r["patient_sex"]:
                d["sex"] = r["patient_sex"]
            d["last_seen"] = max(d["last_seen"],
                                 int(r["created_at"] or 0))
    # Convert to the dict-row shape the rest of the function expects.
    dicom_rows = [
        {
            "phash":       d["phash"],
            "age_group":   d["age_group"],
            "sex":         d["sex"],
            "study_count": d["study_count"],
            "latest_date": d["latest_date"],
            "latest_mod":  d["latest_mod"],
            "last_seen":   d["last_seen"],
        }
        for d in seen_hash.values()
    ]

    by_hash: dict[str, PatientDetail] = {}

    # Seed with manual rows first.
    for r in manual_rows:
        by_hash[r["patient_hash"]] = PatientDetail(
            patient_hash=r["patient_hash"],
            initials=r["initials"] or "",
            mrn=r["mrn"] or "",
            age_value=int(r["age_value"] or 0),
            age_group=r["age_group"] or "",
            sex=r["sex"] or "",
            chief_complaint=r["chief_complaint"] or "",
            notes=r["notes"] or "",
            created_at=int(r["created_at"] or 0),
            updated_at=int(r["updated_at"] or 0),
            study_count=0,
            latest_study_date="",
            latest_modality="",
            last_seen_at=int(r["created_at"] or 0),
            source="manual",
        )

    # Layer in DICOM aggregates.
    for r in dicom_rows:
        ph = r["phash"]
        if ph in by_hash:
            d = by_hash[ph]
            d.study_count = int(r["study_count"] or 0)
            d.latest_study_date = r["latest_date"] or ""
            d.latest_modality = r["latest_mod"] or ""
            d.last_seen_at = max(d.last_seen_at, int(r["last_seen"] or 0))
            d.source = "both"
            # Backfill demographics from DICOM if the manual row left
            # them blank.
            if not d.age_group:
                d.age_group = r["age_group"] or ""
            if not d.sex:
                d.sex = r["sex"] or ""
        else:
            by_hash[ph] = PatientDetail(
                patient_hash=ph,
                initials="",
                mrn="",
                age_value=0,
                age_group=r["age_group"] or "",
                sex=r["sex"] or "",
                chief_complaint="",
                notes="",
                created_at=int(r["last_seen"] or 0),
                updated_at=int(r["last_seen"] or 0),
                study_count=int(r["study_count"] or 0),
                latest_study_date=r["latest_date"] or "",
                latest_modality=r["latest_mod"] or "",
                last_seen_at=int(r["last_seen"] or 0),
                source="dicom",
            )

    # Most-recently-touched first so the medic's current case is at
    # the top of the view.
    return sorted(
        by_hash.values(),
        key=lambda p: p.last_seen_at,
        reverse=True,
    )


@router.get(
    "/patients/{patient_hash}/detail",
    response_model=PatientDetail,
)
async def get_patient_detail(
    patient_hash: str,
    current_user: str = Depends(get_current_user),
) -> PatientDetail:
    """Single patient view. 404 if neither manual nor DICOM knows
    about the hash."""
    all_patients = await list_patients_full(current_user)
    for p in all_patients:
        if p.patient_hash == patient_hash:
            return p
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"patient {patient_hash[:12]} not found",
    )
