"""Research Workspace REST API.

See docs/design/RESEARCH_WORKSPACE_DESIGN.md §6 for the full spec.

Phase 1 surface (this file):
  POST   /api/v1/research/studies                 — create a study
  GET    /api/v1/research/studies                 — list studies
  GET    /api/v1/research/studies/{study_id}      — detail
  PATCH  /api/v1/research/studies/{study_id}      — update (status / protocol)
  DELETE /api/v1/research/studies/{study_id}      — archive (soft-delete)

  GET    /api/v1/research/studies/{study_id}/roster
  POST   /api/v1/research/studies/{study_id}/enrollments
  DELETE /api/v1/research/studies/{study_id}/enrollments/{patient_hash}

  GET    /api/v1/research/studies/{study_id}/eligibility
  POST   /api/v1/research/studies/{study_id}/eligibility/rescan
  POST   /api/v1/research/studies/{study_id}/screenings/{patient_hash}/decision

  GET    /api/v1/research/studies/{study_id}/schedule
  GET    /api/v1/research/studies/{study_id}/assessments
  POST   /api/v1/research/studies/{study_id}/assessments/{visit_id}/complete

  POST   /api/v1/research/studies/{study_id}/protocol/import      (Phase 2)
  GET    /api/v1/research/studies/{study_id}/protocol/extracted   (Phase 2)

  GET    /api/v1/research/studies/{study_id}/observations
  POST   /api/v1/research/studies/{study_id}/observations          — manual AE entry
  POST   /api/v1/research/studies/{study_id}/observations/{obs_id}/confirm
  POST   /api/v1/research/studies/{study_id}/observations/{obs_id}/unlink
  GET    /api/v1/research/studies/{study_id}/safety/stop-rule-status

  GET    /api/v1/research/studies/{study_id}/reports
  POST   /api/v1/research/studies/{study_id}/reports/interim       (Phase 4)
  POST   /api/v1/research/studies/{study_id}/reports/consort       (Phase 4)
  POST   /api/v1/research/studies/{study_id}/export.xlsx           (Phase 4)

  GET    /api/v1/patients/{patient_hash}/studies                  (Patient drill-in)

Auth: every endpoint Depends(get_current_user). user_id is closed over
server-side so cross-tenant reads/writes are impossible.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from nexus_server.auth import get_current_user
from nexus_server.database import get_db_connection
from nexus_server.event_sourcing import EventKind, Store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/research", tags=["research"])

# A separate "patients-side" view of research participation. Mounted on
# the existing /api/v1/patients prefix to keep the Patient → Studies
# endpoint colocated with the rest of the patient API (D18).
patients_studies_router = APIRouter(
    prefix="/api/v1/patients", tags=["research", "patients"]
)


# ─────────────────────────────────────────────────────────────────────
# Wire shapes
# ─────────────────────────────────────────────────────────────────────

class CriterionDef(BaseModel):
    """One inclusion / exclusion criterion (see design §4.1)."""
    id:   str
    text: str
    kind: Literal["auto-rule", "auto-llm", "manual"]
    rule_dsl:        Optional[str] = None
    llm_prompt:      Optional[str] = None
    evidence_sources: Optional[List[str]] = None


class ScheduleVisitDef(BaseModel):
    """One visit / assessment slot in the protocol schedule."""
    label:              str
    offset_days:        int
    assessments:        List[str] = Field(default_factory=list)
    repeat_every_days:  Optional[int] = None
    repeat_until_days:  Optional[int] = None


class StudyCreateRequest(BaseModel):
    display_name: str
    short_code:   str
    phase:        str = ""
    target_n:     Optional[int] = None
    primary_endpoint: Optional[str] = None
    secondary_endpoints: List[str] = Field(default_factory=list)
    inclusion:    List[CriterionDef] = Field(default_factory=list)
    exclusion:    List[CriterionDef] = Field(default_factory=list)
    schedule:     List[ScheduleVisitDef] = Field(default_factory=list)
    arms:         List[dict] = Field(default_factory=list)
    stop_rules:   dict = Field(default_factory=dict)
    protocol_summary: Optional[str] = None


class StudyUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    short_code:   Optional[str] = None
    phase:        Optional[str] = None
    target_n:     Optional[int] = None
    status:       Optional[Literal["draft", "enrolling", "closed", "paused"]] = None
    primary_endpoint: Optional[str] = None
    secondary_endpoints: Optional[List[str]] = None
    inclusion:    Optional[List[CriterionDef]] = None
    exclusion:    Optional[List[CriterionDef]] = None
    schedule:     Optional[List[ScheduleVisitDef]] = None
    arms:         Optional[List[dict]] = None
    stop_rules:   Optional[dict] = None
    protocol_summary: Optional[str] = None


class StudySummary(BaseModel):
    study_id:     str
    display_name: str
    short_code:   str
    phase:        str
    status:       str
    target_n:     Optional[int]
    enrolled_count:    int
    candidate_count:   int
    created_at:        int
    updated_at:        int


class StudyDetail(StudySummary):
    primary_endpoint:    Optional[str]
    secondary_endpoints: List[str]
    protocol_summary:    Optional[str]
    protocol_doc_id:     Optional[str]
    inclusion:           List[CriterionDef]
    exclusion:           List[CriterionDef]
    schedule:            List[ScheduleVisitDef]
    arms:                List[dict]
    stop_rules:          dict


class EnrollmentRequest(BaseModel):
    patient_hash:      str
    arm:               Optional[str] = None
    consent_signed_at: Optional[int] = None
    notes:             Optional[str] = None


class EnrollmentRow(BaseModel):
    study_id:          str
    patient_hash:      str
    enrollment_seq:    int
    status:            str
    arm:               Optional[str]
    enrolled_at:       int
    withdrawn_at:      Optional[int]
    withdrawal_reason: Optional[str]
    consent_signed_at: Optional[int]
    baseline_completed_at: Optional[int]
    notes:             Optional[str]


class WithdrawRequest(BaseModel):
    reason: str = ""


class ScreeningDecisionRequest(BaseModel):
    decision:     Literal["invited", "enrolled", "excluded", "snoozed", "pending"]
    reason:       Optional[str] = None
    snooze_until: Optional[int] = None
    arm:          Optional[str] = None  # if enrolling directly


class CriterionEvaluation(BaseModel):
    kind:          Literal["auto-rule", "auto-llm", "manual"]
    verdict:       Literal["pass", "fail", "unknown"]
    confidence:    Optional[float] = None
    reasoning:     Optional[str] = None
    evidence_refs: Optional[List[str]] = None


class LlmRecommendation(BaseModel):
    overall_confidence:   float
    narrative:            str
    suggested_next_steps: List[str] = Field(default_factory=list)
    model:                str = ""
    latency_ms:           int = 0


class ScreeningRow(BaseModel):
    study_id:        str
    patient_hash:    str
    evaluated_at:    int
    overall_status:  Literal["likely_eligible", "partial", "ineligible", "manual_review"]
    per_criterion:   dict
    llm_recommendation: Optional[LlmRecommendation]
    decision:        str
    decision_at:     Optional[int]
    decision_reason: Optional[str]
    snooze_until:    Optional[int]


class AssessmentRow(BaseModel):
    study_id:        str
    patient_hash:    str
    visit_id:        str
    assessment_kind: str
    status:          str
    due_at:          int
    completed_at:    Optional[int]
    source_node_ids: List[str]
    notes:           Optional[str]


class ObservationRow(BaseModel):
    observation_id:           str
    study_id:                 str
    patient_hash:             str
    created_at:               int
    category:                 str
    ae_grade:                 Optional[str]
    ae_grade_confirmed:       bool
    is_dlt:                   Optional[bool]
    source_kind:              str
    source_node_id:           Optional[str]
    source_text_excerpt:      Optional[str]
    linked_assessment_visit_id: Optional[str]
    medic_confirmed_at:       Optional[int]
    unlinked_at:              Optional[int]
    unlink_reason:            Optional[str]


class StudyMembership(BaseModel):
    """Patient → Studies derived view (design §3.4 D18)."""
    study_id:           str
    study_short_code:   str
    study_display_name: str
    status:             str
    enrollment_seq:     Optional[int]
    arm:                Optional[str]
    enrolled_at:        Optional[int]
    withdrawn_at:       Optional[int]
    withdrawal_reason:  Optional[str]
    consent_signed_at:  Optional[int]


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


def _slugify(s: str) -> str:
    """Tiny slug helper for study_id from display_name when not given."""
    keep = []
    for ch in s.strip().lower():
        if ch.isalnum():
            keep.append(ch)
        elif ch in " -_":
            keep.append("-")
    base = "".join(keep).strip("-") or "study"
    return f"{base}-{uuid.uuid4().hex[:6]}"


def _json_list(v) -> str:
    return json.dumps([m.dict() if hasattr(m, "dict") else m for m in (v or [])],
                      ensure_ascii=False)


def _json_obj(v) -> str:
    return json.dumps(v or {}, ensure_ascii=False)


def _load_study_row(
    user_id: str, study_id: str, *, include_archived: bool = False,
) -> Optional[dict]:
    cond = "" if include_archived else "AND archived_at IS NULL"
    with get_db_connection() as conn:
        row = conn.execute(
            f"SELECT study_id, display_name, short_code, phase, status, "
            f"target_n, primary_endpoint, secondary_endpoints_json, "
            f"protocol_doc_id, protocol_summary, inclusion_json, "
            f"exclusion_json, schedule_json, arms_json, stop_rules_json, "
            f"created_at, updated_at, archived_at "
            f"FROM research_studies "
            f"WHERE user_id = ? AND study_id = ? {cond}",
            (user_id, study_id),
        ).fetchone()
    if not row:
        return None
    return {
        "study_id": row[0], "display_name": row[1], "short_code": row[2],
        "phase": row[3], "status": row[4], "target_n": row[5],
        "primary_endpoint": row[6],
        "secondary_endpoints": json.loads(row[7] or "[]"),
        "protocol_doc_id": row[8], "protocol_summary": row[9],
        "inclusion": json.loads(row[10] or "[]"),
        "exclusion": json.loads(row[11] or "[]"),
        "schedule": json.loads(row[12] or "[]"),
        "arms": json.loads(row[13] or "[]"),
        "stop_rules": json.loads(row[14] or "{}"),
        "created_at": row[15], "updated_at": row[16],
    }


def _count_enrolled(user_id: str, study_id: str) -> int:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM study_enrollments "
            "WHERE user_id = ? AND study_id = ? AND status = 'enrolled'",
            (user_id, study_id),
        ).fetchone()
    return int(row[0]) if row else 0


def _count_candidates(user_id: str, study_id: str) -> int:
    """Distinct patients with a pending screening for this study."""
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT patient_hash) FROM screening_evaluations
            WHERE user_id = ? AND study_id = ?
              AND decision = 'pending'
              AND overall_status IN ('likely_eligible','partial')
            """,
            (user_id, study_id),
        ).fetchone()
    return int(row[0]) if row else 0


def _study_summary(user_id: str, sdict: dict) -> StudySummary:
    sid = sdict["study_id"]
    return StudySummary(
        study_id=sid,
        display_name=sdict["display_name"],
        short_code=sdict["short_code"],
        phase=sdict["phase"],
        status=sdict["status"],
        target_n=sdict["target_n"],
        enrolled_count=_count_enrolled(user_id, sid),
        candidate_count=_count_candidates(user_id, sid),
        created_at=sdict["created_at"],
        updated_at=sdict["updated_at"],
    )


def _emit(user_id: str, kind: EventKind, payload: dict,
          *, patient_hash: Optional[str] = None) -> None:
    """Emit + apply through the canonical Store. patient_hash for
    patient-scoped event kinds; None for study-only kinds."""
    try:
        with get_db_connection() as conn:
            store = Store(conn)
            # Patient-scoped kinds require a patient_hash; study-only
            # kinds don't. We just pass through whatever the caller
            # supplied.
            kwargs: dict[str, Any] = {
                "kind": kind, "payload": payload,
                "apply_fn": lambda c, e: None,  # router writes projection directly
                "user_id": user_id,
            }
            if patient_hash is not None:
                kwargs["patient_hash"] = patient_hash
            store.emit_and_apply(**kwargs)
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        # Audit-log emit is best-effort; the projection write is the
        # canonical write so the application doesn't fail if the event
        # log is temporarily unavailable.
        logger.warning("research event emit failed (%s): %s", kind, exc)


# ─────────────────────────────────────────────────────────────────────
# Studies CRUD
# ─────────────────────────────────────────────────────────────────────


@router.post("/studies", status_code=201)
async def create_study(
    req: StudyCreateRequest,
    user_id: str = Depends(get_current_user),
) -> StudyDetail:
    study_id = _slugify(req.short_code or req.display_name)
    now = _now_ms()

    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO research_studies
            (user_id, study_id, display_name, short_code, phase, status,
             target_n, protocol_summary, primary_endpoint,
             secondary_endpoints_json, inclusion_json, exclusion_json,
             schedule_json, arms_json, stop_rules_json,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, study_id, req.display_name, req.short_code, req.phase,
                req.target_n, req.protocol_summary, req.primary_endpoint,
                json.dumps(req.secondary_endpoints, ensure_ascii=False),
                _json_list(req.inclusion), _json_list(req.exclusion),
                _json_list(req.schedule), _json_list(req.arms),
                _json_obj(req.stop_rules),
                now, now,
            ),
        )
        conn.commit()

    _emit(user_id, EventKind.STUDY_CREATED, {
        "study_id": study_id,
        "display_name": req.display_name,
        "short_code": req.short_code,
        "phase": req.phase,
        "target_n": req.target_n,
        "primary_endpoint": req.primary_endpoint,
    })

    return await get_study(study_id, user_id=user_id)  # type: ignore[arg-type]


@router.get("/studies")
async def list_studies(
    user_id: str = Depends(get_current_user),
    include_archived: bool = Query(False),
) -> List[StudySummary]:
    with get_db_connection() as conn:
        cond = "archived_at IS NULL" if not include_archived else "1=1"
        rows = conn.execute(
            f"SELECT study_id FROM research_studies "
            f"WHERE user_id = ? AND {cond} "
            f"ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()

    out: List[StudySummary] = []
    for r in rows:
        sdict = _load_study_row(user_id, r[0], include_archived=include_archived)
        if sdict:
            out.append(_study_summary(user_id, sdict))
    return out


@router.get("/studies/{study_id}")
async def get_study(
    study_id: str,
    user_id: str = Depends(get_current_user),
) -> StudyDetail:
    sdict = _load_study_row(user_id, study_id)
    if not sdict:
        raise HTTPException(404, "study not found")
    summary = _study_summary(user_id, sdict)
    return StudyDetail(
        **summary.dict(),
        primary_endpoint=sdict["primary_endpoint"],
        secondary_endpoints=sdict["secondary_endpoints"],
        protocol_summary=sdict["protocol_summary"],
        protocol_doc_id=sdict["protocol_doc_id"],
        inclusion=[CriterionDef(**c) for c in sdict["inclusion"]],
        exclusion=[CriterionDef(**c) for c in sdict["exclusion"]],
        schedule=[ScheduleVisitDef(**s) for s in sdict["schedule"]],
        arms=sdict["arms"],
        stop_rules=sdict["stop_rules"],
    )


@router.patch("/studies/{study_id}")
async def update_study(
    study_id: str,
    req: StudyUpdateRequest,
    user_id: str = Depends(get_current_user),
) -> StudyDetail:
    sdict = _load_study_row(user_id, study_id)
    if not sdict:
        raise HTTPException(404, "study not found")

    sets: list[str] = []
    args: list[Any] = []
    protocol_change = False

    if req.display_name is not None:
        sets.append("display_name = ?"); args.append(req.display_name)
    if req.short_code is not None:
        sets.append("short_code = ?"); args.append(req.short_code)
    if req.phase is not None:
        sets.append("phase = ?"); args.append(req.phase)
    if req.status is not None:
        sets.append("status = ?"); args.append(req.status)
    if req.target_n is not None:
        sets.append("target_n = ?"); args.append(req.target_n)
    if req.primary_endpoint is not None:
        sets.append("primary_endpoint = ?"); args.append(req.primary_endpoint)
        protocol_change = True
    if req.secondary_endpoints is not None:
        sets.append("secondary_endpoints_json = ?")
        args.append(json.dumps(req.secondary_endpoints, ensure_ascii=False))
        protocol_change = True
    if req.inclusion is not None:
        sets.append("inclusion_json = ?"); args.append(_json_list(req.inclusion))
        protocol_change = True
    if req.exclusion is not None:
        sets.append("exclusion_json = ?"); args.append(_json_list(req.exclusion))
        protocol_change = True
    if req.schedule is not None:
        sets.append("schedule_json = ?"); args.append(_json_list(req.schedule))
        protocol_change = True
    if req.arms is not None:
        sets.append("arms_json = ?"); args.append(_json_list(req.arms))
        protocol_change = True
    if req.stop_rules is not None:
        sets.append("stop_rules_json = ?"); args.append(_json_obj(req.stop_rules))
        protocol_change = True
    if req.protocol_summary is not None:
        sets.append("protocol_summary = ?"); args.append(req.protocol_summary)

    if not sets:
        return await get_study(study_id, user_id=user_id)  # type: ignore[arg-type]

    sets.append("updated_at = ?"); args.append(_now_ms())
    args.extend([user_id, study_id])

    with get_db_connection() as conn:
        conn.execute(
            f"UPDATE research_studies SET {', '.join(sets)} "
            f"WHERE user_id = ? AND study_id = ?",
            args,
        )
        conn.commit()

    if protocol_change:
        _emit(user_id, EventKind.STUDY_PROTOCOL_UPDATED, {
            "study_id": study_id,
            "inclusion_json": _json_list(req.inclusion) if req.inclusion is not None else None,
            "exclusion_json": _json_list(req.exclusion) if req.exclusion is not None else None,
            "schedule_json":  _json_list(req.schedule)  if req.schedule  is not None else None,
        })

    return await get_study(study_id, user_id=user_id)  # type: ignore[arg-type]


@router.delete("/studies/{study_id}")
async def archive_study(
    study_id: str,
    user_id: str = Depends(get_current_user),
) -> dict:
    sdict = _load_study_row(user_id, study_id)
    if not sdict:
        raise HTTPException(404, "study not found")
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE research_studies SET archived_at = ?, updated_at = ? "
            "WHERE user_id = ? AND study_id = ?",
            (_now_ms(), _now_ms(), user_id, study_id),
        )
        conn.commit()
    _emit(user_id, EventKind.STUDY_ARCHIVED, {"study_id": study_id})
    return {"status": "archived", "study_id": study_id}


# ─────────────────────────────────────────────────────────────────────
# Enrollment / Roster
# ─────────────────────────────────────────────────────────────────────


@router.post("/studies/{study_id}/enrollments", status_code=201)
async def enroll_patient(
    study_id: str,
    req: EnrollmentRequest,
    user_id: str = Depends(get_current_user),
) -> EnrollmentRow:
    sdict = _load_study_row(user_id, study_id)
    if not sdict:
        raise HTTPException(404, "study not found")

    now = _now_ms()
    with get_db_connection() as conn:
        # Idempotent: if already enrolled, return existing row.
        existing = conn.execute(
            "SELECT enrollment_seq, status FROM study_enrollments "
            "WHERE user_id = ? AND study_id = ? AND patient_hash = ?",
            (user_id, study_id, req.patient_hash),
        ).fetchone()
        if existing and existing[1] == "enrolled":
            seq = int(existing[0])
        else:
            # Allocate next enrollment_seq.
            row = conn.execute(
                "SELECT COALESCE(MAX(enrollment_seq), 0) FROM study_enrollments "
                "WHERE user_id = ? AND study_id = ?",
                (user_id, study_id),
            ).fetchone()
            seq = int(row[0]) + 1 if row else 1
            if existing:
                conn.execute(
                    """
                    UPDATE study_enrollments SET
                       status = 'enrolled',
                       arm = COALESCE(?, arm),
                       enrolled_at = ?,
                       consent_signed_at = COALESCE(?, consent_signed_at),
                       notes = COALESCE(?, notes),
                       withdrawn_at = NULL,
                       withdrawal_reason = NULL
                    WHERE user_id = ? AND study_id = ? AND patient_hash = ?
                    """,
                    (req.arm, now, req.consent_signed_at, req.notes,
                     user_id, study_id, req.patient_hash),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO study_enrollments
                    (user_id, study_id, patient_hash, enrollment_seq, status,
                     arm, enrolled_at, consent_signed_at, notes)
                    VALUES (?, ?, ?, ?, 'enrolled', ?, ?, ?, ?)
                    """,
                    (user_id, study_id, req.patient_hash, seq, req.arm, now,
                     req.consent_signed_at, req.notes),
                )
        conn.commit()

    _emit(user_id, EventKind.STUDY_ENROLLED, {
        "study_id": study_id,
        "enrollment_seq": seq,
        "arm": req.arm,
        "consent_signed_at": req.consent_signed_at,
        "notes": req.notes,
    }, patient_hash=req.patient_hash)

    # Auto-expand the schedule (Phase 3 wires up actual reminders).
    try:
        from nexus_server.research.schedule import expand_schedule
        expand_schedule(user_id, study_id, req.patient_hash, now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("schedule expansion deferred: %s", exc)

    return _enrollment_row(user_id, study_id, req.patient_hash)  # type: ignore[return-value]


@router.delete("/studies/{study_id}/enrollments/{patient_hash}")
async def withdraw_patient(
    study_id: str,
    patient_hash: str,
    req: WithdrawRequest = WithdrawRequest(),
    user_id: str = Depends(get_current_user),
) -> dict:
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            UPDATE study_enrollments SET
              status = 'withdrawn',
              withdrawn_at = ?,
              withdrawal_reason = ?
            WHERE user_id = ? AND study_id = ? AND patient_hash = ?
              AND status = 'enrolled'
            """,
            (_now_ms(), req.reason or "", user_id, study_id, patient_hash),
        )
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "no active enrollment")
    _emit(user_id, EventKind.STUDY_WITHDRAWN, {
        "study_id": study_id, "reason": req.reason,
    }, patient_hash=patient_hash)
    return {"status": "withdrawn"}


# ─────────────────────────────────────────────────────────────────────
# Overview KPIs + Recent activity (for the dark UI overview tab)
# ─────────────────────────────────────────────────────────────────────


@router.get("/studies/{study_id}/overview")
async def get_study_overview(
    study_id: str,
    user_id: str = Depends(get_current_user),
) -> dict:
    """One-shot KPIs for the overview tab. Returns enrolled/target/etc
    plus a thin median-followup estimate so the frontend doesn't have
    to issue 4 separate queries on first paint."""
    sdict = _load_study_row(user_id, study_id)
    if not sdict:
        raise HTTPException(404, "study not found")

    now = _now_ms()
    with get_db_connection() as conn:
        enrolled = conn.execute(
            "SELECT COUNT(*), AVG(enrolled_at) FROM study_enrollments "
            "WHERE user_id = ? AND study_id = ? AND status = 'enrolled'",
            (user_id, study_id),
        ).fetchone()
        enrolled_count = int(enrolled[0] or 0)
        avg_enroll_ms  = int(enrolled[1]) if enrolled[1] else 0

        candidate_count = _count_candidates(user_id, study_id)

        # "待医生" = pending candidates that are likely_eligible/partial
        # AND not snoozed (i.e. demanding attention now)
        attn_row = conn.execute(
            """
            SELECT COUNT(DISTINCT patient_hash) FROM screening_evaluations s
            WHERE user_id = ? AND study_id = ? AND decision = 'pending'
              AND overall_status IN ('likely_eligible','partial')
              AND (snooze_until IS NULL OR snooze_until <= ?)
              AND evaluated_at = (
                SELECT MAX(evaluated_at) FROM screening_evaluations
                WHERE user_id = s.user_id AND study_id = s.study_id
                  AND patient_hash = s.patient_hash
              )
            """,
            (user_id, study_id, now),
        ).fetchone()
        attn_count = int(attn_row[0] or 0) if attn_row else 0

    # Median follow-up in months (approximation: now - avg_enrolled_at)
    if enrolled_count > 0 and avg_enroll_ms > 0:
        median_followup_mo = round((now - avg_enroll_ms) / (1000 * 86400 * 30.4), 1)
    else:
        median_followup_mo = 0.0

    return {
        "study_id": study_id,
        "enrolled_count": enrolled_count,
        "target_n": sdict["target_n"],
        "candidate_count": candidate_count,
        "attention_count": attn_count,
        "median_followup_months": median_followup_mo,
        "status": sdict["status"],
        "primary_endpoint": sdict["primary_endpoint"],
    }


@router.get("/studies/{study_id}/schedule/gantt")
async def get_schedule_gantt(
    study_id: str,
    user_id: str = Depends(get_current_user),
) -> dict:
    """Gantt-shaped projection of study_assessments rows.

    Returns a grid of (patient × timepoint) cells suitable for the
    Schedule tab's bar render:

      {
        "timepoints": [{"label": "baseline", "offset_days": 0}, …],
        "rows": [
          {"patient_hash": "...", "enrollment_seq": 1,
           "cells": [{"timepoint": "baseline+0d", "status": "completed",
                      "kinds": ["pet_ct","lab_panel"]}, …]},
          …
        ]
      }

    The timepoint list comes from study.schedule_json so the columns
    are stable across patients. Each row's cells are aligned to those
    columns; missing visit_ids (e.g. a future cycle a patient hasn't
    reached) come back as ``status='future'``.
    """
    sdict = _load_study_row(user_id, study_id)
    if not sdict:
        raise HTTPException(404, "study not found")

    schedule: list[dict] = sdict.get("schedule") or []
    # Expand the schedule_json into the same visit_id labels that
    # research.schedule.expand_schedule writes (label+offset_days,
    # with optional repetition).
    timepoints: list[dict] = []
    for v in schedule:
        label = v.get("label") or "visit"
        off = int(v.get("offset_days") or 0)
        timepoints.append({"label": label, "offset_days": off,
                           "visit_id": f"{label}+{off}d"})
        rep = v.get("repeat_every_days")
        if rep:
            cur = off + int(rep)
            until = int(v.get("repeat_until_days") or 365)
            n = 2
            while cur <= until and n <= 12:   # cap at 12 reps to keep grid usable
                timepoints.append({"label": f"{label}#{n}",
                                   "offset_days": cur,
                                   "visit_id": f"{label}+{cur}d"})
                cur += int(rep)
                n += 1

    rows_out: list[dict] = []
    now_ms = _now_ms()
    with get_db_connection() as conn:
        enrollments = conn.execute(
            """
            SELECT patient_hash, enrollment_seq, status, enrolled_at
            FROM study_enrollments
            WHERE user_id = ? AND study_id = ?
              AND status IN ('enrolled','withdrawn','completed')
            ORDER BY enrollment_seq ASC
            """,
            (user_id, study_id),
        ).fetchall()
        for er in enrollments:
            patient_hash, enrollment_seq, status, enrolled_at = er
            # Pull all assessments for this patient (any visit)
            try:
                arows = conn.execute(
                    """
                    SELECT visit_id, assessment_kind, status, due_at, completed_at
                    FROM study_assessments
                    WHERE user_id = ? AND study_id = ? AND patient_hash = ?
                    """,
                    (user_id, study_id, patient_hash),
                ).fetchall()
            except Exception:  # noqa: BLE001
                arows = []

            # Index by visit_id
            by_visit: dict[str, dict] = {}
            for vr in arows:
                vid, akind, astatus, due_at, cdone = vr
                bucket = by_visit.setdefault(vid, {
                    "visit_id": vid,
                    "status": "planned",
                    "kinds": [],
                    "due_at": due_at,
                    "completed_at": cdone,
                })
                bucket["kinds"].append(akind)
                # Promote bucket status: completed > missed > overdue >
                # in_progress > planned > future
                rank = {"completed": 4, "missed": 3, "in_progress": 2,
                        "planned": 1}.get(astatus, 0)
                cur_rank = {"completed": 4, "missed": 3, "in_progress": 2,
                            "planned": 1, "future": 0}.get(bucket["status"], 0)
                if rank > cur_rank:
                    bucket["status"] = astatus

            # Build cells aligned to timepoints[]
            cells = []
            for tp in timepoints:
                vid = tp["visit_id"]
                if vid in by_visit:
                    b = by_visit[vid]
                    is_overdue = (b["status"] == "planned" and
                                  b.get("due_at") and b["due_at"] < now_ms)
                    cells.append({
                        "timepoint": vid,
                        "status": "overdue" if is_overdue else b["status"],
                        "kinds": b["kinds"],
                        "due_at": b.get("due_at"),
                        "completed_at": b.get("completed_at"),
                    })
                else:
                    cells.append({
                        "timepoint": vid,
                        "status": "future",
                        "kinds": [],
                    })

            rows_out.append({
                "patient_hash": patient_hash,
                "enrollment_seq": enrollment_seq,
                "enrollment_status": status,
                "enrolled_at": enrolled_at,
                "cells": cells,
            })

    return {"timepoints": timepoints, "rows": rows_out}


@router.get("/studies/{study_id}/recent-activity")
async def get_recent_activity(
    study_id: str,
    user_id: str = Depends(get_current_user),
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(30, ge=1, le=200),
) -> List[dict]:
    """Recent activity feed for the overview tab — collapses the most
    important events into a single timeline. Pulls from:
      * screening_evaluations (candidate added / decision made)
      * study_enrollments     (enrolled / withdrawn / screen_failed)
      * study_assessments     (completed / missed)
      * study_observations    (recorded / confirmed)
    """
    cutoff = _now_ms() - days * 86_400_000
    items: list[dict] = []
    with get_db_connection() as conn:
        # candidates added recently
        for r in conn.execute(
            """
            SELECT patient_hash, evaluated_at, overall_status
            FROM screening_evaluations
            WHERE user_id = ? AND study_id = ? AND evaluated_at >= ?
            ORDER BY evaluated_at DESC LIMIT ?
            """,
            (user_id, study_id, cutoff, limit),
        ).fetchall():
            items.append({
                "when_ms": int(r[1]),
                "kind": "candidate",
                "text": f"candidate updated: 患者 {r[0][:6]} · {r[2]}",
                "patient_hash": r[0],
            })
        # enrollments
        for r in conn.execute(
            """
            SELECT patient_hash, enrollment_seq, status, enrolled_at,
                   withdrawn_at, screen_failed_at, completed_at
            FROM study_enrollments
            WHERE user_id = ? AND study_id = ?
              AND (enrolled_at >= ? OR withdrawn_at >= ?
                   OR screen_failed_at >= ? OR completed_at >= ?)
            """,
            (user_id, study_id, cutoff, cutoff, cutoff, cutoff),
        ).fetchall():
            ph, seq, st, ea, wa, sa, ca = r
            if ea and ea >= cutoff and st == 'enrolled':
                items.append({"when_ms": int(ea), "kind": "enroll",
                              "text": f"enrolled: #{seq} 患者 {ph[:6]}",
                              "patient_hash": ph})
            if wa:
                items.append({"when_ms": int(wa), "kind": "withdraw",
                              "text": f"withdrawn: #{seq} 患者 {ph[:6]}",
                              "patient_hash": ph})
            if sa:
                items.append({"when_ms": int(sa), "kind": "screen_failed",
                              "text": f"screen-fail: 患者 {ph[:6]}",
                              "patient_hash": ph})
            if ca:
                items.append({"when_ms": int(ca), "kind": "completed",
                              "text": f"completed: #{seq} 患者 {ph[:6]}",
                              "patient_hash": ph})
        # assessments completed
        try:
            for r in conn.execute(
                """
                SELECT patient_hash, visit_id, assessment_kind, completed_at
                FROM study_assessments
                WHERE user_id = ? AND study_id = ? AND completed_at >= ?
                ORDER BY completed_at DESC LIMIT ?
                """,
                (user_id, study_id, cutoff, limit),
            ).fetchall():
                items.append({
                    "when_ms": int(r[3]),
                    "kind": "visit",
                    "text": f"完成访视: 患者 {r[0][:6]} · {r[1]} · {r[2]}",
                    "patient_hash": r[0],
                })
        except sqlite3.Error:
            pass
        # observations
        try:
            for r in conn.execute(
                """
                SELECT patient_hash, category, ae_grade, created_at
                FROM study_observations
                WHERE user_id = ? AND study_id = ? AND created_at >= ?
                  AND unlinked_at IS NULL
                ORDER BY created_at DESC LIMIT ?
                """,
                (user_id, study_id, cutoff, limit),
            ).fetchall():
                grade = r[2] or ""
                items.append({
                    "when_ms": int(r[3]),
                    "kind": "ae",
                    "text": f"观察事件: 患者 {r[0][:6]} · {r[1]}{(' · ' + grade) if grade else ''}",
                    "patient_hash": r[0],
                })
        except sqlite3.Error:
            pass

    items.sort(key=lambda it: it["when_ms"], reverse=True)
    return items[:limit]


import sqlite3  # noqa: E402  (used by the activity feed query)


@router.get("/studies/{study_id}/roster")
async def get_roster(
    study_id: str,
    user_id: str = Depends(get_current_user),
    include_withdrawn: bool = Query(False),
) -> List[EnrollmentRow]:
    cond = "" if include_withdrawn else "AND status = 'enrolled'"
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT study_id, patient_hash, enrollment_seq, status, arm,
                   enrolled_at, withdrawn_at, withdrawal_reason,
                   consent_signed_at, baseline_completed_at, notes
            FROM study_enrollments
            WHERE user_id = ? AND study_id = ? {cond}
            ORDER BY enrollment_seq ASC
            """,
            (user_id, study_id),
        ).fetchall()
    return [
        EnrollmentRow(
            study_id=r[0], patient_hash=r[1], enrollment_seq=r[2],
            status=r[3], arm=r[4], enrolled_at=r[5],
            withdrawn_at=r[6], withdrawal_reason=r[7],
            consent_signed_at=r[8], baseline_completed_at=r[9],
            notes=r[10],
        )
        for r in rows
    ]


def _enrollment_row(user_id: str, study_id: str, patient_hash: str) -> EnrollmentRow:
    with get_db_connection() as conn:
        r = conn.execute(
            """
            SELECT study_id, patient_hash, enrollment_seq, status, arm,
                   enrolled_at, withdrawn_at, withdrawal_reason,
                   consent_signed_at, baseline_completed_at, notes
            FROM study_enrollments
            WHERE user_id = ? AND study_id = ? AND patient_hash = ?
            """,
            (user_id, study_id, patient_hash),
        ).fetchone()
    if not r:
        raise HTTPException(404, "no such enrollment")
    return EnrollmentRow(
        study_id=r[0], patient_hash=r[1], enrollment_seq=r[2],
        status=r[3], arm=r[4], enrolled_at=r[5],
        withdrawn_at=r[6], withdrawal_reason=r[7],
        consent_signed_at=r[8], baseline_completed_at=r[9],
        notes=r[10],
    )


# ─────────────────────────────────────────────────────────────────────
# Eligibility (candidates + decisions)
# ─────────────────────────────────────────────────────────────────────


@router.get("/studies/{study_id}/eligibility")
async def list_candidates(
    study_id: str,
    user_id: str = Depends(get_current_user),
    decision: Optional[str] = Query(None),
) -> List[ScreeningRow]:
    """Return the most-recent screening evaluation per patient for
    this study, optionally filtered by decision."""
    args: list[Any] = [user_id, study_id]
    extra = ""
    if decision:
        extra = "AND decision = ?"
        args.append(decision)

    sql = f"""
        WITH latest AS (
            SELECT patient_hash, MAX(evaluated_at) AS m
            FROM screening_evaluations
            WHERE user_id = ? AND study_id = ?
            GROUP BY patient_hash
        )
        SELECT s.study_id, s.patient_hash, s.evaluated_at,
               s.overall_status, s.per_criterion_json,
               s.llm_recommendation_json,
               s.decision, s.decision_at, s.decision_reason, s.snooze_until
        FROM screening_evaluations s
        JOIN latest l ON l.patient_hash = s.patient_hash
                     AND l.m = s.evaluated_at
        WHERE s.user_id = ? AND s.study_id = ?
        {extra}
        ORDER BY s.evaluated_at DESC
    """
    args2 = args + [user_id, study_id]
    if decision:
        # we have to pass decision twice if we want it in main where too,
        # but we already filtered post-join; keep just original decision
        # via extra appended above.
        args2 = [user_id, study_id, user_id, study_id, decision]
    else:
        args2 = [user_id, study_id, user_id, study_id]

    with get_db_connection() as conn:
        rows = conn.execute(sql, args2).fetchall()

    out: List[ScreeningRow] = []
    for r in rows:
        llm_json = json.loads(r[5]) if r[5] else None
        out.append(ScreeningRow(
            study_id=r[0], patient_hash=r[1], evaluated_at=r[2],
            overall_status=r[3], per_criterion=json.loads(r[4] or "{}"),
            llm_recommendation=LlmRecommendation(**llm_json) if llm_json else None,
            decision=r[6], decision_at=r[7], decision_reason=r[8],
            snooze_until=r[9],
        ))
    return out


@router.post("/studies/{study_id}/eligibility/rescan")
async def rescan_eligibility(
    study_id: str,
    user_id: str = Depends(get_current_user),
) -> dict:
    """Re-evaluate every known patient against this study."""
    try:
        from nexus_server.research.eligibility import rescan_all_for_study
        n = rescan_all_for_study(user_id, study_id)
        return {"status": "ok", "patients_evaluated": n}
    except ImportError:
        return {"status": "deferred",
                "message": "eligibility engine not yet wired (Phase 2)"}


@router.post("/studies/{study_id}/screenings/{patient_hash}/decision")
async def decide_screening(
    study_id: str,
    patient_hash: str,
    req: ScreeningDecisionRequest,
    user_id: str = Depends(get_current_user),
) -> dict:
    """Persist medic's decision on a screening row + emit audit event.
    For decision='enrolled' the caller should ALSO POST to
    /enrollments — we don't auto-create the enrollment from a decision
    to keep the two surfaces explicit."""
    now = _now_ms()
    with get_db_connection() as conn:
        # Update the latest screening row for this patient/study.
        cur = conn.execute(
            """
            UPDATE screening_evaluations SET
              decision = ?, decision_at = ?, decision_by = ?,
              decision_reason = ?, snooze_until = ?
            WHERE user_id = ? AND study_id = ? AND patient_hash = ?
              AND evaluated_at = (
                SELECT MAX(evaluated_at) FROM screening_evaluations
                WHERE user_id = ? AND study_id = ? AND patient_hash = ?
              )
            """,
            (req.decision, now, user_id, req.reason or "",
             req.snooze_until,
             user_id, study_id, patient_hash,
             user_id, study_id, patient_hash),
        )
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "no screening row")
    _emit(user_id, EventKind.SCREENING_DECISION_MADE, {
        "study_id": study_id,
        "decision": req.decision,
        "reason": req.reason,
        "snooze_until": req.snooze_until,
    }, patient_hash=patient_hash)
    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────
# Schedule / Assessments
# ─────────────────────────────────────────────────────────────────────


@router.get("/studies/{study_id}/assessments")
async def list_assessments(
    study_id: str,
    user_id: str = Depends(get_current_user),
    patient_hash: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    due_before: Optional[int] = Query(None),
) -> List[AssessmentRow]:
    sql = (
        "SELECT study_id, patient_hash, visit_id, assessment_kind, status, "
        "due_at, completed_at, source_node_ids_json, notes "
        "FROM study_assessments "
        "WHERE user_id = ? AND study_id = ?"
    )
    args: list[Any] = [user_id, study_id]
    if patient_hash:
        sql += " AND patient_hash = ?"; args.append(patient_hash)
    if status_filter:
        sql += " AND status = ?"; args.append(status_filter)
    if due_before:
        sql += " AND due_at <= ?"; args.append(due_before)
    sql += " ORDER BY due_at ASC"
    with get_db_connection() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [
        AssessmentRow(
            study_id=r[0], patient_hash=r[1], visit_id=r[2],
            assessment_kind=r[3], status=r[4], due_at=r[5],
            completed_at=r[6],
            source_node_ids=json.loads(r[7] or "[]"),
            notes=r[8],
        )
        for r in rows
    ]


class AssessmentCompleteRequest(BaseModel):
    assessment_kind: str
    source_node_ids: List[str] = Field(default_factory=list)
    notes:           Optional[str] = None


@router.post("/studies/{study_id}/assessments/{visit_id}/complete")
async def complete_assessment(
    study_id: str,
    visit_id: str,
    req: AssessmentCompleteRequest,
    patient_hash: str = Query(...),
    user_id: str = Depends(get_current_user),
) -> dict:
    now = _now_ms()
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            UPDATE study_assessments SET
              status = 'completed',
              completed_at = ?,
              source_node_ids_json = ?,
              notes = COALESCE(?, notes)
            WHERE user_id = ? AND study_id = ? AND patient_hash = ?
              AND visit_id = ? AND assessment_kind = ?
            """,
            (now, json.dumps(req.source_node_ids, ensure_ascii=False),
             req.notes, user_id, study_id, patient_hash,
             visit_id, req.assessment_kind),
        )
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "no such planned assessment")
    _emit(user_id, EventKind.STUDY_ASSESSMENT_COMPLETED, {
        "study_id": study_id,
        "visit_id": visit_id,
        "assessment_kind": req.assessment_kind,
        "source_node_ids": req.source_node_ids,
        "notes": req.notes,
    }, patient_hash=patient_hash)
    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────
# Observations
# ─────────────────────────────────────────────────────────────────────


class ObservationCreateRequest(BaseModel):
    """Manual AE / observation entry — used by the Safety tab when the
    medic types in a candidate before any SOAP → AE auto-extractor is
    wired up (that pipeline is Phase 2; see design §3.3.4 + ROADMAP).
    """
    patient_hash:                str
    category:                    str
    ae_grade:                    Optional[str] = None
    is_dlt:                      Optional[bool] = None
    source_text_excerpt:         Optional[str] = None
    linked_assessment_visit_id:  Optional[str] = None


@router.post(
    "/studies/{study_id}/observations",
    status_code=status.HTTP_201_CREATED,
)
async def create_observation(
    study_id: str,
    req: ObservationCreateRequest,
    user_id: str = Depends(get_current_user),
) -> dict:
    """Record a new observation row. ``ae_grade_confirmed`` starts
    False — the medic must hit a grade button (POST .../confirm) to
    lock it in. That two-step (propose → confirm) is the design
    anti-pattern guarantee that no AE ever lands in the DLT counter
    without an explicit medic action.
    """
    obs_id = uuid.uuid4().hex
    now = _now_ms()
    with get_db_connection() as conn:
        # Sanity: study must belong to this user.
        study = conn.execute(
            "SELECT 1 FROM research_studies "
            "WHERE user_id = ? AND study_id = ?",
            (user_id, study_id),
        ).fetchone()
        if not study:
            raise HTTPException(404, "no such study")
        conn.execute(
            """
            INSERT INTO study_observations (
              observation_id, user_id, study_id, patient_hash,
              created_at, category, ae_grade, ae_grade_confirmed,
              is_dlt, source_kind, source_node_id,
              source_text_excerpt, linked_assessment_visit_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 'manual', NULL, ?, ?)
            """,
            (obs_id, user_id, study_id, req.patient_hash,
             now, req.category, req.ae_grade,
             None if req.is_dlt is None else (1 if req.is_dlt else 0),
             req.source_text_excerpt, req.linked_assessment_visit_id),
        )
        conn.commit()
    _emit(user_id, EventKind.STUDY_OBSERVATION_RECORDED, {
        "observation_id":             obs_id,
        "study_id":                   study_id,
        "category":                   req.category,
        "ae_grade":                   req.ae_grade,
        "is_dlt":                     req.is_dlt,
        "source_kind":                "manual",
        "source_text_excerpt":        req.source_text_excerpt,
        "linked_assessment_visit_id": req.linked_assessment_visit_id,
    }, patient_hash=req.patient_hash)
    return {"observation_id": obs_id}


@router.get("/studies/{study_id}/safety/stop-rule-status")
async def get_stop_rule_status(
    study_id: str,
    user_id: str = Depends(get_current_user),
) -> dict:
    """Aggregate the DLT counter against the protocol's stop-rule cap.

    A "DLT" is any observation row where:
      - ``is_dlt = 1`` (medic flagged on confirm), AND
      - ``ae_grade IN ('G3','G4','G5')`` (NCI CTCAE ≥G3, the regulatory
        cutoff most phase I/II protocols use), AND
      - ``ae_grade_confirmed = 1`` (medic explicitly confirmed grade),
        AND
      - ``unlinked_at IS NULL`` (not a 误判 unlink).

    Thresholds come from ``study.stop_rules_json`` — the starter 8Gy /
    Hybrid RT protocols in ``research/starter_protocols.py`` set this
    to ``{"dlt_cap_run_in": 2, "run_in_n": 6}``. If the column is
    empty the response still works (cap/run_in_n = null, triggered =
    false) so the UI can render "未配置" rather than crashing.
    """
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT stop_rules_json FROM research_studies "
            "WHERE user_id = ? AND study_id = ?",
            (user_id, study_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "no such study")
        stop_rules = json.loads(row[0] or "{}") if row[0] else {}
        dlt_cap   = stop_rules.get("dlt_cap_run_in")
        run_in_n  = stop_rules.get("run_in_n")
        dlt_count = conn.execute(
            """
            SELECT COUNT(*) FROM study_observations
            WHERE user_id = ? AND study_id = ?
              AND is_dlt = 1
              AND ae_grade IN ('G3','G4','G5')
              AND ae_grade_confirmed = 1
              AND unlinked_at IS NULL
            """,
            (user_id, study_id),
        ).fetchone()[0]
    triggered = bool(dlt_cap and dlt_count >= dlt_cap)
    if dlt_cap is None:
        note = "未配置 stop-rule — 在协议导入或手动编辑协议时设置 dlt_cap_run_in"
    elif triggered:
        note = (f"已达 stop-rule 阈值 — {dlt_count}/{dlt_cap} 例 DLT,"
                f"按协议应暂停入组并提交安全审查")
    else:
        note = (f"距 stop-rule 还有 {dlt_cap - dlt_count} 例 DLT 余量"
                + (f"(run-in 队列 {run_in_n} 例)" if run_in_n else ""))
    return {
        "dlt_observed": dlt_count,
        "dlt_cap":      dlt_cap,
        "run_in_n":     run_in_n,
        "triggered":    triggered,
        "note":         note,
    }


@router.get("/studies/{study_id}/observations")
async def list_observations(
    study_id: str,
    user_id: str = Depends(get_current_user),
    patient_hash: Optional[str] = Query(None),
    category:     Optional[str] = Query(None),
) -> List[ObservationRow]:
    sql = (
        "SELECT observation_id, study_id, patient_hash, created_at, "
        "category, ae_grade, ae_grade_confirmed, is_dlt, source_kind, "
        "source_node_id, source_text_excerpt, linked_assessment_visit_id, "
        "medic_confirmed_at, unlinked_at, unlink_reason "
        "FROM study_observations "
        "WHERE user_id = ? AND study_id = ? AND unlinked_at IS NULL"
    )
    args: list[Any] = [user_id, study_id]
    if patient_hash:
        sql += " AND patient_hash = ?"; args.append(patient_hash)
    if category:
        sql += " AND category = ?"; args.append(category)
    sql += " ORDER BY created_at DESC"
    with get_db_connection() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [
        ObservationRow(
            observation_id=r[0], study_id=r[1], patient_hash=r[2],
            created_at=r[3], category=r[4], ae_grade=r[5],
            ae_grade_confirmed=bool(r[6]), is_dlt=(None if r[7] is None else bool(r[7])),
            source_kind=r[8], source_node_id=r[9], source_text_excerpt=r[10],
            linked_assessment_visit_id=r[11], medic_confirmed_at=r[12],
            unlinked_at=r[13], unlink_reason=r[14],
        )
        for r in rows
    ]


class ObservationConfirmRequest(BaseModel):
    ae_grade: Optional[str] = None
    is_dlt:   Optional[bool] = None
    notes:    Optional[str] = None


@router.post("/studies/{study_id}/observations/{obs_id}/confirm")
async def confirm_observation(
    study_id: str,
    obs_id: str,
    req: ObservationConfirmRequest,
    user_id: str = Depends(get_current_user),
) -> dict:
    now = _now_ms()
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT patient_hash FROM study_observations "
            "WHERE observation_id = ? AND user_id = ? AND study_id = ?",
            (obs_id, user_id, study_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "no such observation")
        patient_hash = row[0]
        conn.execute(
            """
            UPDATE study_observations SET
              ae_grade = COALESCE(?, ae_grade),
              ae_grade_confirmed = 1,
              is_dlt = COALESCE(?, is_dlt),
              medic_confirmed_at = ?
            WHERE observation_id = ?
            """,
            (req.ae_grade,
             None if req.is_dlt is None else (1 if req.is_dlt else 0),
             now, obs_id),
        )
        conn.commit()
    _emit(user_id, EventKind.STUDY_OBSERVATION_CONFIRMED, {
        "observation_id": obs_id,
        "ae_grade": req.ae_grade,
        "is_dlt": req.is_dlt,
        "notes": req.notes,
    }, patient_hash=patient_hash)
    return {"status": "ok"}


class ObservationUnlinkRequest(BaseModel):
    reason: str = ""


@router.post("/studies/{study_id}/observations/{obs_id}/unlink")
async def unlink_observation(
    study_id: str,
    obs_id: str,
    req: ObservationUnlinkRequest,
    user_id: str = Depends(get_current_user),
) -> dict:
    now = _now_ms()
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT patient_hash FROM study_observations "
            "WHERE observation_id = ? AND user_id = ? AND study_id = ?",
            (obs_id, user_id, study_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "no such observation")
        patient_hash = row[0]
        conn.execute(
            "UPDATE study_observations SET unlinked_at = ?, unlink_reason = ? "
            "WHERE observation_id = ?",
            (now, req.reason, obs_id),
        )
        conn.commit()
    _emit(user_id, EventKind.STUDY_OBSERVATION_UNLINKED, {
        "observation_id": obs_id, "reason": req.reason,
    }, patient_hash=patient_hash)
    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────
# Reports — Phase 4 (stubs that return file_id when implemented)
# ─────────────────────────────────────────────────────────────────────


@router.get("/studies/{study_id}/reports")
async def list_reports(
    study_id: str,
    user_id: str = Depends(get_current_user),
) -> List[dict]:
    """Reports are tracked as a thin index — actual files live in
    uploads/ and referenced by file_id."""
    with get_db_connection() as conn:
        # No dedicated table — we read STUDY_REPORT_GENERATED events
        # back via a tiny view. For Phase 1 we just return an empty
        # list; Phase 4 populates this.
        return []


@router.post("/studies/{study_id}/reports/interim")
async def generate_interim_report(
    study_id: str,
    user_id: str = Depends(get_current_user),
) -> dict:
    try:
        from nexus_server.research.reports import draft_interim_report
        file_id = draft_interim_report(user_id, study_id)
        _emit(user_id, EventKind.STUDY_REPORT_GENERATED, {
            "study_id": study_id,
            "report_kind": "interim",
            "file_id": file_id,
        })
        return {"status": "ok", "file_id": file_id}
    except ImportError:
        return {"status": "deferred",
                "message": "report generator not yet wired (Phase 4)"}


# ─────────────────────────────────────────────────────────────────────
# Protocol import — Phase 2
# ─────────────────────────────────────────────────────────────────────


class ProtocolImportRequest(BaseModel):
    upload_file_id: str  # uploads.file_id of the .docx

@router.post("/studies/{study_id}/protocol/import")
async def import_protocol(
    study_id: str,
    req: ProtocolImportRequest,
    user_id: str = Depends(get_current_user),
) -> dict:
    """Parse a .docx protocol → return a draft of inclusion / exclusion
    / schedule rules. Slow: 10-30s while the LLM extracts.

    F-docx-import-diag — wrap the LLM call in a broad exception catch
    so the frontend ALWAYS gets a structured 5xx with a readable body
    instead of "Load failed" (which happens when an unhandled exception
    crashes the worker mid-request, dropping the TCP connection). The
    medic should be able to tell from one glance whether the docx is
    malformed (parser error), the LLM is down (gateway error), or the
    server itself has a bug.
    """
    try:
        from nexus_server.research.protocol_parser import parse_protocol_docx
    except ImportError as exc:
        # Phase 2 not wired in this build — degrade gracefully.
        logger.warning(
            "import_protocol: parser module unavailable: %s", exc,
        )
        return {"status": "deferred",
                "message": "protocol parser not yet wired (Phase 2)"}

    try:
        draft = await parse_protocol_docx(user_id, req.upload_file_id)
    except FileNotFoundError as exc:
        # The upload row exists but the file on disk doesn't. Common
        # after a half-completed restore or a manual file-system poke.
        raise HTTPException(
            status_code=404,
            detail=f"docx 文件不存在 (file_id={req.upload_file_id[:12]}…): {exc}",
        ) from exc
    except ValueError as exc:
        # python-docx raises ValueError / KeyError when the file isn't
        # a valid Office Open XML doc — old .doc, corrupted zip, password-
        # protected, etc.
        raise HTTPException(
            status_code=400,
            detail=f"docx 解析失败: {exc} — 请确认是 .docx (Word ≥2007),"
                   f" 不是 .doc 老格式 / 密码保护 / 损坏文件",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        # Catch-all so the request always closes with a readable body
        # instead of crashing the worker (which manifests as a fetch
        # "Load failed" client-side with no diagnostic). Re-raise as
        # 500 with the exception class + message so we can tell from
        # the medic's screenshot whether it was an LLM gateway issue
        # (``LlmGatewayError``), a network hiccup
        # (``httpx.ConnectError``), or a real parser bug.
        logger.exception(
            "import_protocol: parser raised user=%s file=%s",
            user_id, req.upload_file_id[:12],
        )
        raise HTTPException(
            status_code=500,
            detail=f"协议解析出错 ({type(exc).__name__}): {exc}",
        ) from exc

    # Stash the doc id on the study row but do NOT auto-write
    # inclusion/exclusion — wait for the medic to confirm in the
    # batch confirm UI (D7).
    try:
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE research_studies SET protocol_doc_id = ?, updated_at = ? "
                "WHERE user_id = ? AND study_id = ?",
                (req.upload_file_id, _now_ms(), user_id, study_id),
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.exception(
            "import_protocol: DB update failed user=%s study=%s",
            user_id, study_id,
        )
        raise HTTPException(
            status_code=500,
            detail=f"DB 写入失败 ({type(exc).__name__}): {exc}",
        ) from exc
    return {"status": "ok", "draft": draft}


@router.get("/studies/{study_id}/protocol/extracted")
async def get_extracted_protocol(
    study_id: str,
    user_id: str = Depends(get_current_user),
) -> dict:
    """Return the current draft of extracted rules (post-import). The
    batch confirm UI calls PATCH /studies/{id} to commit any edits."""
    sdict = _load_study_row(user_id, study_id)
    if not sdict:
        raise HTTPException(404, "study not found")
    return {
        "inclusion": sdict["inclusion"],
        "exclusion": sdict["exclusion"],
        "schedule":  sdict["schedule"],
        "protocol_doc_id":  sdict["protocol_doc_id"],
        "protocol_summary": sdict["protocol_summary"],
    }


# ─────────────────────────────────────────────────────────────────────
# Starter protocols (3 reference studies, medic-confirmed in §10 #1)
# ─────────────────────────────────────────────────────────────────────


@router.get("/starters")
async def list_starters(
    user_id: str = Depends(get_current_user),
) -> List[dict]:
    """List the built-in starter protocols available to install."""
    from nexus_server.research.starter_protocols import STARTER_PROTOCOLS
    return [
        {
            "starter_id":   sid,
            "display_name": p["display_name"],
            "short_code":   p["short_code"],
            "phase":        p["phase"],
            "target_n":     p["target_n"],
            "summary":      p["protocol_summary"][:280],
        }
        for sid, p in STARTER_PROTOCOLS.items()
    ]


class StarterInstallRequest(BaseModel):
    starter_ids: Optional[List[str]] = None  # None → install all
    overwrite:   bool = False


@router.post("/starters/install")
async def install_starters(
    req: StarterInstallRequest,
    user_id: str = Depends(get_current_user),
) -> dict:
    from nexus_server.research.starter_protocols import (
        STARTER_PROTOCOLS, install_starter, install_all_starters,
    )
    installed: List[str] = []
    if req.starter_ids is None:
        installed = install_all_starters(user_id)
    else:
        for sid in req.starter_ids:
            if sid not in STARTER_PROTOCOLS:
                continue
            try:
                installed.append(install_starter(
                    user_id, sid, overwrite=req.overwrite,
                ))
            except RuntimeError:
                # Already present + overwrite=False — skip silently
                pass
    return {"installed": installed, "count": len(installed)}


# ─────────────────────────────────────────────────────────────────────
# Patient → Studies (derived view, D18)
# ─────────────────────────────────────────────────────────────────────


@patients_studies_router.get("/{patient_hash}/studies")
async def get_patient_studies(
    patient_hash: str,
    user_id: str = Depends(get_current_user),
) -> List[StudyMembership]:
    """Return all studies a patient is in (active + historical).

    JOIN on study_enrollments and most-recent screening — never a
    denormalized column. See design §3.4 D18.
    """
    out: List[StudyMembership] = []
    with get_db_connection() as conn:
        # Active / withdrawn / completed enrollments
        rows = conn.execute(
            """
            SELECT e.study_id, e.status, e.enrollment_seq, e.arm,
                   e.enrolled_at, e.withdrawn_at, e.withdrawal_reason,
                   e.consent_signed_at, r.short_code, r.display_name
            FROM study_enrollments e
            JOIN research_studies r
                ON r.user_id = e.user_id AND r.study_id = e.study_id
            WHERE e.user_id = ? AND e.patient_hash = ?
            ORDER BY e.enrolled_at DESC
            """,
            (user_id, patient_hash),
        ).fetchall()
        for r in rows:
            out.append(StudyMembership(
                study_id=r[0],
                study_short_code=r[8],
                study_display_name=r[9],
                status=r[1],
                enrollment_seq=r[2],
                arm=r[3],
                enrolled_at=r[4],
                withdrawn_at=r[5],
                withdrawal_reason=r[6],
                consent_signed_at=r[7],
            ))
        # Pending screenings (likely_eligible/partial, not yet decided)
        sc_rows = conn.execute(
            """
            WITH latest AS (
                SELECT study_id, MAX(evaluated_at) AS m
                FROM screening_evaluations
                WHERE user_id = ? AND patient_hash = ?
                GROUP BY study_id
            )
            SELECT s.study_id, s.decision, s.overall_status,
                   r.short_code, r.display_name
            FROM screening_evaluations s
            JOIN latest l ON l.study_id = s.study_id AND l.m = s.evaluated_at
            JOIN research_studies r
                ON r.user_id = s.user_id AND r.study_id = s.study_id
            WHERE s.user_id = ? AND s.patient_hash = ?
            """,
            (user_id, patient_hash, user_id, patient_hash),
        ).fetchall()
        # Avoid duplicating studies already in the enrollment list.
        enrolled_ids = {m.study_id for m in out}
        for r in sc_rows:
            if r[0] in enrolled_ids:
                continue
            decision = r[1] or "pending"
            status = "screening" if decision == "pending" else (
                "invited" if decision == "invited" else
                "screen_failed" if decision == "excluded" else
                "screening"
            )
            out.append(StudyMembership(
                study_id=r[0],
                study_short_code=r[3],
                study_display_name=r[4],
                status=status,
                enrollment_seq=None, arm=None,
                enrolled_at=None, withdrawn_at=None,
                withdrawal_reason=None, consent_signed_at=None,
            ))
    return out
