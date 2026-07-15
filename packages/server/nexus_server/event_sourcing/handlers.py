"""Replay handlers — one per (EventKind, version) pair.

Each handler takes (cursor, event_dict) and applies the event's effect to
projection tables. Determinism contract: every handler is pure given
its inputs (event payload + current projection state) and never calls
LLMs / network / external services.

M0 status
---------

Per task #195, M0 ships:
- Real handlers for the chat substrate (USER_MESSAGE, ASSISTANT_RESPONSE,
  TOOL_CALL) so chat_ingester can exercise the full path
- Real handlers for Layer 1 graph mutations (NODE_ADDED, EDGE_ADDED,
  PROVENANCE_RECORDED, NODE_WEIGHT_CHANGED, NODE_RETRACTED, etc.)
- Real handlers for Layer 2 (PRACTITIONER_*) — write to facts/observations
- Real handlers for PATIENT_REGISTERED
- No-op handlers for everything else (DICOM ingestion, conflict resolution,
  imaging, persistence ops). They write nothing but must exist so replay
  doesn't halt on UnknownEventKindError. M1-M9 phases replace them.

Adding handlers
---------------

To add a real handler:
1. Write the function with signature (cur, event) -> None
2. Register at module import time via register_handler(kind, version, fn)

CI checks coverage: every registered (kind, version) in EVENT_REGISTRY
must have a handler in REPLAY_HANDLERS.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from nexus_server.event_sourcing.event_kinds import EventKind
from nexus_server.event_sourcing.replay import register_handler

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Generic helpers
# ─────────────────────────────────────────────────────────────────────

def _noop(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    """Handler that records the event happened but mutates no projection.

    Used for events that:
    - Carry archival information only (ingestion_llm_response — the
      verbatim LLM output is stored in the event, no projection needed)
    - Are processed by handlers of later events (e.g. ingestion_started
      is a marker; the node_added events that follow do the projection work)
    - Are stub events for phases not yet implemented (DICOM ingestion,
      imaging events in M0)

    Logging at debug level only — these are normal, not warnings.
    """
    pass


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    return event["payload"]


# ─────────────────────────────────────────────────────────────────────
# Chat substrate — Layer 1 chat events
# ─────────────────────────────────────────────────────────────────────

def _h_user_message(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    """USER_MESSAGE — chat substrate. No projection write needed; the
    event itself is the canonical record and ``twin_event_log.list_messages``
    (Phase 2a) reads it directly from the shared ``twin_event_log`` table.

    The earlier dual-write that mirrored to a per-user SQLite file has
    been removed — see ``docs/design/EVENT_LOG_UNIFICATION.md``. If you
    need to re-introduce per-user files (e.g. for chain backup), do it
    as a derived export job, not a synchronous handler side effect.
    """
    pass


def _h_assistant_response(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    """ASSISTANT_RESPONSE — same as USER_MESSAGE: no projection write,
    the shared event log is the source of truth."""
    pass


def _h_tool_call(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    pass


def _h_agent_suggestion(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    pass


def _h_suggestion_resolved(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    """Triggers Layer 2 calibration extraction; the practitioner_ingester
    reads suggestion_resolved + the linked agent_suggestion events.
    Projection-side: no direct write; handler is structural."""
    pass


# ─────────────────────────────────────────────────────────────────────
# Ingestion archival — no projection write; the events ARE the archive
# ─────────────────────────────────────────────────────────────────────

def _h_dicom_uploaded(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    """DICOM file uploaded; file is content-addressed on disk by sha256.
    Projection write happens in subsequent NODE_ADDED events for the
    study/series/key_image nodes."""
    pass


def _h_ingestion_started(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    pass


def _h_ingestion_llm_response(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    """The LOAD-BEARING event for replay: the verbatim LLM output is in
    payload.raw_output_text. No projection write — the archival IS the
    projection. Downstream NODE_ADDED events derive their content_json
    from this same archived output."""
    pass


def _h_ingestion_completed(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    pass


# ─────────────────────────────────────────────────────────────────────
# Layer 1 graph mutations — the workhorse projection writers
# ─────────────────────────────────────────────────────────────────────

def _h_node_added(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    p = _payload(event)
    cur.execute(
        "INSERT INTO clinical_graph_nodes "
        "(user_id, patient_hash, node_id, node_type, content_json, "
        " embedding_ref, weight, encounter_id, created_at, updated_at, "
        " originating_event_idx) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event["user_id"],
            event["patient_hash"],
            # node_id derived from event_idx — guarantees uniqueness +
            # determinism on replay (same event_idx → same node_id).
            event["event_idx"],
            p["node_type"],
            json.dumps(p["content_json"], sort_keys=True),
            p.get("embedding_ref"),
            p.get("weight", 1.0),
            p.get("encounter_id"),
            event["ts"],
            event["ts"],
            p.get("originating_event_idx", event["event_idx"]),
        ),
    )


def _h_node_updated(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    p = _payload(event)
    cur.execute(
        "UPDATE clinical_graph_nodes "
        "SET content_json = ?, updated_at = ? "
        "WHERE user_id = ? AND patient_hash = ? AND node_id = ?",
        (
            json.dumps(p["after_state_json"], sort_keys=True),
            event["ts"],
            event["user_id"],
            event["patient_hash"],
            p["node_id"],
        ),
    )


def _h_node_weight_changed(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    p = _payload(event)
    cur.execute(
        "UPDATE clinical_graph_nodes SET weight = ?, updated_at = ? "
        "WHERE user_id = ? AND patient_hash = ? AND node_id = ?",
        (p["after_weight"], event["ts"], event["user_id"],
         event["patient_hash"], p["node_id"]),
    )


def _h_node_retracted(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    p = _payload(event)
    # Mark provenance as retracted; the node row itself stays for audit.
    cur.execute(
        "UPDATE node_provenance "
        "SET retracted_at = ?, retracted_by_user = ?, retracted_reason = ? "
        "WHERE user_id = ? AND patient_hash = ? AND node_id = ?",
        (event["ts"], p["retracted_by_user"], p["reason"],
         event["user_id"], event["patient_hash"], p["node_id"]),
    )
    if cur.rowcount == 0:
        logger.warning(
            "NODE_RETRACTED event_idx=%s: no provenance row found for "
            "user_id=%s patient_hash=%s node_id=%s — node stays visible; "
            "possible ordering issue or partial replay.",
            event.get("event_idx"), event.get("user_id"),
            event.get("patient_hash"), p.get("node_id"),
        )


def _h_edge_added(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    p = _payload(event)
    cur.execute(
        "INSERT OR REPLACE INTO clinical_graph_edges "
        "(user_id, patient_hash, src_node, dst_node, kind, weight, "
        " created_at, originating_event_idx) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event["user_id"], event["patient_hash"],
            p["src_node"], p["dst_node"], p["kind"], p.get("weight", 1.0),
            event["ts"], event["event_idx"],
        ),
    )


def _h_edge_updated(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    p = _payload(event)
    cur.execute(
        "UPDATE clinical_graph_edges SET weight = ? "
        "WHERE user_id = ? AND patient_hash = ? AND src_node = ? "
        "  AND dst_node = ? AND kind = ?",
        (p["after_weight"], event["user_id"], event["patient_hash"],
         p["src_node"], p["dst_node"], p["kind"]),
    )


def _h_edge_removed(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    p = _payload(event)
    cur.execute(
        "DELETE FROM clinical_graph_edges "
        "WHERE user_id = ? AND patient_hash = ? AND src_node = ? "
        "  AND dst_node = ? AND kind = ?",
        (event["user_id"], event["patient_hash"],
         p["src_node"], p["dst_node"], p["kind"]),
    )


def _h_provenance_recorded(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    p = _payload(event)
    cur.execute(
        "INSERT INTO node_provenance "
        "(user_id, patient_hash, node_id, source_kind, source_ref, "
        " source_locator_json, evidence_quote, extracted_by_user, "
        " extracted_at, extraction_model, extraction_prompt_id, "
        " confidence, redaction_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event["user_id"], event["patient_hash"], p["node_id"],
            p["source_kind"], p["source_ref"],
            json.dumps(p["source_locator_json"], sort_keys=True)
                if not isinstance(p["source_locator_json"], str)
                else p["source_locator_json"],
            p["evidence_quote"], p["extracted_by_user"], p["extracted_at"],
            p["extraction_model"], p["extraction_prompt_id"],
            p["confidence"], p["redaction_version"],
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# Layer 1 derived decisions — M0 stubs; M2/M3 fills in
# ─────────────────────────────────────────────────────────────────────

def _h_anatomical_region_normalized(cur, event):  pass  # noqa: D401
def _h_equivalence_merged(cur, event):  pass
def _h_conflict_detected(cur, event):  pass


def _h_conflict_resolved(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    """Conflict resolution emits superseded_by relationships in provenance.
    M0 stub; full four-axis impl in M3."""
    p = _payload(event)
    decision = p["decision"]  # 'prefer_a' | 'prefer_b' | 'flag_for_medic' | 'merge'
    if decision in ("prefer_a", "prefer_b"):
        nodes = p["nodes"]
        winner = nodes[0] if decision == "prefer_a" else nodes[1]
        loser = nodes[1] if decision == "prefer_a" else nodes[0]
        cur.execute(
            "UPDATE node_provenance SET superseded_by_node = ? "
            "WHERE user_id = ? AND patient_hash = ? AND node_id = ?",
            (winner, event["user_id"], event["patient_hash"], loser),
        )


def _h_cross_study_compare_run(cur, event):  pass


# ─────────────────────────────────────────────────────────────────────
# Layer 2 — Practitioner Memory
# ─────────────────────────────────────────────────────────────────────

def _h_practitioner_observation_emitted(
    cur: sqlite3.Cursor, event: dict[str, Any],
) -> None:
    p = _payload(event)
    cur.execute(
        "INSERT INTO practitioner_observations "
        "(user_id, patient_hash, fact_kind, pattern_key, observed_at, "
        " source_encounter_id, evidence_quote, "
        " extraction_model, extraction_prompt_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event["user_id"], event["patient_hash"],
            p["fact_kind"], p["pattern_key"], event["ts"],
            p["source_encounter_id"], p["evidence_quote"],
            p.get("extraction_model", "unknown"),
            p.get("extraction_prompt_id", "unknown"),
        ),
    )


def _h_practitioner_candidate_surfaced(
    cur: sqlite3.Cursor, event: dict[str, Any],
) -> None:
    p = _payload(event)
    # Upsert as un-confirmed candidate.
    cur.execute(
        "INSERT INTO practitioner_facts "
        "(user_id, fact_kind, pattern_key, pattern_value_json, "
        " observed_count, distinct_patient_count, confidence, "
        " first_observed_at, last_reinforced_at, "
        " extraction_model, extraction_prompt_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, fact_kind, pattern_key) DO UPDATE SET "
        "  observed_count = excluded.observed_count, "
        "  distinct_patient_count = excluded.distinct_patient_count, "
        "  confidence = excluded.confidence, "
        "  last_reinforced_at = excluded.last_reinforced_at",
        (
            event["user_id"], p["fact_kind"], p["pattern_key"],
            json.dumps(p.get("pattern_value", {}), sort_keys=True),
            p.get("observed_count", p.get("distinct_count", 0)),
            p["distinct_count"], p["confidence"],
            event["ts"], event["ts"],
            p.get("extraction_model", "unknown"),
            p.get("extraction_prompt_id", "unknown"),
        ),
    )


def _h_practitioner_fact_confirmed(
    cur: sqlite3.Cursor, event: dict[str, Any],
) -> None:
    p = _payload(event)
    cur.execute(
        "UPDATE practitioner_facts SET medic_confirmed_at = ?, medic_rejected_at = NULL "
        "WHERE user_id = ? AND fact_kind = ? AND pattern_key = ?",
        (event["ts"], event["user_id"], p["fact_kind"], p["pattern_key"]),
    )


def _h_practitioner_fact_rejected(
    cur: sqlite3.Cursor, event: dict[str, Any],
) -> None:
    p = _payload(event)
    cur.execute(
        "UPDATE practitioner_facts SET medic_rejected_at = ? "
        "WHERE user_id = ? AND fact_kind = ? AND pattern_key = ?",
        (event["ts"], event["user_id"], p["fact_kind"], p["pattern_key"]),
    )


# ─────────────────────────────────────────────────────────────────────
# Layer 3 — Reference knowledge
# ─────────────────────────────────────────────────────────────────────

def _h_reference_version_ingested(
    cur: sqlite3.Cursor, event: dict[str, Any],
) -> None:
    """Records that a particular version of a reference is in use.
    Payload itself is re-downloadable from source_url; we store the
    version pointer + sha256 for verification."""
    p = _payload(event)
    cur.execute(
        "INSERT OR REPLACE INTO reference_knowledge "
        "(kind, key, content_json, source, version, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            p["kind"], p["key"],
            json.dumps({"content_sha256": p["content_sha256"]}),
            p["source_url"], p["version"], event["ts"],
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# Meta-layer / embeddings / medic UI / persistence / schema — M0 no-ops
# ─────────────────────────────────────────────────────────────────────

def _h_prompt_version_changed(cur, event):  pass
def _h_config_changed(cur, event):  pass
def _h_skill_registered(cur, event):  pass

def _h_embedding_model_changed(cur, event):  pass
def _h_chunk_embedded(cur, event):  pass
def _h_chunk_re_embedded(cur, event):  pass


def _h_patient_registered(cur: sqlite3.Cursor, event: dict[str, Any]) -> None:
    """Register the patient anchor node. Idempotent — INSERT OR IGNORE."""
    p = _payload(event)
    cur.execute(
        "INSERT OR IGNORE INTO clinical_graph_nodes "
        "(user_id, patient_hash, node_id, node_type, content_json, "
        " weight, created_at, updated_at, originating_event_idx) "
        "VALUES (?, ?, ?, 'patient', ?, 1.0, ?, ?, ?)",
        (
            event["user_id"], event.get("patient_hash") or p.get("patient_hash"), event["event_idx"],
            json.dumps({
                "patient_hash": event.get("patient_hash") or p.get("patient_hash"),
                "source":       p["source"],
                "demographics": p.get("demographics_json", {}),
            }, sort_keys=True),
            event["ts"], event["ts"], event["event_idx"],
        ),
    )


def _h_patient_pinned(cur, event):  pass
def _h_patient_unpinned(cur, event):  pass


def _h_finding_accepted_by_medic(
    cur: sqlite3.Cursor, event: dict[str, Any],
) -> None:
    """Stamp 'medic_confirmed' flag on the underlying provenance.
    Useful for the four-axis conflict resolution (axis 2).

    M3 will write to a dedicated column; for now the conflict_resolver
    queries event_log directly for ``finding_accepted_by_medic`` events
    when computing Axis 2 — see conflict_resolver._load_node."""


def _h_finding_edited_by_medic(cur, event):  pass
def _h_impression_edited(cur, event):  pass
def _h_medic_correction(cur, event):  pass

def _h_snapshot_taken(cur, event):  pass
def _h_backup_completed(cur, event):  pass
def _h_restore_performed(cur, event):  pass
def _h_export_bundle_created(cur, event):  pass
def _h_import_bundle_started(cur, event):  pass
def _h_import_bundle_completed(cur, event):  pass

def _h_schema_migration_applied(cur, event):  pass


# ─────────────────────────────────────────────────────────────────────
# Imaging (Rev-9) — M1+ phases fill in real behaviour
# ─────────────────────────────────────────────────────────────────────

def _h_image_redaction_applied(cur, event):  pass
def _h_image_extracted(cur, event):  pass
def _h_image_embedding_computed(cur, event):  pass
def _h_image_feature_extracted(cur, event):  pass
def _h_image_attached_to_context(cur, event):  pass
def _h_redaction_policy_changed(cur, event):  pass


# ─────────────────────────────────────────────────────────────────────
# Scheduled tasks (Phase 1)
# ─────────────────────────────────────────────────────────────────────
#
# Replay handlers are no-ops — the scheduled_tasks projection is
# maintained by direct SQL inside ``scheduler.create_task`` /
# ``cancel_task`` / ``_mark_fired``, not through replay handlers.
# This mirrors the chat_session / patient_registered tables where
# the source-of-truth is the projection and the event log is for
# audit / replay-to-rebuild only.
#
# If a future Phase 2 needs full replay-rebuilds for scheduled_tasks
# (e.g. for the import-bundle path), these stubs become real SQL
# writes that idempotently re-create the rows.

def _h_scheduled_task_proposed(cur, event):  pass
def _h_scheduled_task_created(cur, event):   pass
def _h_scheduled_task_fired(cur, event):     pass
def _h_scheduled_task_cancelled(cur, event): pass


# ─────────────────────────────────────────────────────────────────────
# Research Workspace — Phase 1+
#
# Like scheduled_tasks, the canonical source-of-truth for the
# research_* tables is the projection itself (written via direct SQL
# from research_router.py). These handlers exist so:
#   1. EVENT_REGISTRY coverage check passes
#   2. A future "rebuild from event log" path works
#
# Each is a no-op today; upgrading to replay-rebuild rewrites these
# as idempotent INSERT-or-UPDATE statements mirroring the router's writes.
# ─────────────────────────────────────────────────────────────────────

def _h_study_created(cur, event):                   pass
def _h_study_protocol_updated(cur, event):          pass
def _h_study_archived(cur, event):                  pass
def _h_screening_evaluated(cur, event):             pass
def _h_screening_decision_made(cur, event):         pass
def _h_study_enrolled(cur, event):                  pass
def _h_study_withdrawn(cur, event):                 pass
def _h_study_assessment_planned(cur, event):        pass
def _h_study_assessment_completed(cur, event):      pass
def _h_study_assessment_missed(cur, event):         pass
def _h_study_observation_recorded(cur, event):      pass
def _h_study_observation_confirmed(cur, event):     pass
def _h_study_observation_unlinked(cur, event):      pass
def _h_study_report_generated(cur, event):          pass


# ─────────────────────────────────────────────────────────────────────
# Writing Studio (P1)
#
# Audit-only: the doc_references projection is written via direct SQL
# in writing_router.py (same pattern as scheduled_tasks / research_*).
# ─────────────────────────────────────────────────────────────────────

def _h_doc_reference_created(cur, event):           pass


# ─────────────────────────────────────────────────────────────────────
# Register all handlers at module import time.
# ─────────────────────────────────────────────────────────────────────

_REGISTRATIONS: tuple[tuple[EventKind, str, Any], ...] = (
    # Chat
    (EventKind.USER_MESSAGE,                    "1.0", _h_user_message),
    (EventKind.ASSISTANT_RESPONSE,              "1.0", _h_assistant_response),
    (EventKind.TOOL_CALL,                       "1.0", _h_tool_call),
    (EventKind.AGENT_SUGGESTION,                "1.0", _h_agent_suggestion),
    (EventKind.SUGGESTION_RESOLVED,             "1.0", _h_suggestion_resolved),

    # Ingestion
    (EventKind.DICOM_UPLOADED,                  "1.0", _h_dicom_uploaded),
    (EventKind.INGESTION_STARTED,               "1.0", _h_ingestion_started),
    (EventKind.INGESTION_LLM_RESPONSE,          "1.0", _h_ingestion_llm_response),
    (EventKind.INGESTION_COMPLETED,             "1.0", _h_ingestion_completed),

    # Graph mutations
    (EventKind.NODE_ADDED,                      "1.0", _h_node_added),
    (EventKind.NODE_UPDATED,                    "1.0", _h_node_updated),
    (EventKind.NODE_WEIGHT_CHANGED,             "1.0", _h_node_weight_changed),
    (EventKind.NODE_RETRACTED,                  "1.0", _h_node_retracted),
    (EventKind.EDGE_ADDED,                      "1.0", _h_edge_added),
    (EventKind.EDGE_UPDATED,                    "1.0", _h_edge_updated),
    (EventKind.EDGE_REMOVED,                    "1.0", _h_edge_removed),
    (EventKind.PROVENANCE_RECORDED,             "1.0", _h_provenance_recorded),

    # Layer 1 derived
    (EventKind.ANATOMICAL_REGION_NORMALIZED,    "1.0", _h_anatomical_region_normalized),
    (EventKind.EQUIVALENCE_MERGED,              "1.0", _h_equivalence_merged),
    (EventKind.CONFLICT_DETECTED,               "1.0", _h_conflict_detected),
    (EventKind.CONFLICT_RESOLVED,               "1.0", _h_conflict_resolved),
    (EventKind.CROSS_STUDY_COMPARE_RUN,         "1.0", _h_cross_study_compare_run),

    # Layer 2
    (EventKind.PRACTITIONER_OBSERVATION_EMITTED,"1.0", _h_practitioner_observation_emitted),
    (EventKind.PRACTITIONER_CANDIDATE_SURFACED, "1.0", _h_practitioner_candidate_surfaced),
    (EventKind.PRACTITIONER_FACT_CONFIRMED,     "1.0", _h_practitioner_fact_confirmed),
    (EventKind.PRACTITIONER_FACT_REJECTED,      "1.0", _h_practitioner_fact_rejected),

    # Layer 3
    (EventKind.REFERENCE_VERSION_INGESTED,      "1.0", _h_reference_version_ingested),

    # Meta-layer
    (EventKind.PROMPT_VERSION_CHANGED,          "1.0", _h_prompt_version_changed),
    (EventKind.CONFIG_CHANGED,                  "1.0", _h_config_changed),
    (EventKind.SKILL_REGISTERED,                "1.0", _h_skill_registered),

    # Embeddings
    (EventKind.EMBEDDING_MODEL_CHANGED,         "1.0", _h_embedding_model_changed),
    (EventKind.CHUNK_EMBEDDED,                  "1.0", _h_chunk_embedded),
    (EventKind.CHUNK_RE_EMBEDDED,               "1.0", _h_chunk_re_embedded),

    # Medic UI
    (EventKind.PATIENT_REGISTERED,              "1.0", _h_patient_registered),
    (EventKind.PATIENT_PINNED,                  "1.0", _h_patient_pinned),
    (EventKind.PATIENT_UNPINNED,                "1.0", _h_patient_unpinned),
    (EventKind.FINDING_ACCEPTED_BY_MEDIC,       "1.0", _h_finding_accepted_by_medic),
    (EventKind.FINDING_EDITED_BY_MEDIC,         "1.0", _h_finding_edited_by_medic),
    (EventKind.IMPRESSION_EDITED,               "1.0", _h_impression_edited),
    (EventKind.MEDIC_CORRECTION,                "1.0", _h_medic_correction),

    # Persistence
    (EventKind.SNAPSHOT_TAKEN,                  "1.0", _h_snapshot_taken),
    (EventKind.BACKUP_COMPLETED,                "1.0", _h_backup_completed),
    (EventKind.RESTORE_PERFORMED,               "1.0", _h_restore_performed),
    (EventKind.EXPORT_BUNDLE_CREATED,           "1.0", _h_export_bundle_created),
    (EventKind.IMPORT_BUNDLE_STARTED,           "1.0", _h_import_bundle_started),
    (EventKind.IMPORT_BUNDLE_COMPLETED,         "1.0", _h_import_bundle_completed),

    # Schema
    (EventKind.SCHEMA_MIGRATION_APPLIED,        "1.0", _h_schema_migration_applied),

    # Imaging (Rev-9) — handlers present so unknown-kind doesn't halt;
    # real projection writes go in M1 / M1.5 / M1.6 / M1.7.
    (EventKind.IMAGE_REDACTION_APPLIED,         "1.0", _h_image_redaction_applied),
    (EventKind.IMAGE_EXTRACTED,                 "1.0", _h_image_extracted),
    (EventKind.IMAGE_EMBEDDING_COMPUTED,        "1.0", _h_image_embedding_computed),
    (EventKind.IMAGE_FEATURE_EXTRACTED,         "1.0", _h_image_feature_extracted),
    (EventKind.IMAGE_ATTACHED_TO_CONTEXT,       "1.0", _h_image_attached_to_context),
    (EventKind.REDACTION_POLICY_CHANGED,        "1.0", _h_redaction_policy_changed),

    # Scheduled tasks (Phase 1) — projection writes go via direct SQL
    # in scheduler.py; these handlers are no-ops for now (audit-only).
    (EventKind.SCHEDULED_TASK_PROPOSED,         "1.0", _h_scheduled_task_proposed),
    (EventKind.SCHEDULED_TASK_CREATED,          "1.0", _h_scheduled_task_created),
    (EventKind.SCHEDULED_TASK_FIRED,            "1.0", _h_scheduled_task_fired),
    (EventKind.SCHEDULED_TASK_CANCELLED,        "1.0", _h_scheduled_task_cancelled),

    # Research Workspace (Phase 1+) — projection writes go via direct
    # SQL in research_router.py; these handlers are no-ops for now
    # (audit-only). Each event still carries patient_hash for
    # patient-scoped kinds so per-patient drill-in views work.
    (EventKind.STUDY_CREATED,                   "1.0", _h_study_created),
    (EventKind.STUDY_PROTOCOL_UPDATED,          "1.0", _h_study_protocol_updated),
    (EventKind.STUDY_ARCHIVED,                  "1.0", _h_study_archived),
    (EventKind.SCREENING_EVALUATED,             "1.0", _h_screening_evaluated),
    (EventKind.SCREENING_DECISION_MADE,         "1.0", _h_screening_decision_made),
    (EventKind.STUDY_ENROLLED,                  "1.0", _h_study_enrolled),
    (EventKind.STUDY_WITHDRAWN,                 "1.0", _h_study_withdrawn),
    (EventKind.STUDY_ASSESSMENT_PLANNED,        "1.0", _h_study_assessment_planned),
    (EventKind.STUDY_ASSESSMENT_COMPLETED,      "1.0", _h_study_assessment_completed),
    (EventKind.STUDY_ASSESSMENT_MISSED,         "1.0", _h_study_assessment_missed),
    (EventKind.STUDY_OBSERVATION_RECORDED,      "1.0", _h_study_observation_recorded),
    (EventKind.STUDY_OBSERVATION_CONFIRMED,     "1.0", _h_study_observation_confirmed),
    (EventKind.STUDY_OBSERVATION_UNLINKED,      "1.0", _h_study_observation_unlinked),
    (EventKind.STUDY_REPORT_GENERATED,          "1.0", _h_study_report_generated),

    # Writing Studio (P1) — audit-only; projection via direct SQL in
    # writing_router.py.
    (EventKind.DOC_REFERENCE_CREATED,           "1.0", _h_doc_reference_created),
)


for _kind, _version, _fn in _REGISTRATIONS:
    register_handler(_kind, _version, _fn)


# Sanity check at import time — every registered event kind must have a handler.
def _verify_coverage() -> None:
    from nexus_server.event_sourcing.event_kinds import EVENT_REGISTRY
    from nexus_server.event_sourcing.replay import REPLAY_HANDLERS
    missing = [k for k in EVENT_REGISTRY if k not in REPLAY_HANDLERS]
    if missing:
        raise RuntimeError(
            f"replay handler coverage gap: {missing}. "
            f"Every (kind, version) in EVENT_REGISTRY must have a handler."
        )

_verify_coverage()
