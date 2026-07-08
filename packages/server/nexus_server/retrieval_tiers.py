"""Three-tier retrieval orchestrator (ADR-002 Rev-4 / design v3 §6).

* **T1** Pre-cached views   — SQL hit, ≤ 50ms
* **T2** Single-entity lookup — graph read + 1 LLM call for final answer, ≤ 300ms
* **T3** Algorithm 1 multi-turn — streamed iterative reasoning, 5–15s

Tier classifier is rule-based in M0/M1. M4 can graduate to an LLM
classifier if the rule version's accuracy degrades on a labelled query set.

Output of a retrieval call is a typed iterator yielding ``RetrievalChunk``
events — same shape on every tier; T1/T2 emit one or two events total,
T3 streams reasoning + retrieved-context + final-answer chunks.

This module is consumed by the chat SSE endpoint (``/api/v1/agent/chat``).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Iterator, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Tier classification
# ─────────────────────────────────────────────────────────────────────

class Tier(str, Enum):
    T1 = "T1"   # cached view
    T2 = "T2"   # single-entity lookup
    T3 = "T3"   # iterative multi-turn
    T4 = "T4"   # web-grounded (Tavily + LLM synthesis)


@dataclass(frozen=True)
class TierChoice:
    tier: Tier
    reason: str
    view_kind: Optional[str] = None    # for T1
    anchor_hint: Optional[str] = None  # for T2


# Canned-view patterns. If a query matches AND the corresponding view
# is fresh in cached_views, we hit T1.
CANNED_VIEW_PATTERNS: dict[str, list[re.Pattern]] = {
    "patient_summary":     [re.compile(r"\b(summary|recap|overview)\b", re.I)],
    "active_findings":     [re.compile(r"\b(active|current)\s+findings?\b", re.I),
                            re.compile(r"\bwhat\s+(?:are|is)\s+the\s+findings?\b", re.I)],
    "current_medications": [re.compile(r"\b(current|active)?\s*(?:meds|medications?)\b", re.I)],
    "imaging_chronology":  [re.compile(r"\b(imaging\s+history|prior\s+stud(?:y|ies))\b", re.I)],
    "lab_trends_30d":      [re.compile(r"\b(labs?|trend|trending)\b", re.I)],
}

# Signals that require multi-hop reasoning → T3
MULTI_HOP_KEYWORDS = re.compile(
    r"\b(why|explain|rationale|trajectory|synthes(?:i[sz]e)|"
    r"across|over\s+time|compare.+(?:and|with)|chronology)\b",
    re.IGNORECASE,
)


def classify(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    patient_hash: Optional[str],
    question: str,
) -> TierChoice:
    """Pick the cheapest tier that can answer ``question`` correctly.

    Default cascade: try T1 (if pattern matches + view is fresh)
                  → T3 (if multi-hop signals)
                  → T2 (single-entity fallback).
    """
    q = question.strip()

    # ── T4 — web-grounded
    # Highest priority because it dominates the others when the
    # medic explicitly asks an external-knowledge question. The
    # patient-intent override inside looks_like_web_question keeps
    # us off T4 for clearly patient-anchored asks even when
    # guideline tokens are present.
    try:
        from nexus_server import web_search
        if web_search.looks_like_web_question(q) and web_search.is_configured():
            return TierChoice(Tier.T4, "matched web-intent pattern")
    except Exception as e:  # noqa: BLE001
        # web_search import / probe should never break tier classification
        # — if the module is broken we silently degrade to T1-T3.
        logger.debug("web-intent probe failed: %s", e)

    # ── T1 — canned view pattern match
    for view_kind, patterns in CANNED_VIEW_PATTERNS.items():
        if not any(p.search(q) for p in patterns):
            continue
        if patient_hash is None:
            continue
        if _view_is_fresh(conn, user_id, patient_hash, view_kind):
            return TierChoice(Tier.T1, f"matched view {view_kind!r}",
                              view_kind=view_kind)

    # ── T3 — multi-hop signals
    if MULTI_HOP_KEYWORDS.search(q):
        return TierChoice(Tier.T3, "multi-hop keywords")

    # Heuristic: very long questions probably need multi-hop reasoning
    if len(q.split()) > 25:
        return TierChoice(Tier.T3, "long question")

    # Count entity references → multiple → T3
    if _count_entity_references(conn, user_id, patient_hash, q) >= 3:
        return TierChoice(Tier.T3, "multiple entity references")

    # ── T2 — single-entity default
    anchor = _resolve_single_anchor(conn, user_id, patient_hash, q)
    if anchor:
        return TierChoice(Tier.T2, "single-entity anchor",
                          anchor_hint=anchor)

    # No specific anchor — fall through to T3 for breadth
    return TierChoice(Tier.T3, "no single anchor; broaden search")


def _view_is_fresh(
    conn: sqlite3.Connection, user_id: str, patient_hash: str, view_kind: str,
) -> bool:
    row = conn.execute(
        "SELECT generated_at, ttl_seconds, stale FROM cached_views "
        "WHERE user_id = ? AND patient_hash = ? AND view_kind = ?",
        (user_id, patient_hash, view_kind),
    ).fetchone()
    if row is None:
        return False
    generated_at, ttl, stale = row
    if stale:
        return False
    return (int(time.time()) - generated_at) < ttl


def _count_entity_references(
    conn: sqlite3.Connection,
    user_id: str,
    patient_hash: Optional[str],
    question: str,
) -> int:
    """Cheap NER — count tokens in question that look like graph entities."""
    if patient_hash is None:
        return 0
    rows = conn.execute(
        "SELECT content_json FROM clinical_graph_nodes "
        "WHERE user_id = ? AND patient_hash = ? "
        "  AND node_type IN ('finding','med','lab','anatomical_region','ddx') "
        "LIMIT 200",
        (user_id, patient_hash),
    ).fetchall()
    q_lower = question.lower()
    hits = 0
    for (raw,) in rows:
        try:
            label = (json.loads(raw) or {}).get("label", "")
        except json.JSONDecodeError:
            continue
        if label and label.lower() in q_lower:
            hits += 1
    return hits


def _resolve_single_anchor(
    conn: sqlite3.Connection,
    user_id: str,
    patient_hash: Optional[str],
    question: str,
) -> Optional[str]:
    """Return the label of the most likely single anchor entity, or None."""
    if patient_hash is None:
        return None
    rows = conn.execute(
        "SELECT content_json FROM clinical_graph_nodes "
        "WHERE user_id = ? AND patient_hash = ? "
        "  AND node_type IN ('finding','anatomical_region','med','lab') "
        "ORDER BY weight DESC LIMIT 100",
        (user_id, patient_hash),
    ).fetchall()
    q_lower = question.lower()
    for (raw,) in rows:
        try:
            label = (json.loads(raw) or {}).get("label", "")
        except json.JSONDecodeError:
            continue
        if label and label.lower() in q_lower:
            return label
    return None


# ─────────────────────────────────────────────────────────────────────
# Retrieval chunk events (SSE stream payloads)
# ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RetrievalChunk:
    kind: str
    data: dict


def yield_t1(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    patient_hash: str,
    view_kind: str,
) -> Iterator[RetrievalChunk]:
    """T1 — return cached view + citations as one shot."""
    from nexus_server.cached_views import get_view
    result = get_view(
        conn, user_id=user_id, patient_hash=patient_hash,
        view_kind=view_kind, rebuild_if_stale=True,
    )
    if result is None:
        yield RetrievalChunk("final_answer_chunk", {"text": "No data."})
        yield RetrievalChunk("turn_complete", {})
        return
    content, sources, _ts = result
    yield RetrievalChunk("tier_classified", {"tier": "T1", "view_kind": view_kind})
    yield RetrievalChunk("final_answer_chunk", {"text": content})
    yield RetrievalChunk(
        "citations",
        {"refs": [{"node_id": n, "kind": "cached_view_source"} for n in sources]},
    )
    yield RetrievalChunk("turn_complete", {})


def yield_t2(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    patient_hash: str,
    question: str,
    anchor: str,
) -> Iterator[RetrievalChunk]:
    """T2 — entity-anchored single-shot.

    Builds a textual answer from connected nodes of the anchored entity.
    For M1.6+ this will route through llm_gateway for natural-language
    synthesis; here we render a structured templated answer that callers
    can swap to LLM-backed synthesis later.
    """
    yield RetrievalChunk("tier_classified", {"tier": "T2", "anchor": anchor})

    # Find the anchor node by label match
    anchor_row = conn.execute(
        "SELECT node_id, node_type FROM clinical_graph_nodes "
        "WHERE user_id = ? AND patient_hash = ? "
        "  AND content_json LIKE ? LIMIT 1",
        (user_id, patient_hash, f"%{anchor}%"),
    ).fetchone()
    if anchor_row is None:
        yield RetrievalChunk(
            "final_answer_chunk",
            {"text": f"No information found about {anchor}."},
        )
        yield RetrievalChunk("turn_complete", {})
        return
    anchor_id, anchor_type = anchor_row

    # Pull connected episodic + semantic + measurement nodes
    rows = conn.execute(
        "SELECT n.node_id, n.node_type, n.content_json FROM clinical_graph_nodes n "
        "JOIN clinical_graph_edges e ON "
        "  ((e.src_node = ? AND e.dst_node = n.node_id) OR "
        "   (e.dst_node = ? AND e.src_node = n.node_id)) "
        "WHERE n.user_id = ? AND n.patient_hash = ? "
        "  AND n.node_type IN ('finding','measurement','episodic_event','semantic_fact') "
        "  AND e.user_id = n.user_id AND e.patient_hash = n.patient_hash "
        "ORDER BY n.weight DESC LIMIT 8",
        (anchor_id, anchor_id, user_id, patient_hash),
    ).fetchall()

    parts: list[str] = [f"## About {anchor}\n"]
    refs: list[dict] = [{"node_id": anchor_id, "kind": anchor_type}]
    for nid, ntype, raw in rows:
        try:
            content = json.loads(raw)
        except json.JSONDecodeError:
            continue
        label = content.get("label", "")
        parts.append(f"- {ntype}: {label} [#{nid}]")
        refs.append({"node_id": nid, "kind": ntype})

    yield RetrievalChunk("final_answer_chunk", {"text": "\n".join(parts)})
    yield RetrievalChunk("citations", {"refs": refs})
    yield RetrievalChunk("turn_complete", {})


def yield_t3(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    patient_hash: Optional[str],
    question: str,
) -> Iterator[RetrievalChunk]:
    """T3 — multi-turn streamed reasoning.

    M1.6+ wires the real Algorithm 1 control loop (ported from M3) to
    LLM gateway. The version here streams a placeholder reasoning trail
    + a synthesised summary so the frontend's TierIndicator / ReasoningPane
    have something to render end-to-end.
    """
    yield RetrievalChunk("tier_classified", {"tier": "T3"})

    yield RetrievalChunk("reasoning_chunk",
                        {"text": f"Searching for entities mentioned in: {question[:80]}…"})
    if patient_hash:
        n = conn.execute(
            "SELECT COUNT(*) FROM clinical_graph_nodes "
            "WHERE user_id = ? AND patient_hash = ? "
            "  AND node_type IN ('finding','study','measurement')",
            (user_id, patient_hash),
        ).fetchone()[0]
        yield RetrievalChunk("search_results_summary",
                             {"count": int(n or 0), "preview": "graph entities scanned"})

    yield RetrievalChunk(
        "final_answer_chunk",
        {
            "text": (
                "I've reviewed the available record. (T3 multi-hop "
                "reasoning surface is in place; full Algorithm 1 control "
                "loop with LLM-driven iterative search ships in M1.6+.)"
            )
        },
    )
    yield RetrievalChunk("citations", {"refs": []})
    yield RetrievalChunk("turn_complete", {})


# ─────────────────────────────────────────────────────────────────────
# Top-level dispatcher
# ─────────────────────────────────────────────────────────────────────

def retrieve(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    patient_hash: Optional[str],
    question: str,
) -> Iterator[RetrievalChunk]:
    """Classify + dispatch to the appropriate tier yielder."""
    choice = classify(conn, user_id=user_id, patient_hash=patient_hash, question=question)
    logger.info(
        "retrieve: user=%s patient=%s tier=%s reason=%s",
        user_id, patient_hash, choice.tier.value, choice.reason,
    )
    if choice.tier == Tier.T1 and choice.view_kind and patient_hash:
        yield from yield_t1(conn, user_id=user_id,
                            patient_hash=patient_hash,
                            view_kind=choice.view_kind)
    elif choice.tier == Tier.T2 and choice.anchor_hint and patient_hash:
        yield from yield_t2(conn, user_id=user_id, patient_hash=patient_hash,
                            question=question, anchor=choice.anchor_hint)
    else:
        yield from yield_t3(conn, user_id=user_id,
                            patient_hash=patient_hash, question=question)


# ─────────────────────────────────────────────────────────────────────
# Async dispatcher — T3 now calls the real LLM via llm_gateway. T1/T2
# stay synchronous (template / SQL) and are bridged into the async
# iterator via the sync `yield_t1` / `yield_t2` paths.
# ─────────────────────────────────────────────────────────────────────


def _recent_history_messages(
    user_id: str,
    session_id: Optional[str],
    *,
    max_turns: int = 12,
) -> list[dict]:
    """Pull the last ~12 messages of this chat session as a list of
    ``{role, content}`` dicts ready to prepend to the LLM messages
    array.

    Why this exists: without conversation history, the LLM only sees
    the current turn's question. If the medic typed a full SOAP note
    in turn 1 (e.g. "65y/M IIIB NSCLC ECOG 1 …") and asks a follow-up
    in turn 2, the LLM has no idea about the SOAP — and the anti-
    fabrication guard correctly refuses to invent. Threading recent
    history through fixes that: the LLM sees what the medic already
    said, and the anti-fabrication rule's permitted source A
    ("CONVERSATION HISTORY") becomes real.

    Best-effort: missing session, lookup failures, etc. all degrade
    gracefully to "no history" rather than blowing up the turn.
    """
    if not session_id:
        return []
    try:
        from nexus_server import twin_event_log
        raw, _total = twin_event_log.list_messages(
            user_id, max_turns, before_idx=None, session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("history fetch failed for session=%s: %s", session_id, exc)
        return []
    out: list[dict] = []
    for m in raw:
        role = m.get("role") or ""
        # Map server roles ("assistant"/"user") to OpenAI/Anthropic-
        # compatible roles. Drop anything else (system / tool) — the
        # gateway adds its own system prompt and tool outputs aren't
        # part of the user-visible thread.
        if role not in ("user", "assistant"):
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        out.append({"role": role, "content": content})
    return out


def _gather_study_protocol(
    conn: sqlite3.Connection, user_id: str, study_id: str,
) -> str:
    """Build a compact representation of a study's protocol body for
    research-chat LLM grounding.

    Before this existed, the LLM in research scope only knew the
    study_id + cohort hashes; asking "what is this study about?" got
    "I don't have details about study X". The actual protocol
    (display name, phase, endpoints, inclusion / exclusion criteria,
    visit schedule, summary) was sitting in the research_studies row
    the whole time — we just weren't telling the LLM.

    Best-effort: missing row / JSON parse failures fall through to an
    empty string so the cohort prompt still works.
    """
    import json as _json
    try:
        # F-roster-archive-filter — same defensive filter as the
        # roster query above. A medic who archived a study but
        # somehow still holds a handle to its study_id (stale URL,
        # localStorage activeStudyId pointing at a now-archived row,
        # etc.) should NOT have its protocol body fed back into the
        # LLM. The active-study guard lives on the read path so any
        # caller is covered.
        row = conn.execute(
            "SELECT display_name, short_code, phase, status, "
            "       target_n, primary_endpoint, secondary_endpoints_json, "
            "       inclusion_json, exclusion_json, schedule_json, "
            "       stop_rules_json "
            "FROM research_studies "
            "WHERE user_id = ? AND study_id = ? AND archived_at IS NULL",
            (user_id, study_id),
        ).fetchone()
    except sqlite3.Error:
        return ""
    if not row:
        return ""
    (display_name, short_code, phase, status, target_n,
     primary_endpoint, secondary_endpoints_json,
     inclusion_json, exclusion_json, schedule_json, stop_rules_json) = row

    def _parse_list(blob):
        try:
            v = _json.loads(blob or "[]")
            return v if isinstance(v, list) else []
        except Exception:
            return []

    inclusion = _parse_list(inclusion_json)
    exclusion = _parse_list(exclusion_json)
    schedule  = _parse_list(schedule_json)
    sec_eps   = _parse_list(secondary_endpoints_json)

    def _crit_lines(items, label):
        if not items:
            return f"  {label}: (none recorded yet)\n"
        lines = [f"  {label} ({len(items)}):"]
        for c in items[:25]:
            if isinstance(c, dict):
                txt = (c.get("text") or "").strip()
                if txt:
                    lines.append(f"    - {txt}")
        if len(items) > 25:
            lines.append(f"    … (+{len(items)-25} more)")
        return "\n".join(lines) + "\n"

    def _sched_lines(items):
        if not items:
            return "  schedule: (none recorded yet)\n"
        lines = [f"  schedule ({len(items)} visits):"]
        for v in items[:20]:
            if isinstance(v, dict):
                label = (v.get("label") or "").strip()
                offset = v.get("offset_days")
                assess = ", ".join((v.get("assessments") or [])[:6])
                lines.append(
                    f"    - {label} (D{offset}{'' if not assess else ': ' + assess})"
                )
        if len(items) > 20:
            lines.append(f"    … (+{len(items)-20} more)")
        return "\n".join(lines) + "\n"

    out = ["\n\nRESEARCH PROTOCOL (this study's actual rules):"]
    out.append(f"  display_name:     {display_name or '(unnamed)'}")
    out.append(f"  short_code:       {short_code or ''}")
    out.append(f"  phase:            {phase or 'n/a'}")
    out.append(f"  status:           {status or 'draft'}")
    if target_n:
        out.append(f"  target_n:         {target_n}")
    if primary_endpoint:
        out.append(f"  primary_endpoint: {primary_endpoint}")
    if sec_eps:
        out.append(f"  secondary_endpoints: {', '.join(str(e) for e in sec_eps[:8])}")
    out.append("")
    out.append(_crit_lines(inclusion, "inclusion criteria"))
    out.append(_crit_lines(exclusion, "exclusion criteria"))
    out.append(_sched_lines(schedule))
    return "\n".join(out)


def _gather_all_studies_summary(
    conn: sqlite3.Connection, user_id: str, max_per_section: int = 12,
) -> str:
    """Build a multi-study summary for cross-research chat.

    Status policy (matches the design's "accumulate, not reset" anti-
    pattern + the medic's "filter archived from default view" ask):

      * `enrolling` / `active` / `completed` → ACTIVE bucket: shown
        with full inclusion / exclusion text, and the LLM is told it
        MAY recommend matching a patient to these.
      * `draft` → DRAFT bucket: shown as a one-line awareness mention
        (no criteria), and the LLM is told NOT to recommend a patient
        to a draft trial (the medic hasn't finalised inclusion yet).
      * `archived` / `withdrawn` → hidden completely. A footer line
        tells the LLM how many archived studies exist so it can
        truthfully answer "how many trials have I retired?" without
        listing them by default.

    A medic who explicitly asks ("show me the archived ones too")
    triggers a re-query via the LLM's tool layer in a later phase;
    for MVP we keep them out of the default prompt entirely.
    """
    import json as _json
    try:
        # F-roster-archive-filter — research_router.archive_study sets
        # ``archived_at`` but DOESN'T change ``status`` (the study can
        # still be "enrolling" semantically — archive is a UI-hide
        # signal, not a lifecycle transition). The sidebar's GET
        # /studies query DOES filter ``archived_at IS NULL`` (see
        # research_router.py line 287) but this roster query missed
        # it, so the LLM kept listing "deleted" studies as active in
        # the cross-research chat. Anchor the same filter here.
        rows = conn.execute(
            "SELECT study_id, display_name, short_code, phase, status, "
            "       target_n, primary_endpoint, inclusion_json, exclusion_json, "
            "       created_at "
            "FROM research_studies "
            "WHERE user_id = ? AND archived_at IS NULL "
            "ORDER BY "
            "  CASE COALESCE(status,'draft') "
            "    WHEN 'enrolling' THEN 0 "
            "    WHEN 'active'    THEN 0 "
            "    WHEN 'completed' THEN 1 "
            "    WHEN 'draft'     THEN 2 "
            "    ELSE 3 "
            "  END, "
            "  created_at DESC",
            (user_id,),
        ).fetchall()
    except sqlite3.Error:
        return ""

    active_rows  = []
    draft_rows   = []
    archived_n   = 0
    for r in rows:
        status = (r[4] or "draft").lower()
        if status in ("archived", "withdrawn"):
            archived_n += 1
            continue
        if status == "draft":
            draft_rows.append(r)
        else:
            active_rows.append(r)

    if not active_rows and not draft_rows:
        # F-roster-empty-explicit — DON'T return empty here. When the
        # medic has archived every study, prior chat history (loaded
        # into the LLM context by retrieve_async) still contains the
        # AI's previous answers listing those studies as active. Without
        # an explicit "no studies" signal in the current prompt, the
        # LLM defaults to echoing its own previous turn ("您目前有 3 项
        # 活跃研究…") even after the medic clearly deleted them — the
        # exact bug the medic reported as "我已经删除了这些研究, 但还
        # 是说是 active 的".
        #
        # The current-state block must always win over historical
        # turns. Emitting an explicit zero-rows section + a strong
        # instruction below makes "you currently have 0 studies"
        # impossible for the LLM to ignore.
        return (
            "\n\nRESEARCH STUDIES IN THIS WORKSPACE (0 active, 0 draft"
            + (f", {archived_n} archived — hidden"
               if archived_n else "")
            + "):"
            "\n  (none — the medic has not created any studies yet, "
            "or every study has been archived)"
            "\n\n  ★ AUTHORITATIVE CURRENT STATE — if previous turns "
            "in this conversation referenced studies (e.g. HYBRID-RT-…, "
            "ES-SCLC-…) they have been ARCHIVED since. Do NOT list them "
            "as active. When the medic asks 'what studies do I have', "
            "answer 'none — your study list is empty'."
        )

    def _crit_inline(blob, label, cap):
        try:
            v = _json.loads(blob or "[]") or []
        except Exception:
            v = []
        if not v:
            return f"      {label}: (none recorded)"
        out = [f"      {label}:"]
        shown = 0
        for c in v:
            if not isinstance(c, dict):
                continue
            txt = (c.get("text") or "").strip()
            if not txt:
                continue
            out.append(f"        - {txt}")
            shown += 1
            if shown >= cap:
                break
        if len(v) > shown:
            out.append(f"        … (+{len(v)-shown} more)")
        return "\n".join(out)

    parts = [
        f"\n\nRESEARCH STUDIES IN THIS WORKSPACE "
        f"({len(active_rows)} active, {len(draft_rows)} draft"
        + (f", {archived_n} archived — hidden, not shown unless medic asks"
           if archived_n else "")
        + "):"
        # F-roster-empty-explicit — declare this block authoritative
        # so the LLM doesn't sneak in study names it remembers from
        # earlier turns. Without this, the model regularly listed
        # archived / deleted studies the medic had moved out.
        "\n  ★ This is the AUTHORITATIVE current roster. If a study "
        "name appeared in earlier turns but is NOT listed here, it has "
        "been archived — do NOT mention it as active."
    ]

    if active_rows:
        parts.append("\n  ── ACTIVE (recommend-eligible) ──")
        for (sid, name, code, phase, status, target_n, primary,
             incl_json, excl_json, _ct) in active_rows:
            parts.append(
                f"\n  Study {code or sid[:8]} — {name or '(unnamed)'}"
                f"\n    phase: {phase or 'n/a'} · status: {status or 'n/a'}"
                + (f" · target_n: {target_n}" if target_n else "")
                + (f"\n    primary_endpoint: {primary}" if primary else "")
            )
            parts.append(_crit_inline(incl_json, "inclusion", max_per_section))
            parts.append(_crit_inline(excl_json, "exclusion", max_per_section))

    if draft_rows:
        parts.append(
            "\n  ── DRAFT (mention for awareness only, do NOT recommend "
            "patients to a draft trial — inclusion criteria not finalised) ──"
        )
        for (sid, name, code, phase, _st, target_n, _pe,
             _ij, _ej, _ct) in draft_rows:
            parts.append(
                f"\n    Study {code or sid[:8]} — {name or '(unnamed)'} "
                f"(phase: {phase or 'n/a'}, draft)"
            )

    return "\n".join(parts)


def _gather_patient_roster(
    conn: sqlite3.Connection, user_id: str, limit: int = 30,
) -> str:
    """F-cross-patient-retrieval — render the medic's patient roster
    with top findings, for cross-patient / cross-research chats.

    Without this, asking "老李 的情况怎么样" in cross-* scope hits an
    empty PATIENT CONTEXT (the per-patient gatherer at line ~733
    short-circuits on ``patient_hash is None``). The LLM truthfully
    answers "no records" — but the data is right there in the DB.

    This block lists up to ``limit`` patients with:
      - patient label (initials/MRN/sequence number for display)
      - patient_hash prefix (for the LLM to refer back precisely)
      - top 5 findings / studies as ``[Nxx]`` citations

    The LLM can then:
      1. Recognize "老李" matches the labelled row
      2. Cite specific [Nxx] from that row when answering
      3. Compare across rows when asked cohort-level questions

    Empty string when the user has no patients yet.
    """
    # F-merge-patients-db — the ``patients`` table now lives in the
    # SHARED rune_server.db (the caller's ``conn``), so we can read
    # directly without opening a second connection. The earlier
    # F-roster-db-split workaround that hand-opened dicom_index.db is
    # now redundant and removed — the migration in
    # init_patients_table copied the rows over and dropped the legacy
    # table.
    try:
        patient_rows = conn.execute(
            "SELECT patient_hash, initials, mrn, age_group, sex, "
            "       created_at, updated_at "
            "FROM patients "
            "WHERE user_id = ? AND archived_at IS NULL "
            "ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    except sqlite3.Error as exc:
        # F-cross-patient-retrieval — verbose log so we can SEE this
        # fail in production. The previous silent-empty-return was
        # exactly the trap that masked the wrong-DB bug for an hour
        # of debugging.
        logger.warning(
            "patient roster query failed (user=%s): %s — "
            "cross-patient chat will see no roster",
            user_id, exc,
        )
        return ""
    if not patient_rows:
        return ""

    lines: list[str] = [
        "PATIENT ROSTER (this medic's ACTIVE patients — reference "
        "by label or by ``[Nxx]`` for a specific finding. Archived "
        "patients are intentionally NOT listed; do NOT speculate "
        "about anyone outside this roster):",
        "",
    ]
    # Sequence number = stable per-user position in the medic's
    # active roster (1-indexed). We compute it here at query time
    # rather than storing a column because archiving / un-archiving
    # would otherwise leave gaps the medic finds confusing.
    for idx, (phash, initials, mrn, age_group, sex,
              _created_at, _updated_at) in enumerate(patient_rows, start=1):
        seq_num = idx
        # Patient label — match the desktop's display logic:
        # initials + #seq, or mrn-prefix + #seq, or just #seq.
        if initials:
            label = f"{initials} · #{seq_num}"
        elif mrn:
            label = f"{mrn[:6]} · #{seq_num}"
        else:
            label = f"#{seq_num}"

        demo_bits: list[str] = []
        if sex:        demo_bits.append(str(sex))
        if age_group:  demo_bits.append(str(age_group))
        demo = " · ".join(demo_bits)

        # Top findings (cap at 5 per patient to keep prompt size bounded).
        try:
            findings = conn.execute(
                "SELECT node_id, content_json FROM clinical_graph_nodes "
                "WHERE user_id = ? AND patient_hash = ? "
                "  AND node_type IN ('finding','semantic_fact','study','med') "
                "ORDER BY weight DESC LIMIT 5",
                (user_id, phash),
            ).fetchall()
        except sqlite3.Error:
            findings = []
        finding_strs: list[str] = []
        for (node_id, raw) in findings:
            try:
                content = json.loads(raw)
            except json.JSONDecodeError:
                continue
            label_of = (content.get("label") or content.get("modality")
                        or content.get("name") or "?")
            finding_strs.append(f"[N{int(node_id)}] {label_of}")

        header = f"  · {label}"
        if demo:
            header += f" ({demo})"
        header += f" — hash={phash[:12]}"
        lines.append(header)
        if finding_strs:
            lines.append("      " + " · ".join(finding_strs))
        else:
            lines.append("      (no clinical entities ingested yet)")

    return "\n".join(lines)


def _gather_patient_context(
    conn: sqlite3.Connection, user_id: str, patient_hash: str,
) -> str:
    """Build a compact text block of the patient's graph for LLM grounding.

    Each item is prefixed with ``[Nxx]`` where ``xx`` is the node_id —
    so the LLM can cite back to specific findings via the same
    syntax the chat surface already understands as citation chips
    (``CitationChip2`` + ``contextRailContent.citation``). Without
    the inline IDs the LLM can only describe findings in prose; the
    right-rail provenance card stays empty and the medic has no
    one-click drill-down.

    Includes findings, medications, recent studies, semantic facts,
    measurements, and differential diagnoses — everything T3 retrieval
    grounds on.
    """
    parts: list[str] = []
    try:
        rows = conn.execute(
            "SELECT node_id, node_type, content_json "
            "FROM clinical_graph_nodes "
            "WHERE user_id = ? AND patient_hash = ? "
            "  AND node_type IN ('finding','med','ddx','study','semantic_fact','measurement') "
            "ORDER BY weight DESC LIMIT 40",
            (user_id, patient_hash),
        ).fetchall()
    except sqlite3.Error:
        return ""
    if not rows:
        return ""
    by_kind: dict[str, list[str]] = {}
    for node_id, ntype, raw in rows:
        try:
            content = json.loads(raw)
        except json.JSONDecodeError:
            continue
        label = content.get("label") or content.get("modality") or content.get("name") or "?"
        extra = ""
        if "size_cm" in content:    extra = f" ({content['size_cm']} cm)"
        elif "study_date" in content: extra = f" on {content['study_date']}"
        elif "value" in content:    extra = f" = {content['value']}"
        # ``[Nxx]`` prefix so the LLM can cite this node by id in its
        # answer (matches the citation-chip protocol the desktop's
        # chat pane consumes).
        by_kind.setdefault(ntype, []).append(
            f"[N{int(node_id)}] {label}{extra}"
        )
    label_map = {
        "finding": "Active findings",
        "med": "Medications",
        "ddx": "Differential diagnoses",
        "study": "Imaging studies",
        "semantic_fact": "Patient-level facts",
        "measurement": "Measurements",
    }
    for kind, items in by_kind.items():
        parts.append(f"{label_map.get(kind, kind)}: " + "; ".join(items[:10]))
    return "\n".join(parts)


async def yield_t3_llm(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    patient_hash: Optional[str],
    question: str,
    attachment_images: Optional[list[tuple[str, str, bytes]]] = None,
    research_scope: Optional[dict] = None,
    session_id: Optional[str] = None,
) -> AsyncIterator[RetrievalChunk]:
    """T3 — real LLM-grounded answer.

    When ``research_scope`` is set, the system prompt is reshaped:
      * Names the study + lists cohort patient_hashes
      * Adds CITATION PROTOCOL for external sources too ([PMID], [NCT])
      * Mentions the available external knowledge tools so the LLM can
        ask for them via tool-calling (or include their references)

    Without it the existing patient-scope behaviour is unchanged.
    """
    tier_meta = {"tier": "T3"}
    if research_scope and research_scope.get("kind") in ("research", "cross_patient"):
        tier_meta["scope"] = research_scope.get("kind", "")
        if research_scope.get("study_id"):
            tier_meta["study_id"] = research_scope["study_id"]
    yield RetrievalChunk("tier_classified", tier_meta)
    yield RetrievalChunk(
        "reasoning_chunk",
        {"text": f"Searching the patient record for: {question[:80]}…"},
    )

    context_block = ""
    cited_node_ids: list[int] = []
    if patient_hash:
        try:
            ctx_rows = conn.execute(
                "SELECT node_id, node_type FROM clinical_graph_nodes "
                "WHERE user_id = ? AND patient_hash = ? "
                "  AND node_type IN ('finding','med','study') "
                "ORDER BY weight DESC LIMIT 8",
                (user_id, patient_hash),
            ).fetchall()
            cited_node_ids = [int(r[0]) for r in ctx_rows]
        except sqlite3.Error:
            cited_node_ids = []
        context_block = _gather_patient_context(conn, user_id, patient_hash)
        if context_block:
            yield RetrievalChunk(
                "search_results_summary",
                {"count": len(cited_node_ids), "preview": "graph entities scanned"},
            )

    system_prompt = (
        "You are Nexus, a clinical workflow assistant for a practising "
        "physician. Answer the medic's question directly and concisely. "
        "Do NOT include hedging boilerplate; the medic is qualified."
        "\n\n"
        "LANGUAGE: respond in the SAME language as the medic's most "
        "recent message. If they wrote in Chinese, answer in Chinese; "
        "if English, English; if the message is mixed, use the "
        "dominant language. Medical terms / drug names / abbreviations "
        "(NSCLC, RECIST 1.1, CTCAE, mg, etc.) keep their canonical "
        "form regardless of body language."
        "\n\n"
        "PATIENT FACTS — three permitted sources only:\n"
        "  A. CONVERSATION HISTORY: anything the medic typed or pasted "
        "     in this chat (the SOAP note they're writing right now, "
        "     earlier turns in this session). This is fully legitimate "
        "     context — you may quote and reason about it freely "
        "     WITHOUT a [Nxx] tag. The medic typing \"65y/M, IIIB "
        "     NSCLC, ECOG 1, prior chemo ×2\" IS the patient record "
        "     for this turn; treat it as ground truth.\n"
        "  B. PATIENT CONTEXT block below (clinical graph, tagged "
        "     [Nxx]): facts from prior turns that have been extracted "
        "     into the structured graph. Any claim sourced from here "
        "     MUST carry its [Nxx] tag right after the supporting "
        "     phrase.\n"
        "  C. General medical knowledge (textbook / guideline): "
        "     allowed for non-patient-specific questions (\"what is "
        "     RECIST 1.1?\", \"G3 pneumonitis treatment options\"). "
        "     Preface with \"按通用医学知识\" / \"per general medical "
        "     knowledge\" so the medic can tell it apart from the "
        "     patient's own data.\n"
        "\n"
        "ANTI-FABRICATION (hard rules — violating is a patient-safety "
        "failure):\n"
        "  1. Patient-specific facts that are NOT in A, B, or C above "
        "     are forbidden. Don't invent pack-years, BP history, lab "
        "     values, lesion sizes, prior dates, allergies, smoking "
        "     status, family history, etc.\n"
        "  2. NEVER invent a [Nxx] tag. Use ONLY tags literally "
        "     present in the PATIENT CONTEXT block. The medic's own "
        "     typed text does NOT have [Nxx] tags — don't synthesise "
        "     ones for it. NEVER use placeholder tags like ``[N/A]``, "
        "     ``[N1]``, ``[?]``, ``[N待补]`` — any of these in your "
        "     reply destroys the citation chips and the medic's trust. "
        "     If you can't cite, just write the fact WITHOUT any tag.\n"
        "  3. If CONVERSATION HISTORY contains no patient facts AND "
        "     PATIENT CONTEXT is empty (<no record yet>) AND the "
        "     question is patient-specific (not general-knowledge), "
        "     ask the medic to share the clinical context first — DO "
        "     NOT generate a problem list out of thin air.\n"
        "  4. Always recommend professional review for any decision-"
        "     bearing output.\n"
        "  5. ★ YOU DO NOT HAVE WRITE CAPABILITY. You CANNOT add, "
        "     update, delete, or persist anything in PATIENT CONTEXT / "
        "     患者图谱 / 当前发现 / 用药 / 记忆. So NEVER say things "
        "     like \"好的，我已经更新了\", \"我已经记录了\", \"已添加 "
        "     到病人档案\", \"I've updated the record\", \"saved to "
        "     memory\", \"added to the graph\", etc. A background "
        "     extraction job (chat_ingester) will look at this turn "
        "     AFTER you reply and may pull facts into the graph — but "
        "     you cannot promise or speak for it. The honest framing "
        "     when the medic shares new clinical info is: \"我已了解 "
        "     这些信息，本轮结束后系统会尝试自动归入图谱 (是否成功你 "
        "     会在聊天底部的'已记忆'提示看到)。\" / \"I've noted those "
        "     facts; the background ingestion will attempt to record "
        "     them after this turn — you'll see a chip below confirming "
        "     success or failure.\""
        "\n\n"
        "CITATION PROTOCOL: every item in PATIENT CONTEXT below is "
        "prefixed with a tag like ``[N42]``. When you mention or rely "
        "on that item, append the same tag right after the supporting "
        "phrase, e.g. \"8 mm RUL nodule [N42] needs follow-up.\" These "
        "tags drive the desktop's citation chips and right-rail drill-"
        "down — a chip on a tag you invented opens a 404 panel and "
        "destroys medic trust."
    )
    if context_block:
        system_prompt += "\n\nPATIENT CONTEXT (from the local clinical graph):\n" + context_block
    # F-unified-chat-files — patient-scope file library. Files
    # uploaded inside this patient's chat are scoped to their hash
    # and surface here as [F1] [F2] for the LLM to cite.
    if patient_hash:
        from nexus_server.chat_files_router import _gather_file_lib
        _patient_file_block = _gather_file_lib(
            conn, user_id, 'patient', patient_hash,
        )
        if _patient_file_block:
            system_prompt += _patient_file_block
    else:
        # Without this explicit empty marker, the LLM tends to confuse
        # "no context provided" with "answer from training knowledge"
        # and fabricate patient findings (we saw this in the wild — a
        # patient with zero ingested data got back a 5-bullet problem
        # list with citations [N1]-[N5] that didn't exist). The marker
        # gives anti-fabrication rule 3 an unambiguous trigger.
        system_prompt += (
            "\n\nPATIENT CONTEXT (from the local clinical graph):\n"
            "<no record yet for this patient — no findings, no studies, "
            "no medications, no notes have been ingested>"
        )

    # ── PRACTITIONER PROFILE (Layer 2 read-back) ─────────────────────
    # Inject the medic's established preferences — what Nexus has
    # learned about their style / workflow / practice / calibration
    # across past encounters and now confirmed in the "Nexus has
    # learned" panel. Wired up so EVERY chat surface (patient-bound,
    # research per-study, cross-research) gets the same personalised
    # context. Per-user only; never crosses doctor boundaries.
    #
    # Best-effort: if the composer fails (DB locked, schema missing
    # mid-migration, etc.) we omit the block and continue — the
    # core PATIENT CONTEXT + research blocks are not affected.
    try:
        from nexus_server.practitioner.composer import build_prompt_enrichment
        practitioner_block = build_prompt_enrichment(conn, user_id=user_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("practitioner composer skipped: %s", exc)
        practitioner_block = ""
    if practitioner_block:
        system_prompt += (
            "\n\nPRACTITIONER PROFILE (per-user, medic-confirmed; "
            "treat as soft defaults, not hard rules):\n"
            + practitioner_block
        )

    # ── PRIOR INSIGHTS (Layer 2b — session_takeaway read-back) ───────
    # LLM-distilled qualitative observations of how this medic reasons.
    # Scope-aware: pull insights tagged for the current scope (patient
    # / study / cross-research) with a soft cross-scope blend so the
    # medic's general patterns show up even outside their primary
    # scope. Per-user only.
    try:
        from nexus_server.practitioner.session_takeaway import (
            fetch_prior_insights, render_prior_insights_block,
            scope_tuple_from_request,
        )
        # Reconstruct the scope tuple from the parameters we already
        # have. This mirrors chat_router.scope_tuple_from_request.
        _sk, _sr = "other", "__no_patient__"
        if patient_hash:
            _sk, _sr = "patient", patient_hash
        elif research_scope:
            rkind = research_scope.get("kind")
            sid = research_scope.get("study_id")
            if rkind == "research" and sid:
                _sk, _sr = "research", sid
            elif rkind in ("research", "cross_patient"):
                _sk, _sr = "cross_research", "__cross_research__"
        insights = fetch_prior_insights(
            conn, user_id=user_id, scope_kind=_sk, scope_ref=_sr, limit=5,
        )
        insights_block = render_prior_insights_block(insights)
    except Exception as exc:  # noqa: BLE001
        logger.debug("session_takeaway read skipped: %s", exc)
        insights_block = ""
    if insights_block:
        system_prompt += "\n\n" + insights_block

    # F-cross-patient-retrieval — inject the patient roster ANYTIME
    # the turn is not bound to a specific patient. This covers:
    #   · CrossPatientChat (Today bar): patient_hash=null, scope=None
    #   · CrossResearchChat (workspace): patient_hash=null,
    #                                    scope.kind="cross_patient"
    #   · per-study chat without a focused patient
    # Without this, asking "老李的情况怎么样" got "no records" because
    # the per-patient context block at line ~735 short-circuits when
    # patient_hash is None — the LLM had ZERO patient data even though
    # the DB knows who 老李 is.
    if not patient_hash:
        roster = _gather_patient_roster(conn, user_id, limit=30)
        if roster:
            system_prompt += "\n\n" + roster

    # Research scope augmentation — bias the persona toward cohort
    # reasoning, expose external knowledge tools, and inject a brief
    # cohort summary so the LLM doesn't try to answer one-patient-at-
    # a-time questions.
    if research_scope and research_scope.get("kind") in ("research", "cross_patient"):
        sid = research_scope.get("study_id") or "(unspecified)"
        phs = research_scope.get("patient_hashes") or []
        focus = research_scope.get("focus_patient_hash")
        # ── Load protocol context for the LLM.
        # Two cases:
        #   (a) A specific study is selected → inject ONE study's
        #       protocol body (name / phase / endpoints / inclusion /
        #       exclusion / schedule). This is the per-study Research
        #       Chat path.
        #   (b) No study_id (workspace-level Cross-Research Chat) →
        #       inject a COMPACT summary of ALL the user's studies so
        #       the LLM can match "patient looks like X → which trial
        #       does X fit?". Without this, the LLM in cross-research
        #       has no idea what studies exist and can't do triage.
        if sid != "(unspecified)":
            protocol_block = _gather_study_protocol(conn, user_id, sid)
        else:
            protocol_block = _gather_all_studies_summary(conn, user_id)
        cohort_block = (
            f"\n\nRESEARCH SCOPE:\n"
            f"  study_id: {sid}\n"
            f"  cohort_size: {len(phs)} patient(s)\n"
            + (f"  focus_patient: {focus} (writes/orders are scoped to this patient only)\n"
               if focus else
               "  focus_patient: <none — read-only across cohort>\n")
            + "  cohort_patient_hashes (truncated): "
            + ", ".join(p[:8] for p in phs[:20])
            + ("…" if len(phs) > 20 else "")
            + protocol_block
        )
        external_block = (
            "\n\nEXTERNAL KNOWLEDGE TOOLS (cite when used):\n"
            "  pubmed_search · europe_pmc_search · pmc_full_text · "
            "oa_pdf_lookup · semantic_scholar_search · preprint_search · "
            "ctcae_v5_lookup · drug_db_query · guideline_lookup\n"
            "\nCITATION PROTOCOL FOR EXTERNAL SOURCES:\n"
            "  - PubMed/PMC items → [PMID 12345678]\n"
            "  - ClinicalTrials.gov → [NCT01234567]\n"
            "  - Guidelines → [CSCO 2024 §6.3] / [NCCN NSCLC v2.2025]\n"
            "  - DOI fallback → [doi:10.xxxx/...]\n"
            "  Cite both internal [N42] and external [PMID …] as appropriate."
        )
        is_cross_research = (sid == "(unspecified)")
        persona_extension = (
            "\n\nYOU ARE NOW IN RESEARCH SCOPE. Your job:\n"
            "  - When the medic asks about the cohort, aggregate — never answer "
            "    as if it's a single patient.\n"
            "  - When the medic asks comparative questions ('our mPFS vs PACIFIC?'), "
            "    call pubmed_search / ct_gov_search to ground your answer in the "
            "    public literature, then compare to the cohort.\n"
            "  - When asked to draft a Table 1 / interim report section, suggest "
            "    the medic call POST /reports/interim from the UI; do NOT generate "
            "    fake numbers.\n"
            "  - LLM advice never executes writes. If asked to enroll/withdraw a "
            "    patient, instruct the medic to use the Eligibility Inbox or "
            "    Roster tab. (D3.)"
        )
        if is_cross_research:
            persona_extension += (
                "\n\nCROSS-RESEARCH MODE — you are NOT scoped to a single trial. "
                "The RESEARCH STUDIES IN THIS WORKSPACE block above lists this "
                "medic's trials, split into two buckets:\n"
                "  - ACTIVE (recommend-eligible): enrolling / completed studies "
                "    with full inclusion + exclusion criteria. These are the "
                "    only studies you may recommend a patient to.\n"
                "  - DRAFT: studies whose inclusion criteria the medic hasn't "
                "    finalised yet. NEVER recommend a patient to a draft trial "
                "    — say \"Study X is still a draft, finalise its inclusion "
                "    criteria first\" instead.\n"
                "  - ARCHIVED studies are HIDDEN. If the prompt's header line "
                "    mentions \"N archived\", you may say \"you have N archived "
                "    trials in this workspace; ask 'show archived' if you want "
                "    them listed\" — but NEVER guess what they were.\n"
                "\n"
                "Two recurring tasks:\n"
                "  (1) Patient → trial match. The medic pastes a patient summary "
                "      (\"65y/M, IIIB NSCLC, EGFR-, ECOG 1, treatment-naive\"). "
                "      Walk EVERY ACTIVE study's inclusion + exclusion list, "
                "      decide hits / misses / unclear, and rank the 1-3 best "
                "      ACTIVE candidate trials. For each: 'why it fits' + "
                "      'what's still unclear' + 'how to confirm (which test / "
                "      which fact to add)'. Treat the medic's text as ground "
                "      truth (conversation source A).\n"
                "  (2) Cross-trial questions ('which of my trials has the "
                "      strictest exclusion?', 'any trial overlap on inclusion?'). "
                "      Default to active trials only. If the medic explicitly "
                "      asks about drafts or archived ones, say which are "
                "      visible to you and which need separate retrieval.\n"
                "\n"
                "Recommending a match is NOT enrollment. Tell the medic to open "
                "the trial's Eligibility Inbox to formally invite the patient — "
                "you cannot enroll them yourself."
            )
        # F-unified-chat-files — inject the file-library block so the
        # LLM can cite [F1] [F2] for files the medic has attached to
        # this chat surface. The library is keyed by (scope_kind,
        # scope_ref):
        #
        #   * per-study research chat  → ('research', study_id)
        #   * cross-research chat      → ('cross_research', '__workspace__')
        #
        # The frontend uploads with the matching kind via api.uploadFile,
        # so the read here MUST mirror that mapping exactly or the
        # files will be invisible to the LLM. (Previously this used
        # 'research' for BOTH, which silently lost cross-research files.)
        from nexus_server.chat_files_router import _gather_file_lib
        if sid == '(unspecified)':
            _lib_scope_kind = 'cross_research'
            _lib_scope_ref  = '__workspace__'
        else:
            _lib_scope_kind = 'research'
            _lib_scope_ref  = sid
        file_block = _gather_file_lib(
            conn, user_id, _lib_scope_kind, _lib_scope_ref,
        )
        system_prompt = (
            system_prompt + cohort_block + file_block
            + external_block + persona_extension
        )

    answer_buf: list[str] = []   # accumulates full answer for citation extraction
    try:
        if attachment_images:
            # Vision path — Gemini multimodal STREAMING call. Each
            # image passes as a Part.from_bytes alongside the prompt
            # text. We pin the model to Flash 2.5 (cheap + supports
            # vision + supports the streaming API).
            #
            # Tokens stream out via ``final_answer_chunk`` events so
            # the desktop's chat pane renders them as they arrive —
            # matching the SSE feel of the text-only path. Previously
            # we returned the whole answer in one shot which made
            # vision turns feel laggy (~5-10s blank screen).
            async for delta in _t3_stream_with_images(
                question=question,
                system_prompt=system_prompt,
                images=attachment_images,
            ):
                if delta:
                    answer_buf.append(delta)
                    yield RetrievalChunk("final_answer_chunk", {"text": delta})
            logger.info(
                "yield_t3_llm: vision stream done, images=%d answer_chars=%d",
                len(attachment_images), sum(len(s) for s in answer_buf),
            )
        else:
            from nexus_server import llm_gateway
            # Prepend recent conversation history so the LLM sees what
            # the medic already typed (SOAP, follow-up Qs). Without
            # this, an answer based on context from turn N−1 is
            # impossible — the LLM only sees turn N's question.
            history = _recent_history_messages(user_id, session_id)
            content, model, _stop, _tools = await llm_gateway.call_llm(
                messages=history + [{"role": "user", "content": question}],
                system_prompt=system_prompt,
                model=None,            # use config.DEFAULT_LLM_MODEL
                temperature=0.4,
                # 1024 was way too tight for cross-research (listing 5+
                # studies with inclusion/exclusion was getting truncated
                # mid-list, even with the SDK's 2-step auto-continuation).
                # 4096 leaves room for a complete answer + lab values +
                # citation chips without burning into the truncation
                # marker fallback path.
                max_tokens=4096,
                tools=None,
            )
            logger.info("yield_t3_llm: model=%s answer_chars=%d history=%d",
                        model, len(content), len(history))
            text = content.strip()
            if not text:
                # Empty LLM body — distinguish from a successful empty
                # answer. Could mean: bad/expired API key (the most
                # common cause), provider rate-limit, safety filter
                # rejection, or the streaming gateway closed early.
                # Hand the medic something actionable rather than the
                # mysterious "(no response)".
                text = (
                    "⚠ LLM 返回了空响应。可能原因:\n"
                    "  • API key 已失效/过期 — 打开 Settings · LLM 重置\n"
                    "  • 模型限流或安全过滤 — 换个 provider/model 试试\n"
                    "  • 网络中断 — 看 ~/Library/Logs/Nexus/server.log"
                )
            answer_buf.append(text)
            yield RetrievalChunk("final_answer_chunk", {"text": text})
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM call failed in yield_t3_llm")
        err_msg = (
            f"⚠ LLM call failed: {exc}. Check Settings · LLM — make sure "
            f"the active provider has an API key, and the key is valid."
        )
        answer_buf.append(err_msg)
        yield RetrievalChunk("final_answer_chunk", {"text": err_msg})

    answer = "".join(answer_buf)

    # Refine the citations event to only include node IDs the LLM
    # actually mentioned in its answer. Falling back to the full
    # ``cited_node_ids`` set (every graph_node we sent in context)
    # would over-attribute — the right-rail drill-down should reflect
    # nodes the answer relies on, not nodes that happened to be in
    # the prompt.
    answered_node_ids: list[int] = []
    seen: set[int] = set()
    for m in re.finditer(r"\[N(\d+)\]", answer):
        try:
            nid = int(m.group(1))
        except ValueError:
            continue
        # Only emit citations for nodes the LLM had legitimate access
        # to — drops hallucinated IDs that don't exist in the patient
        # graph.
        if nid not in seen and nid in set(cited_node_ids):
            seen.add(nid)
            answered_node_ids.append(nid)
    # Backstop: if the LLM didn't cite any tag (e.g. very short
    # answer / non-clinical chitchat), keep the original behaviour
    # so the right-rail still has *something* to drill into.
    if not answered_node_ids:
        answered_node_ids = cited_node_ids[:6]

    yield RetrievalChunk(
        "citations",
        {"refs": [{"node_id": nid, "kind": "graph_node"} for nid in answered_node_ids]},
    )
    yield RetrievalChunk("turn_complete", {})


async def _t3_stream_with_images(
    *,
    question: str,
    system_prompt: str,
    images: list[tuple[str, str, bytes]],
) -> AsyncIterator[str]:
    """Direct google.genai multimodal STREAMING call — bypasses
    llm_gateway (text-only). Yields partial text deltas as Gemini
    produces them.

    Mirrors ``quick_scan._gemini_triage_grid``'s call shape but uses
    the ``generate_content_stream`` API instead of the buffered
    ``generate_content`` so the desktop's chat pane gets the same
    SSE-stream feel for vision turns as for plain text.

    The model is pinned to ``gemini-2.5-flash`` for cost (text+vision
    Flash is ~10× cheaper than Pro). Yields nothing on API error and
    raises so the caller's try/except can format a useful user-facing
    message.
    """
    # Live API-key read so Settings · LLM updates take effect on the
    # next chat turn without restarting the sidecar.
    from nexus_server.quick_scan import _live_gemini_api_key
    api_key = _live_gemini_api_key()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY not configured — set it in Settings · LLM "
            "before attaching images to chat."
        )

    # Build the multipart prompt. system_prompt is folded into the
    # user content because google-genai's generate_content() doesn't
    # accept system_instructions on every client version uniformly.
    full_prompt = f"{system_prompt}\n\n--- USER QUESTION ---\n{question}"

    try:
        from google import genai
        from google.genai import types as gtypes
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"google-genai SDK unavailable: {exc}")

    client = genai.Client(api_key=api_key)
    parts: list = []
    for name, mime, raw in images:
        # Gemini accepts image/png, image/jpeg, image/webp, image/heic,
        # image/heif. TIFF (common in pathology / mammography /
        # radiology PDF exports) is NOT in the supported list — we
        # transparently transcode to PNG via Pillow so the medic can
        # paste a TIFF and have it Just Work.
        norm_mime = mime
        norm_bytes = raw
        if mime.lower() in ("image/tiff", "image/tif") or \
                name.lower().endswith((".tif", ".tiff")):
            try:
                from PIL import Image as _PILImage
                import io as _io
                im = _PILImage.open(_io.BytesIO(raw))
                # Multi-page TIFF: keep only the first page — Gemini
                # would treat additional pages as separate images
                # without context glue.
                if hasattr(im, "n_frames") and im.n_frames > 1:
                    im.seek(0)
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                buf = _io.BytesIO()
                im.save(buf, format="PNG", optimize=True)
                norm_bytes = buf.getvalue()
                norm_mime = "image/png"
                logger.info(
                    "transcoded TIFF %s (%d bytes) → PNG (%d bytes)",
                    name, len(raw), len(norm_bytes),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "TIFF transcode for %s failed (%s) — forwarding "
                    "raw bytes; Gemini may reject", name, e,
                )
        parts.append(gtypes.Part.from_bytes(data=norm_bytes, mime_type=norm_mime))
    parts.append(full_prompt)

    # Stream via google-genai's iterator API. The synchronous call
    # returns an iterator that yields ``GenerateContentResponse``
    # chunks; we adapt to async by running the next-chunk fetch in a
    # thread (the underlying httpx call is blocking).
    import asyncio as _asyncio

    def _make_stream():
        return client.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=parts,
        )

    loop = _asyncio.get_event_loop()
    stream = await loop.run_in_executor(None, _make_stream)

    saw_any = False
    while True:
        # ``next(stream, sentinel)`` is the pattern that doesn't raise
        # on exhaustion. Run it in a thread so the event loop stays
        # responsive and Quick scan / other concurrent work isn't
        # blocked.
        SENTINEL = object()
        chunk = await loop.run_in_executor(
            None, lambda: next(stream, SENTINEL),
        )
        if chunk is SENTINEL:
            break
        delta = (getattr(chunk, "text", "") or "")
        if delta:
            saw_any = True
            yield delta

    if not saw_any:
        # Defensive fallback — older google-genai builds occasionally
        # ship a final response on .text but nothing on stream chunks.
        # We don't want a silent empty bubble in chat.
        yield "(model returned no text)"


async def yield_t4_web(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    patient_hash: Optional[str],
    question: str,
    session_id: Optional[str] = None,
) -> AsyncIterator[RetrievalChunk]:
    """T4 — web-grounded clinical answer.

    Pipeline:
      1. Emit tier_classified + web_search_started.
      2. Hit web_search.search_clinical (PHI-scrubbed, allow-list filtered).
      3. Emit web_search_results so the UI can render a "found N
         sources" card BEFORE the LLM synthesis arrives.
      4. Build the prompt: PATIENT CONTEXT (if patient_hash) + WEB
         CONTEXT (the top snippets). Both citation prefixes ([Nxx]
         and [Wxx]) are taught in the system prompt.
      5. Stream the LLM answer (gateway path — same Gemini Flash 2.5
         as T3, just with a larger prompt).
      6. Emit citations including BOTH node refs and web refs so the
         desktop's chip rail can render both kinds.
    """
    from nexus_server import web_search

    yield RetrievalChunk("tier_classified", {"tier": "T4"})
    yield RetrievalChunk(
        "reasoning_chunk",
        {"text": f"Searching the literature for: {question[:80]}…"},
    )
    yield RetrievalChunk(
        "web_search_started",
        {"query": question[:120], "provider": "tavily"},
    )

    search = await web_search.search_clinical(question, max_results=5)

    if search.error and not search.results:
        # No key OR provider failure. Degrade to T3 — emit a
        # reasoning chunk that tells the medic what happened, then
        # delegate to the patient-only LLM path.
        yield RetrievalChunk(
            "reasoning_chunk",
            {"text": f"⚠ Web search unavailable: {search.error}. "
                     "Answering from patient context only."},
        )
        async for chunk in yield_t3_llm(
            conn, user_id=user_id, patient_hash=patient_hash,
            question=question,
        ):
            yield chunk
        return

    # Emit results metadata for the UI search-card render.
    yield RetrievalChunk(
        "web_search_results",
        {
            "count": len(search.results),
            "results": [r.to_dict() for r in search.results],
        },
    )

    # Build WEB CONTEXT block. Tag each result with [W1], [W2]... so
    # the LLM can cite them inline. Bound the per-result snippet to
    # ~400 chars to keep prompt budget < 4 KB even with 5 sources.
    web_block_lines: list[str] = []
    for r in search.results:
        web_block_lines.append(
            f"[W{r.w_id}] {r.title} ({r.domain})\n"
            f"    {r.snippet[:400]}"
        )
    web_block = "\n\n".join(web_block_lines)

    # PATIENT CONTEXT (when applicable). Same code path as T3.
    patient_block = ""
    cited_node_ids: list[int] = []
    if patient_hash:
        try:
            ctx_rows = conn.execute(
                "SELECT node_id FROM clinical_graph_nodes "
                "WHERE user_id = ? AND patient_hash = ? "
                "  AND node_type IN ('finding','med','study') "
                "ORDER BY weight DESC LIMIT 8",
                (user_id, patient_hash),
            ).fetchall()
            cited_node_ids = [int(r[0]) for r in ctx_rows]
        except sqlite3.Error:
            cited_node_ids = []
        patient_block = _gather_patient_context(conn, user_id, patient_hash)

    system_prompt = (
        "You are Nexus, a clinical workflow assistant for a practising "
        "physician. The medic's question requires both the patient's "
        "own record AND external clinical knowledge (guidelines, "
        "literature). Synthesise both sources into a direct, concise "
        "answer.\n\n"
        "LANGUAGE: respond in the SAME language as the medic's most "
        "recent message. If they wrote in Chinese, answer in Chinese; "
        "if English, English; if the message is mixed, use the "
        "dominant language. Medical terms / drug names / abbreviations "
        "(NSCLC, RECIST 1.1, CTCAE, mg, etc.) keep their canonical "
        "form regardless of body language.\n\n"
        "ANTI-FABRICATION (hard rules):\n"
        "  1. NEVER invent patient-specific facts (pack-years, BP, lab "
        "     values, lesion sizes, prior meds, etc.). Patient-specific "
        "     statements must be backed by a [Nxx] tag from PATIENT "
        "     CONTEXT.\n"
        "  2. NEVER invent a [Nxx] or [Wxx] tag. Only literal tags "
        "     present in the corresponding context block may be used. "
        "     NEVER use placeholders like [N/A], [N1], [?], [N待补] — "
        "     if you can't cite, just write the fact tag-free.\n"
        "  3. If PATIENT CONTEXT is empty (<no record yet>), do NOT "
        "     produce a differential diagnosis or problem list. Tell "
        "     the medic the patient has no ingested record yet.\n"
        "  4. External-knowledge claims that come from WEB CONTEXT "
        "     must carry a [Wxx] tag. Claims from training knowledge "
        "     are allowed but MUST be prefaced with \"按通用医学知识\" / "
        "     \"per general medical knowledge\" so the medic can tell "
        "     them apart from cited claims.\n"
        "  5. ★ YOU DO NOT HAVE WRITE CAPABILITY to PATIENT CONTEXT, "
        "     the patient graph, 当前发现, 用药, or 记忆. Do NOT say "
        "     \"我已经更新了\" / \"已记录到病人档案\" / \"saved to "
        "     memory\" — that's hallucinated. The background "
        "     chat_ingester job will attempt to extract facts AFTER "
        "     this turn; the medic sees a chip below your reply with "
        "     the real outcome. The honest phrasing is "
        "     \"已了解 — 本轮结束后系统会尝试归档，是否成功你会在底部 "
        "     的'已记忆'提示看到\".\n\n"
        "CITATION PROTOCOL — every claim that comes from one of the "
        "context blocks below MUST carry an inline tag immediately "
        "after the supporting phrase:\n"
        "  • [Nxx] for items from PATIENT CONTEXT (the patient's own "
        "    graph nodes)\n"
        "  • [Wxx] for items from WEB CONTEXT (cited sources)\n"
        "Always recommend professional review for any decision-bearing "
        "output."
    )
    if patient_block:
        system_prompt += (
            "\n\nPATIENT CONTEXT (from the local clinical graph):\n"
            + patient_block
        )
    else:
        system_prompt += (
            "\n\nPATIENT CONTEXT (from the local clinical graph):\n"
            "<no record yet for this patient — no findings, no studies, "
            "no medications, no notes have been ingested>"
        )
    if web_block:
        system_prompt += (
            "\n\nWEB CONTEXT (cited clinical sources — every snippet has "
            "a [Wxx] tag for inline citation):\n"
            + web_block
        )

    # PRACTITIONER PROFILE (same wire as T3 — per-user, medic-confirmed).
    try:
        from nexus_server.practitioner.composer import build_prompt_enrichment
        practitioner_block = build_prompt_enrichment(conn, user_id=user_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("practitioner composer skipped (T4 path): %s", exc)
        practitioner_block = ""
    if practitioner_block:
        system_prompt += (
            "\n\nPRACTITIONER PROFILE (per-user, medic-confirmed; "
            "treat as soft defaults, not hard rules):\n"
            + practitioner_block
        )

    # PRIOR INSIGHTS (Layer 2b — same wire as T3 path). T4 path here
    # is patient-bound (we only enter T4 from a patient context), so
    # scope is straightforwardly "patient".
    try:
        from nexus_server.practitioner.session_takeaway import (
            fetch_prior_insights, render_prior_insights_block,
        )
        insights = fetch_prior_insights(
            conn, user_id=user_id,
            scope_kind="patient", scope_ref=patient_hash or "__no_patient__",
            limit=5,
        )
        insights_block = render_prior_insights_block(insights)
    except Exception as exc:  # noqa: BLE001
        logger.debug("session_takeaway read skipped (T4 path): %s", exc)
        insights_block = ""
    if insights_block:
        system_prompt += "\n\n" + insights_block

    # Stream the LLM answer. Same gateway as T3 — text-only since web
    # results have no image attachments.
    answer_buf: list[str] = []
    try:
        from nexus_server import llm_gateway
        history = _recent_history_messages(user_id, session_id)
        content, model, _stop, _tools = await llm_gateway.call_llm(
            messages=history + [{"role": "user", "content": question}],
            system_prompt=system_prompt,
            model=None,
            temperature=0.4,
            max_tokens=1500,    # T4 needs more room for synthesis
            tools=None,
        )
        text = content.strip()
        if not text:
            # See yield_t3_llm — same actionable error rather than
            # the opaque "(no response)".
            text = (
                "⚠ LLM 返回了空响应。可能原因:\n"
                "  • API key 已失效/过期 — 打开 Settings · LLM 重置\n"
                "  • 模型限流或安全过滤 — 换个 provider/model 试试\n"
                "  • 网络中断 — 看 ~/Library/Logs/Nexus/server.log"
            )
        answer_buf.append(text)
        yield RetrievalChunk("final_answer_chunk", {"text": text})
        logger.info(
            "yield_t4_web: model=%s answer_chars=%d web_results=%d "
            "patient_nodes=%d",
            model, len(text), len(search.results), len(cited_node_ids),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM call failed in yield_t4_web")
        err_msg = (
            f"⚠ LLM call failed during web synthesis: {exc}. "
            "Check Settings · LLM."
        )
        answer_buf.append(err_msg)
        yield RetrievalChunk("final_answer_chunk", {"text": err_msg})

    # Parse out [Nxx] + [Wxx] tags the LLM actually used; emit
    # citations carrying both kinds. Refs unused in the answer are
    # NOT cited — keeps the ContextRail focused on what grounded
    # the response.
    answer = "".join(answer_buf)
    cited_nodes_in_answer: list[int] = []
    seen_n: set[int] = set()
    for m in re.finditer(r"\[N(\d+)\]", answer):
        try:
            nid = int(m.group(1))
        except ValueError:
            continue
        if nid not in seen_n and nid in set(cited_node_ids):
            seen_n.add(nid)
            cited_nodes_in_answer.append(nid)

    cited_webs_in_answer: list[dict] = []
    seen_w: set[int] = set()
    web_by_id = {r.w_id: r for r in search.results}
    for m in re.finditer(r"\[W(\d+)\]", answer):
        try:
            wid = int(m.group(1))
        except ValueError:
            continue
        if wid in seen_w or wid not in web_by_id:
            continue
        seen_w.add(wid)
        r = web_by_id[wid]
        cited_webs_in_answer.append({
            "kind":    "web_source",
            "w_id":    wid,
            "url":     r.url,
            "title":   r.title,
            "domain":  r.domain,
            "snippet": r.snippet[:300],
        })

    # Backstop: if the LLM didn't cite any tags, include the first
    # web source so the medic at least has the top result reachable
    # from the ContextRail.
    if not cited_webs_in_answer and search.results:
        r = search.results[0]
        cited_webs_in_answer.append({
            "kind":    "web_source",
            "w_id":    r.w_id,
            "url":     r.url,
            "title":   r.title,
            "domain":  r.domain,
            "snippet": r.snippet[:300],
        })

    refs: list[dict] = [
        {"node_id": nid, "kind": "graph_node"} for nid in cited_nodes_in_answer
    ] + cited_webs_in_answer

    yield RetrievalChunk("citations", {"refs": refs})
    yield RetrievalChunk("turn_complete", {})


async def retrieve_async(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    patient_hash: Optional[str],
    question: str,
    attachment_images: Optional[list[tuple[str, str, bytes]]] = None,
    research_scope: Optional[dict] = None,
    session_id: Optional[str] = None,
) -> AsyncIterator[RetrievalChunk]:
    """Async retrieval dispatcher.

    Existing behaviour: T1/T2 sync SQL/template paths bridged into an
    async iterator; T3 calls the LLM via llm_gateway.

    ``research_scope`` (Research Workspace) — when set, the retriever:
        * Forces T3 with a cohort-aware system prompt
        * Names the study + lists the cohort patient hashes for the LLM
        * Loads external knowledge tools (pubmed/CTCAE/OpenFDA/etc.)
        * Biases the persona toward cohort-level reasoning
        * Respects ``focus_patient_hash`` (D2): when the medic
          explicitly focused a patient, narrow patient_hash so writes
          are properly scoped

    Shape:
        {"kind": "research"|"cross_patient",
         "study_id": "...", "patient_hashes": [...],
         "focus_patient_hash": "..." | None}

    When ``research_scope`` is None, behaviour is exactly as before.
    """
    # If the medic attached images, force T3.
    has_images = bool(attachment_images)
    if has_images:
        async for chunk in yield_t3_llm(
            conn, user_id=user_id, patient_hash=patient_hash,
            question=question, attachment_images=attachment_images,
            research_scope=research_scope,
            session_id=session_id,
        ):
            yield chunk
        return

    # Research scope override: always T3 with cohort context.
    if research_scope and research_scope.get("kind") in ("research", "cross_patient"):
        focus = research_scope.get("focus_patient_hash")
        async for chunk in yield_t3_llm(
            conn, user_id=user_id,
            patient_hash=focus or patient_hash,
            question=question, attachment_images=None,
            research_scope=research_scope,
            session_id=session_id,
        ):
            yield chunk
        return

    choice = classify(conn, user_id=user_id, patient_hash=patient_hash, question=question)
    logger.info(
        "retrieve_async: user=%s patient=%s tier=%s reason=%s",
        user_id, patient_hash, choice.tier.value, choice.reason,
    )
    if choice.tier == Tier.T1 and choice.view_kind and patient_hash:
        for chunk in yield_t1(conn, user_id=user_id,
                              patient_hash=patient_hash,
                              view_kind=choice.view_kind):
            yield chunk
    elif choice.tier == Tier.T2 and choice.anchor_hint and patient_hash:
        for chunk in yield_t2(conn, user_id=user_id, patient_hash=patient_hash,
                              question=question, anchor=choice.anchor_hint):
            yield chunk
    elif choice.tier == Tier.T4:
        async for chunk in yield_t4_web(
            conn, user_id=user_id, patient_hash=patient_hash, question=question,
            session_id=session_id,
        ):
            yield chunk
    else:
        async for chunk in yield_t3_llm(
            conn, user_id=user_id, patient_hash=patient_hash, question=question,
            session_id=session_id,
        ):
            yield chunk
