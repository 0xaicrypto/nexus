"""Layer 2b — Session takeaway distiller.

Where ``heuristic_extractor`` (Layer 2) captures discrete, enumerable
patterns the regex rules know how to match, THIS module captures the
soft stuff: how the medic reasons through a case, what they weight
when choosing between options, when they push back on a guideline,
which tools they reach for. The output is 1-3 short sentences per
"insight" (a Takeaway), stored per-user in ``chat_takeaways`` and
injected into future turns' system prompts as PRIOR INSIGHTS.

Scope semantics
───────────────

A takeaway is attached to a ``scope`` so retrieval can pick the right
ones at next-turn assembly:

  - patient        → ``scope_ref = patient_hash``
                     Read back when next chatting about THIS patient
                     OR (lower priority) cross-patient questions.
  - research       → ``scope_ref = study_id``
                     Read back in this study's chat + cross-research.
  - cross_research → ``scope_ref = '__cross_research__'`` (sentinel)
                     Read back in workspace-level chat + any per-
                     study chat (lower priority).
  - other          → catch-all for chats that don't match the above
                     (e.g. settings exploration, no patient and no
                     study in scope).

Cadence
───────

We don't distill every turn — that's expensive and produces a
noisy stream. Rules (kept simple, tunable in one place below):

  - First distillation fires after turn #2 in the session (need at
    least one user+assistant exchange to summarise).
  - Subsequent distillations fire every 3rd assistant turn within
    the same session.
  - Hard cap: at most 12 takeaways per session.
  - Insights with confidence < 0.4 are dropped (LLM said "low
    confidence" or could not extract).

LLM prompt design
─────────────────

The extractor LLM is told it is observing a clinician's conversation
with an AI assistant. It must NOT extract patient-specific facts
(that's Layer 1's job) or doctrine that's already in textbooks. It
must focus on the MEDIC's own reasoning, preferences, and tool-use
patterns. Output is JSON-strict with one object per insight.

The prompt explicitly asks the LLM to OMIT a takeaway if the medic
didn't really demonstrate any reasoning beyond a textbook fact —
preventing noise overwhelming the table on uninteresting turns.

Privacy
───────

Each row PKs (effectively) on user_id; nothing crosses doctor
boundaries. Even when ``scope_kind = cross_research``, the
takeaway is per-user (different doctors using the same workspace
generate independent insight rows).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Tuning knobs (single-source-of-truth for distillation cadence)
# ─────────────────────────────────────────────────────────────────────

DISTILL_AFTER_TURN_INDEX = 2          # first distillation after turn #2
DISTILL_EVERY_N_TURNS    = 3          # then every 3rd assistant turn
DISTILL_HARD_CAP         = 12         # never store more than this per session
MIN_CONFIDENCE_KEEP      = 0.4        # below this → dropped
RECENT_TURN_WINDOW       = 6          # last N events (user+assistant) fed to LLM
DEDUP_JACCARD_THRESHOLD  = 0.55       # >= ⇒ treat as duplicate of an
                                       # existing insight; tuned for the
                                       # short (1-3 sentence) insights LLM
                                       # emits — too tight misses obvious
                                       # rephrasings, too loose collapses
                                       # genuinely distinct ideas.
EXTRACTION_MODEL_TAG     = "gemini-2.5-flash"
EXTRACTION_PROMPT_ID     = "session_takeaway_v1"

# Valid tag taxonomy. Constrained list so the UI can colour-code.
VALID_TAGS = (
    "clinical_reasoning",   # "how the medic ranked DDX"
    "preference",            # "prefers immunotherapy first-line in EGFR-wt NSCLC IIIB"
    "tool_use",              # "tends to ask for pubmed when discussing rare AE"
    "decision_rationale",    # "ordered FEV1 because DLCO was borderline"
    "disagreement",          # "pushed back on AI's suggestion of carbo over cis"
)


@dataclass(frozen=True)
class TakeawayCandidate:
    text: str
    tag: str
    confidence: float


_SYSTEM = """\
You are observing a clinician chatting with an AI assistant.

Extract ONLY insights about HOW THE CLINICIAN THINKS — never about
which patient, never restating textbook knowledge. Each insight must
generalise beyond this one conversation (e.g. "prefers X over Y when
Z" — not "today the patient had a 3.2cm nodule").

Output ONLY valid JSON. Schema:

  {"takeaways": [
    {
      "text":       "<one to three sentences>",
      "tag":        "clinical_reasoning" | "preference" |
                    "tool_use" | "decision_rationale" | "disagreement",
      "confidence": 0.0 - 1.0
    },
    ...
  ]}

Rules:
- Each takeaway must describe the CLINICIAN (not the AI). If the
  conversation was the AI telling the medic textbook stuff, return
  ``{"takeaways": []}`` — that's fine.
- 0-3 takeaways per pass is normal. Don't pad.
- Language: write the ``text`` in the SAME language as the chat. If
  the chat is in Chinese, write Chinese. Acronyms (NSCLC, RECIST,
  CTCAE, mg) keep their canonical form regardless.
- DO NOT include patient identifiers (names, hashes, dates,
  specific lesion sizes). Aggregate to general principles.
- Confidence: < 0.4 means "I'm not sure I really saw this" — those
  WILL be dropped by the consumer; only emit them if you can't
  decide between dropping and 0.5+.
"""


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def should_distill_this_turn(
    conn: sqlite3.Connection, *, user_id: str, session_id: str,
) -> bool:
    """Cadence gate. Returns True iff this turn satisfies the cadence
    rules (see module docstring). Falsey for the first turn, every
    non-Nth turn, or sessions that have already hit the hard cap."""
    if not session_id:
        return False

    # Count assistant turns in this session via twin_event_log.
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM twin_event_log "
            "WHERE user_id = ? "
            "  AND event_kind = 'assistant_response' "
            "  AND payload_json LIKE ?",
            (user_id, f"%{session_id}%"),
        )
        assistant_count = int(cur.fetchone()[0] or 0)
    except sqlite3.Error:
        return False

    if assistant_count < DISTILL_AFTER_TURN_INDEX:
        return False

    # Already at the per-session cap?
    cur = conn.execute(
        "SELECT COUNT(*) FROM chat_takeaways "
        "WHERE user_id = ? AND session_id = ?",
        (user_id, session_id),
    )
    so_far = int(cur.fetchone()[0] or 0)
    if so_far >= DISTILL_HARD_CAP:
        return False

    # Fire on turn # = DISTILL_AFTER_TURN_INDEX, then every N-th.
    if assistant_count == DISTILL_AFTER_TURN_INDEX:
        return True
    return (assistant_count - DISTILL_AFTER_TURN_INDEX) % DISTILL_EVERY_N_TURNS == 0


def gather_recent_chat(
    conn: sqlite3.Connection, *, user_id: str, session_id: str,
    window: int = RECENT_TURN_WINDOW,
) -> str:
    """Pull last ``window`` user+assistant events for this session.
    Builds a plain-text transcript the LLM can read directly."""
    rows = conn.execute(
        "SELECT event_kind, payload_json FROM twin_event_log "
        "WHERE user_id = ? "
        "  AND event_kind IN ('user_message', 'assistant_response') "
        "  AND payload_json LIKE ? "
        "ORDER BY event_idx DESC LIMIT ?",
        (user_id, f"%{session_id}%", window),
    ).fetchall()
    rows = list(reversed(rows))   # back to chronological
    parts: list[str] = []
    for kind, payload_json in rows:
        try:
            p = json.loads(payload_json)
        except json.JSONDecodeError:
            continue
        text = (p.get("text") or "").strip()
        if not text:
            continue
        speaker = "MEDIC" if kind == "user_message" else "ASSISTANT"
        parts.append(f"[{speaker}] {text}")
    return "\n\n".join(parts)


def distill_session_takeaways(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    scope_kind: str,
    scope_ref: str,
    session_id: str,
    source_event_idx: int,
) -> list[int]:
    """Run one LLM distillation pass and persist 0..N rows in
    ``chat_takeaways``. Returns the list of row IDs created.

    Caller is responsible for the cadence gate (see
    ``should_distill_this_turn``); this function distills every time
    it's called.
    """
    transcript = gather_recent_chat(
        conn, user_id=user_id, session_id=session_id,
    )
    if not transcript:
        return []

    try:
        from nexus_server import llm_gateway

        async def _call() -> str:
            content, _model, _stop, _tools = await llm_gateway.call_llm(
                messages=[{"role": "user", "content": transcript}],
                system_prompt=_SYSTEM,
                model=None,
                temperature=0.4,
                max_tokens=900,
                tools=None,
            )
            return content

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(asyncio.run, _call())
                    raw = fut.result(timeout=30)
            else:
                raw = loop.run_until_complete(_call())
        except RuntimeError:
            raw = asyncio.run(_call())
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_takeaway LLM call failed: %s", exc)
        return []

    parsed = _parse_json_safe(raw)
    candidates_raw = parsed.get("takeaways") or []

    # Validate + filter.
    kept: list[TakeawayCandidate] = []
    for item in candidates_raw:
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue
        tag = item.get("tag")
        if tag not in VALID_TAGS:
            continue
        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        if conf < MIN_CONFIDENCE_KEEP:
            continue
        # Tiny PHI scrubber — strip any string that looks like a SHA256
        # hash or an ISO date from the text body. The LLM is told not
        # to include them but we belt-and-brace.
        if _HASH_RE.search(text) or _DATE_RE.search(text):
            logger.info(
                "session_takeaway: dropping insight with hash/date in text: %r",
                text[:80],
            )
            continue
        kept.append(TakeawayCandidate(
            text=text, tag=tag, confidence=max(0.0, min(1.0, conf)),
        ))

    if not kept:
        return []

    # ── Dedup against existing takeaways for this user ────────────────
    # We don't have an embedding model in-process yet (would add a
    # tensor dep). Use a cheap text-similarity heuristic instead:
    # tokenize on whitespace + Chinese-character n-grams, compute
    # Jaccard. >= ``DEDUP_JACCARD_THRESHOLD`` ⇒ skip as duplicate.
    # We compare against the most recent 50 takeaways across ALL
    # scopes for this user — same insight expressed in two different
    # scopes is still a duplicate worth merging.
    existing_texts = [
        r[0] for r in conn.execute(
            "SELECT text FROM chat_takeaways "
            "WHERE user_id = ? AND medic_rejected_at IS NULL "
            "ORDER BY distilled_at DESC LIMIT 50",
            (user_id,),
        ).fetchall()
    ]
    deduped: list[TakeawayCandidate] = []
    dupes_dropped = 0
    for cand in kept:
        if any(
            _approx_similarity(cand.text, prior) >= DEDUP_JACCARD_THRESHOLD
            for prior in existing_texts
        ):
            dupes_dropped += 1
            continue
        # Also dedup within THIS batch — LLM sometimes restates the
        # same insight twice in one pass.
        if any(
            _approx_similarity(cand.text, d.text) >= DEDUP_JACCARD_THRESHOLD
            for d in deduped
        ):
            dupes_dropped += 1
            continue
        deduped.append(cand)

    if not deduped:
        logger.info(
            "session_takeaway: user=%s session=%s — all %d candidates "
            "were duplicates of existing insights",
            user_id, session_id, len(kept),
        )
        return []

    now = int(time.time())
    row_ids: list[int] = []
    for cand in deduped:
        cur = conn.execute(
            "INSERT INTO chat_takeaways "
            "(user_id, scope_kind, scope_ref, session_id, text, tag, "
            " confidence, distilled_at, source_event_idx) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, scope_kind, scope_ref, session_id,
                cand.text, cand.tag, cand.confidence,
                now, source_event_idx,
            ),
        )
        row_ids.append(int(cur.lastrowid or 0))
    if dupes_dropped:
        logger.info(
            "session_takeaway: user=%s session=%s — dropped %d duplicate(s)",
            user_id, session_id, dupes_dropped,
        )
    conn.commit()

    logger.info(
        "session_takeaway: user=%s scope=%s/%s session=%s — distilled %d "
        "(raw %d, dropped %d)",
        user_id, scope_kind, scope_ref[:24], session_id,
        len(row_ids), len(candidates_raw), len(candidates_raw) - len(row_ids),
    )
    return row_ids


# ─────────────────────────────────────────────────────────────────────
# Retrieval — read-back into the next turn's system prompt
# ─────────────────────────────────────────────────────────────────────


def fetch_prior_insights(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    scope_kind: str,
    scope_ref: str,
    limit: int = 5,
) -> list[dict]:
    """Return the most recent active (un-rejected) takeaways for this
    scope, sorted newest first.

    Cross-scope soft join: we also pull a few CROSS-scope takeaways so
    the medic's general reasoning shows up regardless of whether they
    happen to be in a patient chat or research chat right now. Mix
    ratio: 60% same-scope, 40% cross-scope.

    Orphan filter: ``scope_kind='patient'`` rows whose scope_ref no
    longer exists in the ``patients`` table are EXCLUDED. The medic
    deleted that patient → they don't want insights from that
    encounter leaking into the next conversation. This is the second
    line of defense; delete_patient also actively drops these rows,
    but a deployment that pre-dates the cascade fix may still have
    orphans on disk. Cheap join (≤5 rows).
    """
    same_n = max(1, int(limit * 0.6))
    cross_n = max(0, limit - same_n)

    same_rows = conn.execute(
        "SELECT t.text, t.tag, t.distilled_at FROM chat_takeaways t "
        "WHERE t.user_id = ? AND t.scope_kind = ? AND t.scope_ref = ? "
        "  AND t.medic_rejected_at IS NULL "
        "  AND (t.scope_kind <> 'patient' OR EXISTS ("
        "       SELECT 1 FROM patients p "
        "       WHERE p.user_id = t.user_id AND p.patient_hash = t.scope_ref"
        "  )) "
        "ORDER BY t.distilled_at DESC LIMIT ?",
        (user_id, scope_kind, scope_ref, same_n),
    ).fetchall()

    cross_rows = []
    if cross_n > 0:
        cross_rows = conn.execute(
            "SELECT t.text, t.tag, t.distilled_at FROM chat_takeaways t "
            "WHERE t.user_id = ? "
            "  AND NOT (t.scope_kind = ? AND t.scope_ref = ?) "
            "  AND t.medic_rejected_at IS NULL "
            "  AND (t.scope_kind <> 'patient' OR EXISTS ("
            "       SELECT 1 FROM patients p "
            "       WHERE p.user_id = t.user_id AND p.patient_hash = t.scope_ref"
            "  )) "
            "ORDER BY t.distilled_at DESC LIMIT ?",
            (user_id, scope_kind, scope_ref, cross_n),
        ).fetchall()

    out: list[dict] = []
    for r in list(same_rows) + list(cross_rows):
        out.append({"text": r[0], "tag": r[1], "at": r[2]})
    return out


def render_prior_insights_block(insights: list[dict]) -> str:
    """Render fetched insights as a markdown block for system-prompt
    injection. Returns empty string if no insights — the caller can
    skip the section heading entirely."""
    if not insights:
        return ""
    lines = [
        "PRIOR INSIGHTS (per-user, distilled from earlier chats; soft "
        "defaults, not rules — surface when applicable, flag when "
        "this case appears to contradict):",
        "",
    ]
    for ins in insights:
        tag = ins.get("tag") or "insight"
        text = ins.get("text") or ""
        lines.append(f"  • [{tag}] {text}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


_HASH_RE = re.compile(r"[0-9a-f]{32,}", re.IGNORECASE)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _tokens_for_similarity(s: str) -> set[str]:
    """Bag-of-tokens for Jaccard. Handles bilingual zh/en well enough
    for our 1-3-sentence insights:
      - Lowercase ASCII alphanumeric runs become whole tokens
      - Each Chinese character becomes its own token PLUS each
        bigram (覆盖率 → {覆,盖,率,覆盖,盖率}) so synonyms with
        shared 2-char roots still overlap
      - Punctuation + stopword-ish single chars ('的', 'a', 'i')
        dropped
    Cheap, no extra deps. Good enough for the noise level we care
    about (catching paraphrased duplicates of the same insight)."""
    s = (s or "").lower().strip()
    if not s:
        return set()
    tokens: set[str] = set()
    # ASCII runs
    for m in re.findall(r"[a-z0-9]{2,}", s):
        tokens.add(m)
    # Chinese chars + bigrams (only over consecutive CJK runs to avoid
    # punctuation bridging unrelated phrases)
    for run in re.findall(r"[一-鿿]+", s):
        for ch in run:
            tokens.add(ch)
        for i in range(len(run) - 1):
            tokens.add(run[i : i + 2])
    # Drop ultra-common single chars that add no discrimination.
    return tokens - {"的", "了", "是", "在", "和", "与", "及", "或",
                     "中", "下", "上", "a", "i", "an", "to", "of",
                     "is", "be"}


def _approx_similarity(a: str, b: str) -> float:
    """Jaccard similarity over the tokens defined above. Range 0-1."""
    ta = _tokens_for_similarity(a)
    tb = _tokens_for_similarity(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _parse_json_safe(raw: str) -> dict:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        logger.info("session_takeaway: LLM output not JSON: %r", s[:200])
        return {}


def scope_tuple_from_request(
    *, patient_hash: Optional[str], scope,
) -> tuple[str, str]:
    """Resolve (scope_kind, scope_ref) from the chat request fields.
    Matches the sentinel rules in chat_router._scope_sentinel_patient_hash.
    """
    if patient_hash:
        return ("patient", patient_hash)
    if scope is None:
        return ("other", "__no_patient__")
    kind = getattr(scope, "kind", None)
    sid = getattr(scope, "study_id", None)
    if kind == "research" and sid:
        return ("research", sid)
    if kind == "cross_patient" or kind == "research":
        return ("cross_research", "__cross_research__")
    return ("other", "__no_patient__")
