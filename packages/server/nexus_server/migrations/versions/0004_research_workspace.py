"""Research Workspace tables.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-15

Backs the Research Workspace feature — see
``docs/design/RESEARCH_WORKSPACE_DESIGN.md``. Lifts the application
from a single per-patient view to a per-study + per-patient dual-axis
workspace. The doctor's primary mental model becomes "research first";
each patient is one entity within (potentially) multiple studies.

This migration creates five tables + extends sessions/patient_memory
with scope columns. Everything is reconstructable from the event log
(STUDY_* event kinds), so a drop+replay rebuilds byte-identical state.

Tables created:
  research_studies         — one row per clinical study / protocol
  study_enrollments        — (study, patient) membership
  screening_evaluations    — per-trigger eligibility evaluations
  study_assessments        — planned + observed visits / data points
  study_observations       — adhoc observations (e.g. SOAP mirrors)

Extensions:
  nexus_sessions           + scope_kind / scope_id   (per design §4.1)
  patient_memory           + scope_tags              (per D16/D17)

Indices:
  by-study lookups for roster / inbox / schedule hot paths
  by-patient lookups for the Patient → Studies derived view (§3.4 D18)
  by-due_at on study_assessments for the scheduler scan loop

Per ENGINEERING_STANDARDS rule 2: schema changes flow through Alembic.
"""
from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _table_exists(bind, name: str) -> bool:
    rows = bind.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchall()
    return bool(rows)


def _column_exists(bind, table: str, column: str) -> bool:
    rows = bind.exec_driver_sql(
        f"PRAGMA table_info({table})"
    ).fetchall()
    return any(r[1] == column for r in rows)


def upgrade() -> None:
    bind = op.get_bind()

    # ── research_studies ────────────────────────────────────────────
    if not _table_exists(bind, "research_studies"):
        bind.exec_driver_sql(
            """
            CREATE TABLE research_studies (
                user_id              TEXT NOT NULL,
                study_id             TEXT NOT NULL,
                display_name         TEXT NOT NULL,
                short_code           TEXT NOT NULL,
                phase                TEXT NOT NULL DEFAULT '',
                status               TEXT NOT NULL DEFAULT 'draft',
                target_n             INTEGER,
                protocol_doc_id      TEXT,
                protocol_text        TEXT,
                protocol_summary     TEXT,
                primary_endpoint     TEXT,
                secondary_endpoints_json  TEXT NOT NULL DEFAULT '[]',
                inclusion_json       TEXT NOT NULL DEFAULT '[]',
                exclusion_json       TEXT NOT NULL DEFAULT '[]',
                schedule_json        TEXT NOT NULL DEFAULT '[]',
                stop_rules_json      TEXT NOT NULL DEFAULT '{}',
                arms_json            TEXT NOT NULL DEFAULT '[]',
                created_at           INTEGER NOT NULL,
                updated_at           INTEGER NOT NULL,
                archived_at          INTEGER,
                PRIMARY KEY (user_id, study_id)
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_research_studies_user "
            "ON research_studies (user_id, status)"
        )

    # ── study_enrollments ───────────────────────────────────────────
    if not _table_exists(bind, "study_enrollments"):
        bind.exec_driver_sql(
            """
            CREATE TABLE study_enrollments (
                user_id              TEXT NOT NULL,
                study_id             TEXT NOT NULL,
                patient_hash         TEXT NOT NULL,
                enrollment_seq       INTEGER NOT NULL,
                status               TEXT NOT NULL,
                arm                  TEXT,
                enrolled_at          INTEGER NOT NULL,
                withdrawn_at         INTEGER,
                withdrawal_reason    TEXT,
                consent_signed_at    INTEGER,
                baseline_completed_at INTEGER,
                notes                TEXT,
                PRIMARY KEY (user_id, study_id, patient_hash)
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_study_enrollments_patient "
            "ON study_enrollments (user_id, patient_hash)"
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_study_enrollments_study_status "
            "ON study_enrollments (user_id, study_id, status)"
        )

    # ── screening_evaluations ───────────────────────────────────────
    if not _table_exists(bind, "screening_evaluations"):
        bind.exec_driver_sql(
            """
            CREATE TABLE screening_evaluations (
                user_id                  TEXT NOT NULL,
                study_id                 TEXT NOT NULL,
                patient_hash             TEXT NOT NULL,
                evaluated_at             INTEGER NOT NULL,
                triggered_by_event_id    TEXT,
                per_criterion_json       TEXT NOT NULL,
                overall_status           TEXT NOT NULL,
                llm_recommendation_json  TEXT,
                decision                 TEXT NOT NULL DEFAULT 'pending',
                decision_at              INTEGER,
                decision_by              TEXT,
                decision_reason          TEXT,
                snooze_until             INTEGER,
                PRIMARY KEY (user_id, study_id, patient_hash, evaluated_at)
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_screening_study_decision "
            "ON screening_evaluations (user_id, study_id, decision, evaluated_at)"
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_screening_patient "
            "ON screening_evaluations (user_id, patient_hash, evaluated_at)"
        )

    # ── study_assessments (planned visit data points) ───────────────
    if not _table_exists(bind, "study_assessments"):
        bind.exec_driver_sql(
            """
            CREATE TABLE study_assessments (
                user_id                  TEXT NOT NULL,
                study_id                 TEXT NOT NULL,
                patient_hash             TEXT NOT NULL,
                visit_id                 TEXT NOT NULL,
                assessment_kind          TEXT NOT NULL,
                status                   TEXT NOT NULL DEFAULT 'planned',
                due_at                   INTEGER NOT NULL,
                completed_at             INTEGER,
                source_node_ids_json     TEXT NOT NULL DEFAULT '[]',
                notes                    TEXT,
                PRIMARY KEY (user_id, study_id, patient_hash, visit_id, assessment_kind)
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_study_assessments_due "
            "ON study_assessments (user_id, due_at, status)"
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_study_assessments_roster "
            "ON study_assessments (user_id, study_id, patient_hash)"
        )

    # ── study_observations (event-driven mirrors) ───────────────────
    if not _table_exists(bind, "study_observations"):
        bind.exec_driver_sql(
            """
            CREATE TABLE study_observations (
                observation_id           TEXT PRIMARY KEY,
                user_id                  TEXT NOT NULL,
                study_id                 TEXT NOT NULL,
                patient_hash             TEXT NOT NULL,
                created_at               INTEGER NOT NULL,
                category                 TEXT NOT NULL,
                ae_grade                 TEXT,
                ae_grade_confirmed       INTEGER NOT NULL DEFAULT 0,
                is_dlt                   INTEGER,
                source_kind              TEXT NOT NULL,
                source_node_id           TEXT,
                source_text_excerpt      TEXT,
                llm_classification_json  TEXT,
                linked_assessment_visit_id TEXT,
                medic_confirmed_at       INTEGER,
                unlinked_at              INTEGER,
                unlink_reason            TEXT
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_study_obs_study "
            "ON study_observations (user_id, study_id, created_at DESC)"
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_study_obs_patient "
            "ON study_observations (user_id, patient_hash, created_at DESC)"
        )

    # ── nexus_sessions: scope_kind / scope_id ───────────────────────
    if _table_exists(bind, "nexus_sessions"):
        if not _column_exists(bind, "nexus_sessions", "scope_kind"):
            bind.exec_driver_sql(
                "ALTER TABLE nexus_sessions ADD COLUMN "
                "scope_kind TEXT NOT NULL DEFAULT 'patient'"
            )
        if not _column_exists(bind, "nexus_sessions", "scope_id"):
            bind.exec_driver_sql(
                "ALTER TABLE nexus_sessions ADD COLUMN "
                "scope_id TEXT NOT NULL DEFAULT ''"
            )
        # Backfill: rows that have a patient_hash but empty scope_id
        # should get scope_id = patient_hash so the per-session
        # retrieval keeps working without any other change. Skip when
        # the legacy patient_hash column has never been added (fresh
        # installs that haven't gone through patient_memory's lazy
        # ALTER yet).
        if _column_exists(bind, "nexus_sessions", "patient_hash"):
            bind.exec_driver_sql(
                "UPDATE nexus_sessions "
                "SET scope_id = patient_hash "
                "WHERE scope_kind = 'patient' "
                "  AND (scope_id IS NULL OR scope_id = '') "
                "  AND patient_hash IS NOT NULL "
                "  AND patient_hash <> ''"
            )

    # ── patient_memory: scope_tags (D16/D17) ────────────────────────
    if _table_exists(bind, "patient_memory"):
        if not _column_exists(bind, "patient_memory", "scope_tags"):
            bind.exec_driver_sql(
                "ALTER TABLE patient_memory ADD COLUMN "
                "scope_tags TEXT NOT NULL DEFAULT '[]'"
            )

    # ── patients: email_address + email_reminder_consent (Phase 3) ──
    if _table_exists(bind, "patients"):
        if not _column_exists(bind, "patients", "email_address"):
            bind.exec_driver_sql(
                "ALTER TABLE patients ADD COLUMN "
                "email_address TEXT NOT NULL DEFAULT ''"
            )
        if not _column_exists(bind, "patients", "email_reminder_consent"):
            bind.exec_driver_sql(
                "ALTER TABLE patients ADD COLUMN "
                "email_reminder_consent INTEGER NOT NULL DEFAULT 0"
            )


def downgrade() -> None:
    # Soft no-op: dropping research tables loses doctor-confirmed
    # enrollments and protocol drafts. ENGINEERING_STANDARDS rule 2:
    # data loss on downgrade is never automatic.
    pass
