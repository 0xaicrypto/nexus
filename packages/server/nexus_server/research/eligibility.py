"""3-stage eligibility engine (design §5.1).

Stage 1 — auto-rule        : pure SQL DSL ``rule_dsl`` over patient_facts
Stage 2 — auto-llm         : per-criterion LLM judge with evidence_refs
Stage 3 — overall recommend: LLM looks at all per-criterion results and
                              writes a narrative + confidence + next-steps

The engine writes a ``screening_evaluations`` row and emits
``SCREENING_EVALUATED``. It NEVER auto-creates an enrollment — only the
medic's explicit POST /enrollments call can do that (D3).

Stage 2 + 3 calls are best-effort: if the LLM gateway is unavailable
the engine falls back to the auto-rule-only outcome (overall_status =
``manual_review`` if any auto-llm criteria are present).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Optional

from nexus_server.database import get_db_connection
from nexus_server.event_sourcing import EventKind, Store
from nexus_server.research import rule_dsl
from nexus_server.research.patient_facts import (
    PatientFacts,
    get_patient_facts,
    list_known_patient_hashes,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Outputs
# ─────────────────────────────────────────────────────────────────────


@dataclass
class PerCriterionResult:
    kind: str                     # 'auto-rule' | 'auto-llm' | 'manual'
    verdict: str                  # 'pass' | 'fail' | 'unknown'
    confidence: Optional[float] = None
    reasoning: Optional[str] = None
    evidence_refs: Optional[list[str]] = None


@dataclass
class OverallResult:
    status: str                   # 'likely_eligible' | 'partial' | 'ineligible' | 'manual_review'
    per_criterion: dict[str, PerCriterionResult]
    llm_recommendation: Optional[dict] = None


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def evaluate_patient_against_study(
    conn: sqlite3.Connection,
    user_id: str,
    study_id: str,
    patient_hash: str,
    *,
    triggered_by_event_id: Optional[str] = None,
    facts: Optional[PatientFacts] = None,
    write: bool = True,
) -> OverallResult:
    """Run the full 3-stage evaluation. By default persists to
    screening_evaluations + emits an event."""
    sdict = _load_study(conn, user_id, study_id)
    if not sdict:
        raise RuntimeError(f"unknown study {study_id}")

    if facts is None:
        facts = get_patient_facts(conn, user_id, patient_hash)

    inclusion = sdict["inclusion"] or []
    exclusion = sdict["exclusion"] or []
    all_crits = list(inclusion) + list(exclusion)

    per_crit: dict[str, PerCriterionResult] = {}

    # ── Stage 1 — auto-rule + manual ───────────────────────────────
    for crit in all_crits:
        cid  = crit.get("id") or _slug(crit.get("text", ""))
        kind = crit.get("kind", "manual")
        if kind == "auto-rule":
            expr = crit.get("rule_dsl") or ""
            verdict = rule_dsl.evaluate(expr, facts) if expr else "unknown"
            per_crit[cid] = PerCriterionResult(kind="auto-rule", verdict=verdict)
        elif kind == "manual":
            per_crit[cid] = PerCriterionResult(kind="manual", verdict="unknown")
        # auto-llm — Stage 2

    # ── Stage 2 — auto-llm ─────────────────────────────────────────
    llm_crits = [c for c in all_crits if c.get("kind") == "auto-llm"]
    for crit in llm_crits:
        cid = crit.get("id") or _slug(crit.get("text", ""))
        try:
            r = _llm_judge_criterion(facts, crit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM judge failed for crit %s: %s", cid, exc)
            r = PerCriterionResult(
                kind="auto-llm", verdict="unknown",
                reasoning=f"llm unavailable: {exc}",
            )
        per_crit[cid] = r

    # ── Stage 3 — overall ──────────────────────────────────────────
    overall_status = _compute_overall_status(per_crit, inclusion, exclusion)
    llm_recommendation = None
    if llm_crits:
        try:
            llm_recommendation = _llm_overall_recommendation(
                facts, sdict, per_crit, overall_status,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM overall recommendation failed: %s", exc)

    out = OverallResult(
        status=overall_status,
        per_criterion=per_crit,
        llm_recommendation=llm_recommendation,
    )

    if write and overall_status in ("likely_eligible", "partial", "manual_review"):
        _persist_evaluation(
            conn, user_id, study_id, patient_hash, out,
            triggered_by_event_id=triggered_by_event_id,
        )

    return out


def rescan_all_for_study(user_id: str, study_id: str) -> int:
    """Re-evaluate every known patient against this study. Returns the
    number of patients evaluated."""
    n = 0
    with get_db_connection() as conn:
        patients = list_known_patient_hashes(conn, user_id)
    for ph in patients:
        try:
            with get_db_connection() as conn:
                # Skip already-enrolled patients.
                if _already_enrolled(conn, user_id, study_id, ph):
                    continue
                evaluate_patient_against_study(conn, user_id, study_id, ph)
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("rescan patient=%s failed: %s", ph[:8], exc)
    return n


def rescan_all_studies_for_patient(user_id: str, patient_hash: str) -> int:
    """Re-evaluate one patient against every active study. Called from
    event handlers when patient state changes."""
    n = 0
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT study_id FROM research_studies "
            "WHERE user_id = ? AND status IN ('enrolling','draft') "
            "  AND archived_at IS NULL",
            (user_id,),
        ).fetchall()
    for (sid,) in rows:
        try:
            with get_db_connection() as conn:
                if _already_enrolled(conn, user_id, sid, patient_hash):
                    continue
                evaluate_patient_against_study(conn, user_id, sid, patient_hash)
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("rescan study=%s failed: %s", sid[:8], exc)
    return n


# ─────────────────────────────────────────────────────────────────────
# Stage 2 / Stage 3 — LLM bridges
# ─────────────────────────────────────────────────────────────────────


def _llm_judge_criterion(
    facts: PatientFacts, crit: dict,
) -> PerCriterionResult:
    """Ask the LLM to decide one auto-llm criterion. Returns 'pass'
    /'fail'/'unknown' + reasoning + confidence + evidence_refs (which
    must be drawn from facts._evidence_pool — LLM cannot invent ids).
    """
    from nexus_server import llm_gateway  # local import to avoid hard dep

    prompt = _criterion_prompt(facts, crit)
    allowed_refs = set()
    for pool in facts._evidence_pool.values():
        allowed_refs.update(pool)

    raw = llm_gateway.call_llm(
        prompt=prompt,
        model="gemini-1.5-flash",
        response_mime_type="application/json",
        max_output_tokens=512,
    ) if hasattr(llm_gateway, "call_llm") else _fallback_llm_judge(prompt)

    # Parse strict JSON.
    try:
        out = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, json.JSONDecodeError):
        out = {}

    verdict = (out.get("verdict") or "unknown").lower()
    if verdict not in ("pass", "fail", "unknown"):
        verdict = "unknown"

    confidence = out.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))

    reasoning = (out.get("reasoning") or "")[:600]
    refs = out.get("evidence_refs") or []
    # Constraint: every ref must come from the allowed_refs pool.
    refs = [r for r in refs if isinstance(r, str) and r in allowed_refs]
    if not refs and verdict != "unknown":
        # No evidence → force unknown (design safety guardrail).
        verdict = "unknown"
        reasoning = (reasoning + " [evidence_refs empty → forced unknown]").strip()

    # Low confidence on pass → demote to partial-unknown.
    if verdict == "pass" and confidence is not None and confidence < 0.4:
        verdict = "unknown"

    return PerCriterionResult(
        kind="auto-llm",
        verdict=verdict,
        confidence=confidence,
        reasoning=reasoning,
        evidence_refs=refs,
    )


def _llm_overall_recommendation(
    facts: PatientFacts,
    study: dict,
    per_crit: dict[str, PerCriterionResult],
    overall: str,
) -> dict:
    from nexus_server import llm_gateway

    payload = {
        "study_short_code": study["short_code"],
        "study_display_name": study["display_name"],
        "primary_endpoint": study.get("primary_endpoint"),
        "patient_facts": facts.to_dict(),
        "per_criterion": {k: asdict(v) for k, v in per_crit.items()},
        "overall_status_so_far": overall,
    }
    prompt = (
        "You are a clinical research assistant. Given the per-criterion "
        "evaluation results below, produce a SHORT recommendation for the "
        "doctor: should they invite this patient? What's missing? Reply with "
        "strict JSON of shape "
        '{"overall_confidence": 0..1, "narrative": "...", '
        '"suggested_next_steps": ["..."]}. '
        "Do NOT recommend enrollment; only provide context. "
        f"Input:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    t0 = time.time()
    raw = llm_gateway.call_llm(
        prompt=prompt,
        model="gemini-1.5-flash",
        response_mime_type="application/json",
        max_output_tokens=512,
    ) if hasattr(llm_gateway, "call_llm") else _fallback_overall(payload)
    elapsed_ms = int((time.time() - t0) * 1000)

    try:
        out = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, json.JSONDecodeError):
        out = {}
    return {
        "overall_confidence": float(out.get("overall_confidence") or 0.0),
        "narrative":          (out.get("narrative") or "")[:1000],
        "suggested_next_steps": list(out.get("suggested_next_steps") or [])[:8],
        "model":              "gemini-1.5-flash",
        "latency_ms":         elapsed_ms,
    }


def _criterion_prompt(facts: PatientFacts, crit: dict) -> str:
    text = crit.get("text", "")
    llm_prompt = crit.get("llm_prompt") or ""
    evidence_sources = crit.get("evidence_sources") or []

    allowed_refs: list[str] = []
    for pool in facts._evidence_pool.values():
        allowed_refs.extend(pool)

    return (
        "You are a clinical research eligibility judge. Decide whether this "
        "specific criterion is met for the given patient.\n\n"
        f"CRITERION: {text}\n"
        + (f"GUIDANCE: {llm_prompt}\n" if llm_prompt else "")
        + (f"EVIDENCE SOURCES TO CONSIDER: {', '.join(evidence_sources)}\n"
           if evidence_sources else "")
        + f"\nPATIENT FACTS (structured):\n{json.dumps(facts.to_dict(), ensure_ascii=False)}\n"
        + "\nReply with strict JSON of shape "
        + '{"verdict":"pass"|"fail"|"unknown","confidence":0..1,'
        + '"reasoning":"...","evidence_refs":["graph_node_id_1",...]}.\n'
        + "evidence_refs MUST be a subset of: "
        + json.dumps(allowed_refs[:64]) + ". "
        + "If you cannot ground the verdict in actual evidence, respond "
        + "verdict='unknown' with evidence_refs=[]."
    )


# Fallback implementations for environments without an LLM gateway
# (offline tests / pure unit runs).
def _fallback_llm_judge(_prompt: str) -> str:
    return json.dumps({"verdict": "unknown", "evidence_refs": []})


def _fallback_overall(_payload: dict) -> str:
    return json.dumps({"overall_confidence": 0.0, "narrative": ""})


# ─────────────────────────────────────────────────────────────────────
# Overall status logic
# ─────────────────────────────────────────────────────────────────────


def _compute_overall_status(
    per_crit: dict[str, PerCriterionResult],
    inclusion: list[dict], exclusion: list[dict],
) -> str:
    """Map per-criterion verdicts to overall_status. Conservative."""
    incl_ids = {c.get("id") or _slug(c.get("text", "")) for c in inclusion}
    excl_ids = {c.get("id") or _slug(c.get("text", "")) for c in exclusion}

    # Any exclusion criterion that PASSED → ineligible.
    for cid in excl_ids:
        r = per_crit.get(cid)
        if r and r.verdict == "pass":
            return "ineligible"

    # Any inclusion criterion that FAILED → ineligible.
    for cid in incl_ids:
        r = per_crit.get(cid)
        if r and r.verdict == "fail":
            return "ineligible"

    # All auto-* + manual either pass or unknown.
    incl_unknown = sum(
        1 for cid in incl_ids
        if (r := per_crit.get(cid)) and r.verdict == "unknown"
    )
    excl_unknown = sum(
        1 for cid in excl_ids
        if (r := per_crit.get(cid)) and r.verdict == "unknown"
    )
    total_unknown = incl_unknown + excl_unknown
    has_manual = any(r.kind == "manual" for r in per_crit.values())

    if total_unknown == 0:
        return "likely_eligible"
    if has_manual and total_unknown <= sum(1 for r in per_crit.values() if r.kind == "manual"):
        return "likely_eligible"
    return "partial"


# ─────────────────────────────────────────────────────────────────────
# Persistence + event
# ─────────────────────────────────────────────────────────────────────


def _persist_evaluation(
    conn: sqlite3.Connection,
    user_id: str, study_id: str, patient_hash: str,
    result: OverallResult,
    *,
    triggered_by_event_id: Optional[str] = None,
) -> None:
    now = int(time.time() * 1000)
    per_crit_json = json.dumps(
        {k: asdict(v) for k, v in result.per_criterion.items()},
        ensure_ascii=False,
    )
    llm_json = (
        json.dumps(result.llm_recommendation, ensure_ascii=False)
        if result.llm_recommendation else None
    )

    conn.execute(
        """
        INSERT INTO screening_evaluations
        (user_id, study_id, patient_hash, evaluated_at,
         triggered_by_event_id, per_criterion_json, overall_status,
         llm_recommendation_json, decision)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (user_id, study_id, patient_hash, now,
         triggered_by_event_id, per_crit_json, result.status, llm_json),
    )
    conn.commit()

    # Emit event (best-effort).
    try:
        store = Store(conn)
        store.emit_and_apply(
            kind=EventKind.SCREENING_EVALUATED,
            payload={
                "study_id": study_id,
                "per_criterion_json": per_crit_json,
                "overall_status": result.status,
                "llm_recommendation_json": llm_json,
                "triggered_by": triggered_by_event_id,
            },
            apply_fn=lambda c, e: None,
            user_id=user_id,
            patient_hash=patient_hash,
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("SCREENING_EVALUATED emit failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _load_study(conn: sqlite3.Connection, user_id: str, study_id: str) -> Optional[dict]:
    r = conn.execute(
        "SELECT display_name, short_code, primary_endpoint, "
        "inclusion_json, exclusion_json "
        "FROM research_studies WHERE user_id = ? AND study_id = ? "
        "AND archived_at IS NULL",
        (user_id, study_id),
    ).fetchone()
    if not r:
        return None
    return {
        "display_name": r[0],
        "short_code": r[1],
        "primary_endpoint": r[2],
        "inclusion": json.loads(r[3] or "[]"),
        "exclusion": json.loads(r[4] or "[]"),
    }


def _already_enrolled(
    conn: sqlite3.Connection, user_id: str, study_id: str, patient_hash: str,
) -> bool:
    row = conn.execute(
        "SELECT status FROM study_enrollments "
        "WHERE user_id = ? AND study_id = ? AND patient_hash = ?",
        (user_id, study_id, patient_hash),
    ).fetchone()
    return bool(row and row[0] == "enrolled")


def _slug(s: str) -> str:
    out = []
    for ch in (s or "").strip().lower():
        if ch.isalnum():
            out.append(ch)
    return "".join(out)[:32] or "c"
