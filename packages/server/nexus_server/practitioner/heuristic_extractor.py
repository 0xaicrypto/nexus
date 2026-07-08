"""Deterministic regex-based practitioner extractor.

Replaces the M1.6 ``stub_practitioner_extractor`` (always returned [])
so Layer 2 can start observing actual medic behaviour before the M8
real-LLM extractor lands. The patterns here are deliberately
conservative: only language-side / workflow-side signals that don't
leak patient-specific data into ``practitioner_facts`` (which would
trip the Rev-5/R15 PHI invariants Store enforces at write time).

Signal taxonomy (kind → pattern_key family):

  style         How the medic phrases requests.
                Examples: ``style:terse_imperative``,
                ``style:bilingual_workflow``.
                Threshold: ≥3 distinct patients (N_THRESHOLDS).

  workflow      Procedural steps the medic reaches for repeatedly.
                Examples: ``workflow:compare_to_prior``,
                ``workflow:rule_out_protocol``,
                ``workflow:contrast_verification``,
                ``workflow:explicit_windowing_request``.
                Threshold: ≥5 distinct patients.

  practice      Decision-making patterns (conservative vs aggressive
                workup, follow-up cadence).
                Examples: ``practice:short_interval_followup``,
                ``practice:aggressive_workup``,
                ``practice:watchful_waiting``.
                Threshold: ≥5 distinct patients.

  calibration   How the medic gauges thresholds. INTENTIONALLY narrow
                here — calibration signals interact with the agent's
                suggestion stream (per Rev-5 they actively suppress
                suggestions), so we want high confidence before
                surfacing one. We only emit calibration on phrases
                that explicitly name a calibration ("I usually...",
                "for this kind of patient I...").
                Threshold: ≥8 distinct patients.

Each candidate carries a verbatim ``evidence_quote`` (the matched
phrase, truncated). The distiller aggregates by (kind, pattern_key)
and only promotes when N-of-distinct-patients crosses threshold —
so spam-matching is naturally rate-limited by patient diversity.

This module is dependency-light by design — pure stdlib + re. Real
M8 will swap it back to an LLM call; until then this gives us a
working pipeline + smoke data to demo Layer 2 with.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from nexus_server.practitioner.extractor import Candidate

# ─────────────────────────────────────────────────────────────────────
# Pattern table
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _PatternRule:
    """One regex → (fact_kind, pattern_key) rule.

    ``regex`` is case-insensitive by convention (compiled with re.I).
    ``evidence_clip`` is the max chars we keep from the matched text —
    we deliberately strip down to a short signature to minimise the
    risk of accidentally carrying patient-identifying tokens into
    practitioner_observations.
    """
    regex: re.Pattern[str]
    fact_kind: str
    pattern_key: str
    evidence_clip: int = 120


# Ordered so more-specific patterns match first. Stops at the first
# matched rule per category — a single user message can produce at
# most one observation per (fact_kind) bucket, which keeps the
# emit volume bounded and avoids one verbose question generating
# 10 observations.
# Note on \b and CJK:
#   Python's re ``\b`` is the ASCII-style "transition between word and
#   non-word char". CJK ideographs are word chars in re.UNICODE mode,
#   but adjacent ideographs don't have a ``\b`` between them — so a
#   pattern like ``\b对比\b`` only matches at the edges of a CJK run.
#   For mixed Chinese/English text the ``\b`` ends up false-rejecting
#   common phrasings ("请对比之前的扫描" — no word boundary after 历史
#   or before 对比 in the original test fixture).
#
#   We compromise: English alternatives keep ``\b``; Chinese
#   alternatives drop it and are anchored by their literal chars. This
#   accepts a small false-positive risk on broken-mid-word matches
#   (basically nil for our domain phrases) in exchange for actually
#   firing on real medic input.
_RULES: list[_PatternRule] = [
    # ───── workflow ─────
    _PatternRule(
        re.compile(
            r"(?:\b(?:compare(?:d)?\s+(?:to|with)\s+prior|prior\s+(?:study|scan|imaging))\b"
            r"|对比.{0,8}(?:之前|既往|历史|prior)"
            r"|(?:之前|既往|历史).{0,8}对比)",
            re.I | re.U,
        ),
        "workflow", "workflow:compare_to_prior",
    ),
    _PatternRule(
        re.compile(r"(?:\b(?:rule[\s-]?out|r/o)\b|排除)", re.I | re.U),
        "workflow", "workflow:rule_out_protocol",
    ),
    _PatternRule(
        re.compile(
            r"(?:\b(?:with(?:\s+iv)?\s+contrast|contrast[\s-]?enhanced)\b"
            r"|增强(?:扫描|CT|MR|MRI)?)",
            re.I | re.U,
        ),
        "workflow", "workflow:contrast_verification",
    ),
    _PatternRule(
        re.compile(
            r"(?:\b(?:windowing|change\s+(?:the\s+)?window|lung\s+window|mediastinal\s+window|bone\s+window)\b"
            r"|窗位|窗宽|肺窗|纵隔窗|骨窗)",
            re.I | re.U,
        ),
        "workflow", "workflow:explicit_windowing_request",
    ),
    _PatternRule(
        re.compile(
            r"(?:\b(?:MIP|maximum\s+intensity\s+projection|MPR|multiplanar)\b"
            r"|多平面|最大密度投影)",
            re.I | re.U,
        ),
        "workflow", "workflow:advanced_reconstruction",
    ),

    # ───── practice ─────
    _PatternRule(
        re.compile(
            r"(?:\b(?:follow[\s-]?up\s+in\s+\d+\s*(?:weeks?|months?|mo)"
            r"|short[\s-]?interval\s+follow[\s-]?up)\b"
            r"|\d+\s*(?:周|月|个月).*?随访"
            r"|随访.*?\d+\s*(?:周|月|个月))",
            re.I | re.U,
        ),
        "practice", "practice:short_interval_followup",
    ),
    _PatternRule(
        re.compile(
            r"(?:\b(?:biopsy|tissue\s+sampling)\b|穿刺|活检)",
            re.I | re.U,
        ),
        "practice", "practice:aggressive_workup",
    ),
    _PatternRule(
        re.compile(
            r"(?:\b(?:watchful\s+waiting|observe(?:\s+only)?|conservative\s+management)\b"
            r"|观察(?:即可|随访)?|保守(?:观察|处理))",
            re.I | re.U,
        ),
        "practice", "practice:watchful_waiting",
    ),
    _PatternRule(
        re.compile(
            r"(?:\b(?:multidisciplinary|MDT|tumor\s+board)\b|多学科(?:讨论|MDT)?)",
            re.I | re.U,
        ),
        "practice", "practice:mdt_referral",
    ),

    # ───── style ─────
    # Bilingual: any Chinese character anywhere in the message — auto-detected
    # via the CJK Unified Ideographs block.
    _PatternRule(
        re.compile(r"[一-鿿]"),
        "style", "style:bilingual_workflow",
    ),
    # Very short imperative: starts with a clinical verb, ≤6 words total.
    # Catches "show me the windowing", "find the nodule", "list mets".
    _PatternRule(
        re.compile(r"^\s*(?:show|find|list|highlight|circle|count|measure|report)\s+\w+(?:\s+\w+){0,4}\s*[\.\?!]?\s*$", re.I),
        "style", "style:terse_imperative",
    ),
    # Citation-asker: the medic repeatedly asks "where did you get that"
    # / "source?" — important signal that they want grounding by default.
    _PatternRule(
        re.compile(
            r"(?:\b(?:source\?|cite\s+(?:this|that)|where\s+(?:did|do)\s+you\s+(?:get|find))\b"
            r"|根据|出处|引用)",
            re.I | re.U,
        ),
        "style", "style:citation_seeker",
    ),

    # ───── calibration ─────
    # Self-reported calibration phrases — narrow on purpose (see
    # module docstring on calibration's stricter threshold).
    _PatternRule(
        re.compile(
            r"(?:\b(?:I\s+(?:usually|always|never|tend\s+to)|in\s+my\s+(?:practice|experience))\b"
            r"|我(?:通常|一般|总是|从不|习惯))",
            re.I | re.U,
        ),
        "calibration", "calibration:explicit_self_calibration",
    ),
]


# ─────────────────────────────────────────────────────────────────────
# Extractor entrypoint
# ─────────────────────────────────────────────────────────────────────


def _clip(text: str, n: int) -> str:
    """Truncate `text` to `n` chars + ellipsis. We do this on the matched
    span (not the full message) so practitioner_observations never
    carries a 1000-char prompt verbatim."""
    text = text.strip()
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def extract_from_user_text(
    user_text: str,
    *,
    source_encounter_id: str,
) -> list[Candidate]:
    """Apply every rule to one user message; return at most one
    candidate per fact_kind so a single verbose turn doesn't dominate
    the observation log.

    Caller (chat_router) is expected to pass the medic's raw
    question text — the text BEFORE any LLM enrichment. We avoid the
    assistant reply because it contains LLM-generated patient
    summaries that would trip privacy filters at the projection layer.
    """
    if not user_text or not user_text.strip():
        return []

    candidates: list[Candidate] = []
    seen_kinds: set[str] = set()
    for rule in _RULES:
        if rule.fact_kind in seen_kinds:
            continue
        m = rule.regex.search(user_text)
        if m is None:
            continue
        seen_kinds.add(rule.fact_kind)
        candidates.append(Candidate(
            fact_kind=rule.fact_kind,
            pattern_key=rule.pattern_key,
            evidence_quote=_clip(m.group(0), rule.evidence_clip),
            source_encounter_id=source_encounter_id,
            extraction_model="heuristic-v1@0.1",
            extraction_prompt_id="practitioner_heuristic_v1",
        ))
    return candidates


def heuristic_practitioner_extractor(
    conn: sqlite3.Connection,
    user_id: str,
    patient_hash: str,
    source_encounter_id: str,
) -> list[Candidate]:
    """The ``Extractor`` callable shape that ``extract_from_encounter``
    accepts. We look up the most recent user_message for the encounter
    in this conn and run the heuristics over it.

    Returns [] when no user_message exists yet (called too early, or
    the row hasn't committed). This is the safe failure mode — Layer 2
    just doesn't grow for that turn; nothing else breaks."""
    row = conn.execute(
        # twin_event_log isn't on the v3 event_log table — different DB
        # entirely. This extractor walks the v3 event_log instead,
        # which is what ``extract_from_encounter`` is bound to via
        # the Store passed in. The 'user_message' kind name matches
        # event_kinds.py.
        "SELECT payload_json FROM event_log "
        "WHERE user_id = ? "
        "  AND (patient_hash = ? OR patient_hash IS NULL) "
        "  AND kind = 'user_message' "
        "ORDER BY event_idx DESC LIMIT 1",
        (user_id, patient_hash),
    ).fetchone()
    if row is None:
        return []
    import json
    try:
        payload = json.loads(row[0])
    except Exception:  # noqa: BLE001
        return []
    text = str(payload.get("text") or "")
    return extract_from_user_text(text, source_encounter_id=source_encounter_id)
