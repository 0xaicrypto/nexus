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
) -> AsyncIterator[RetrievalChunk]:
    """T3 — real LLM-grounded answer. Replaces the placeholder.

    Pipeline:
      1. Emit tier_classified + a reasoning preview (so the UI's
         TierIndicator + ReasoningPane have something to render).
      2. Pull patient context from clinical_graph_nodes.
      3. Call llm_gateway.call_llm with a clinician-grounded system
         prompt + the patient context. When ``attachment_images`` is
         non-empty, bypass the gateway and call google.genai directly
         with ``Part.from_bytes`` for each image — the gateway path
         is still text-only.
      4. Emit the LLM's answer as a single final_answer_chunk + a
         citations event with any nodes we grounded on.
    """
    yield RetrievalChunk("tier_classified", {"tier": "T3"})
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
        "When relevant, ground your answer in the patient context "
        "provided below. If the context is empty or does not address "
        "the question, answer from general medical knowledge but say "
        "so explicitly. Always recommend professional review for any "
        "decision-bearing output. Do NOT include hedging boilerplate; "
        "the medic is qualified."
        "\n\n"
        "CITATION PROTOCOL: every item in the PATIENT CONTEXT below "
        "is prefixed with a tag like ``[N42]``. When you mention or "
        "rely on that item in your answer, append the same tag right "
        "after the relevant phrase, e.g. \"8 mm RUL nodule [N42] "
        "needs follow-up.\" Use only IDs that appear in the context "
        "block — never invent one. These tags drive the desktop's "
        "citation chips and right-rail drill-down."
    )
    if context_block:
        system_prompt += "\n\nPATIENT CONTEXT (from the local clinical graph):\n" + context_block

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
            content, model, _stop, _tools = await llm_gateway.call_llm(
                messages=[{"role": "user", "content": question}],
                system_prompt=system_prompt,
                model=None,            # use config.DEFAULT_LLM_MODEL
                temperature=0.4,
                max_tokens=1024,
                tools=None,
            )
            logger.info("yield_t3_llm: model=%s answer_chars=%d", model, len(content))
            text = content.strip() or "(no response)"
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


async def retrieve_async(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    patient_hash: Optional[str],
    question: str,
    attachment_images: Optional[list[tuple[str, str, bytes]]] = None,
) -> AsyncIterator[RetrievalChunk]:
    """Async retrieval dispatcher. T1/T2 share their synchronous
    implementations (they're pure SQL/template), so we adapt them via
    a sync-iterator → async-iterator bridge. T3 uses yield_t3_llm,
    which actually calls the LLM gateway.

    ``attachment_images`` carries any pasted/dropped image bytes the
    medic attached to this turn (list of ``(name, mime, bytes)``).
    They flow to T3 only — Gemini Flash 2.5 with ``Part.from_bytes``
    multimodal — so the LLM literally sees the screenshot instead of
    just being told "an image is attached". T1/T2 are template/SQL
    paths and ignore them.
    """
    # If the medic attached images, force T3 — the visual content
    # has to land in front of the multimodal model, and T1's cached
    # views / T2's templated answer have no path to surface it.
    has_images = bool(attachment_images)
    if has_images:
        async for chunk in yield_t3_llm(
            conn, user_id=user_id, patient_hash=patient_hash,
            question=question, attachment_images=attachment_images,
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
    else:
        async for chunk in yield_t3_llm(
            conn, user_id=user_id, patient_hash=patient_hash, question=question,
        ):
            yield chunk
