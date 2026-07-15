"""Chat SSE endpoint (UX v2 §8.2 + Rev-4).

POST /api/v1/agent/chat — streams Server-Sent Events:

  turn_started → tier_classified → [reasoning_chunk | search_query |
  search_results_summary | image_attached]* → final_answer_chunk* →
  citations → turn_complete

Context redesign phase 1 adds an additive ``context_info`` frame after
``tier_classified`` on the LLM-backed tiers (T3/T4)::

  {type:'context_info', history_msgs, summary_included,
   retrieval_blocks, dropped_history, dropped_blocks, token_estimate}

It is emitted by retrieval_tiers (via context_builder.build) and
passed through here like every other RetrievalChunk. Existing clients
ignore unknown frame types (the desktop api-client switch has a
``default: break``), so this is wire-compatible.

Every turn:
1. user_message event written to event_log
2. retrieval_tiers.retrieve() yields tier-specific events
3. assistant_response event written to event_log with full text +
   model + prompt_id + citations payload
4. Background: chat_ingester runs to extract entities (M0 already wires
   this; we don't block the response on it)

Auth-gated; user_id closed over server-side per same pattern as
memory_router_v2.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from nexus_server.auth.routes import get_current_user
from nexus_server.database import get_db_connection
from nexus_server.event_sourcing import EventKind, Store, init_event_sourcing_schema
from nexus_server.event_sourcing.handlers import (
    _h_assistant_response,
    _h_user_message,
)
from nexus_server.retrieval_tiers import retrieve_async

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


class ChatScope(BaseModel):
    """Research Workspace scope override (design §3.3.4 + §5.4).

    kind='research' triggers:
      * cohort retrieval (patient_hash IN enrolled∪candidates)
      * injection of external_knowledge tools (pubmed/semantic_scholar/
        CTCAE/OpenFDA/etc.)
      * persona prompt that biases the LLM toward cohort-level reasoning

    focus_patient_hash is the per-turn "🎯 聚焦" chip (D2 — Research
    Chat may write to a single patient only when explicitly scoped).
    """
    kind: str = "patient"                  # 'patient' | 'research' | 'cross_patient'
    study_id: Optional[str] = None
    focus_patient_hash: Optional[str] = None


class ChatRequest(BaseModel):
    text: str
    session_id: str
    patient_hash: Optional[str] = None
    # File IDs the medic attached to this turn (pasted images, dropped
    # PDFs, etc.). Front end uploads each via /api/v1/files/upload first,
    # then references them here. The server enriches the question with
    # each attachment's name + extracted text (when available) so the
    # downstream LLM sees them. Images get a name-only mention until we
    # ship vision-API plumbing through ``llm_gateway.call_llm``.
    attachments: list[str] = []
    # Research Workspace scope (optional). When omitted, the existing
    # patient-scope semantics apply.
    scope: Optional[ChatScope] = None
    # Explicit per-message skill invocation (the "/" menu in the
    # composer). Each name must be an installed + enabled skill for
    # this user — unknown / disabled names are silently dropped.
    # Independent of this list, skills flagged auto_apply=1 in
    # user_skill_prefs are injected on EVERY v2 turn. See
    # skills_router.build_skills_block.
    skills: list[str] = []


def _sse(event: dict) -> str:
    """Serialise a chunk as an SSE message."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/chat")
async def chat(
    req: ChatRequest,
    request: Request,
    current_user: str = Depends(get_current_user),
):
    """Stream a chat turn as SSE events. See module docstring for shape."""
    # A turn is valid if EITHER the medic typed something OR they
    # attached at least one file. Pasting a screenshot with no text
    # ("what is this?") should not be rejected — that's a legitimate
    # "tell me about this" intent. We synthesise a generic prompt
    # downstream when text is empty.
    if not req.text.strip() and not (req.attachments or []):
        raise HTTPException(status_code=400, detail="empty message")

    async def event_stream() -> AsyncIterator[str]:
        with get_db_connection() as conn:
            init_event_sourcing_schema(conn)
            store = Store(conn)

            # Resolve attachments → text + image-bytes per file. Three
            # tracks downstream:
            #
            #   A. Text-extractable (txt / md / csv / pdf / docx / etc.):
            #      pull from uploads.extracted_text, OR on-demand-extract
            #      from disk_path via nexus_core.distiller.extract_text
            #      and cache back to the row. Text goes into the prompt
            #      preamble.
            #
            #   B. Image (png / jpeg / tiff / webp / gif): collect the
            #      raw bytes for the multimodal Gemini call in
            #      yield_t3_llm. The LLM gets Part.from_bytes so it
            #      actually SEES the screenshot the medic pasted —
            #      previously the chat just echoed "I can't view this
            #      file" because we never fed bytes through.
            #
            #   C. Anything else: name-only mention in the preamble so
            #      the LLM at least acknowledges the attachment exists.
            attachment_meta: list[dict] = []
            attachment_preamble_parts: list[str] = []
            attachment_images: list[tuple[str, str, bytes]] = []  # (name, mime, raw)
            for fid in (req.attachments or []):
                try:
                    row = conn.execute(
                        "SELECT name, mime, extracted_text, disk_path "
                        "FROM uploads "
                        "WHERE user_id = ? AND file_id = ?",
                        (current_user, fid),
                    ).fetchone()
                except Exception:  # noqa: BLE001
                    row = None
                if not row:
                    continue
                name = str(row[0] or fid)
                mime = str(row[1] or "")
                etext = str(row[2] or "").strip()
                disk_path = str(row[3] or "")

                is_image = mime.startswith("image/")

                # Track A — on-demand text extraction if not cached.
                if not etext and not is_image and disk_path:
                    try:
                        from pathlib import Path as _Path
                        p = _Path(disk_path)
                        if p.is_file():
                            raw = p.read_bytes()
                            from nexus_server.files import (
                                _bytes_to_text, _save_extracted_text,
                            )
                            text_out = _bytes_to_text(raw, name, mime)
                            if text_out:
                                etext = text_out.strip()
                                _save_extracted_text(fid, etext)
                    except Exception as e:  # noqa: BLE001
                        logger.debug(
                            "lazy extract for %s failed: %s", fid[:8], e,
                        )

                # Track B — collect image bytes for the vision call.
                if is_image and disk_path:
                    try:
                        from pathlib import Path as _Path
                        p = _Path(disk_path)
                        if p.is_file():
                            raw = p.read_bytes()
                            # Cap each image at 4 MB so a pathologically
                            # huge paste doesn't OOM the LLM call. Real
                            # screenshots / photos are well under this.
                            if len(raw) <= 4 * 1024 * 1024:
                                attachment_images.append((name, mime, raw))
                            else:
                                logger.warning(
                                    "image %s exceeds 4MB (%d bytes) — "
                                    "skipping vision pass",
                                    name, len(raw),
                                )
                    except Exception as e:  # noqa: BLE001
                        logger.debug(
                            "image read for %s failed: %s", fid[:8], e,
                        )

                attachment_meta.append({
                    "file_id": fid, "name": name, "mime": mime,
                    "has_text": bool(etext),
                    "is_image": is_image,
                })

                if etext:
                    # Cap each attachment's inlined text so a 500-page PDF
                    # doesn't blow the prompt context. 8 KB per attachment
                    # × 5 attachments = 40 KB of preamble — well within
                    # Gemini 2.5 Flash's 1M-token window.
                    snippet = etext[:8000]
                    attachment_preamble_parts.append(
                        f"--- {name} ({mime}) ---\n{snippet}"
                    )
                elif is_image:
                    attachment_preamble_parts.append(
                        f"--- {name} ({mime}) ---\n"
                        f"(image attached — see it inline in the model's "
                        f"input. Describe what's shown if relevant to "
                        f"the question.)"
                    )
                else:
                    attachment_preamble_parts.append(
                        f"--- {name} ({mime or 'unknown'}) ---\n"
                        f"(binary file — no text content extractable. "
                        f"Tell the medic what format it is and ask for "
                        f"clarification if their question depends on "
                        f"the contents.)"
                    )

            # Synthesise a default question when the medic pasted only
            # files (no text). Gives the LLM something concrete to do
            # AND tells it explicitly to look at the attachments.
            base_question = req.text.strip()
            if not base_question:
                if attachment_images:
                    base_question = (
                        "What does the attached image show? Please describe "
                        "it in clinical terms relevant to this patient."
                    )
                else:
                    base_question = (
                        "Summarise the attached file(s) and tell me anything "
                        "clinically relevant for this patient."
                    )

            question_for_retrieval = base_question
            if attachment_preamble_parts:
                question_for_retrieval = (
                    "The medic attached the following file(s) to this turn:\n\n"
                    + "\n\n".join(attachment_preamble_parts)
                    + "\n\n--- QUESTION ---\n"
                    + base_question
                )

            # 1. Persist the user message + announce turn
            user_idx = store.emit_and_apply(
                kind=EventKind.USER_MESSAGE,
                payload={
                    "text":        req.text,
                    "session_id":  req.session_id,
                    "attachments": [a["file_id"] for a in attachment_meta],
                },
                apply_fn=_h_user_message,
                user_id=current_user, patient_hash=req.patient_hash,
            )
            logger.info(
                "chat: USER_MESSAGE persisted idx=%d session_id=%r "
                "patient_hash=%s text_chars=%d",
                user_idx, req.session_id,
                (req.patient_hash[:12] if req.patient_hash else "(none)"),
                len(req.text),
            )
            yield _sse({
                "type": "turn_started",
                "event_idx": user_idx,
                "patient_hash": req.patient_hash,
                "attachments": attachment_meta,
            })

            # 1.5 — Research scope: resolve cohort if scope.kind='research'.
            # We do this once per turn (cheap SQL) and stash it on the
            # retrieve_async call so the LLM sees cohort context + external
            # knowledge tool catalog in its system prompt.
            research_scope_dict: Optional[dict] = None
            if req.scope and req.scope.kind in ("research", "cross_patient"):
                cohort: list[str] = []
                if req.scope.study_id:
                    try:
                        # Pull cohort from study_enrollments (enrolled) +
                        # screening_evaluations (any patient with at least one
                        # screening row — likely_eligible/partial/excluded —
                        # gives the "research view" a wider denominator).
                        rows = conn.execute(
                            """
                            SELECT DISTINCT patient_hash FROM study_enrollments
                            WHERE user_id = ? AND study_id = ?
                              AND status IN ('enrolled','completed')
                            UNION
                            SELECT DISTINCT patient_hash FROM screening_evaluations
                            WHERE user_id = ? AND study_id = ?
                            """,
                            (current_user, req.scope.study_id,
                             current_user, req.scope.study_id),
                        ).fetchall()
                        cohort = [r[0] for r in rows if r[0]]
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "cohort resolve failed (study=%s): %s",
                            req.scope.study_id[:8] if req.scope.study_id else "(none)",
                            exc,
                        )
                research_scope_dict = {
                    "kind": req.scope.kind,
                    "study_id": req.scope.study_id,
                    "patient_hashes": cohort,
                    "focus_patient_hash": req.scope.focus_patient_hash,
                }
                # Emit a header SSE event so the UI can show "scope=research,
                # cohort_size=N" right away (before LLM tokens arrive).
                yield _sse({
                    "type": "scope_resolved",
                    "kind": req.scope.kind,
                    "study_id": req.scope.study_id or "",
                    "cohort_size": len(cohort),
                    "focus_patient_hash": req.scope.focus_patient_hash or "",
                })

            # 1.7 — Skills injection (per-user skills management).
            # Compose the ACTIVE SKILLS system-prompt block: explicit
            # "/" invocations from req.skills (installed+enabled only;
            # others silently dropped) + every enabled auto_apply
            # skill. When an explicit invocation resolved, force the
            # T3 LLM path so the skill always reaches the model
            # (T1/T2 template answers never see the system prompt).
            skills_block = ""
            applied_skills: list[str] = []
            force_t3 = False
            try:
                from nexus_server.skills_router import build_skills_block
                skills_block, applied_skills = build_skills_block(
                    current_user, req.skills or [],
                )
                force_t3 = bool(
                    set(req.skills or []) & set(applied_skills)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "skills block build failed (non-fatal): %s", exc,
                )
            if applied_skills:
                yield _sse({
                    "type": "skills_applied",
                    "skills": applied_skills,
                })

            # 2. Run retrieval — yields RetrievalChunk events
            collected_answer: list[str] = []
            collected_refs: list[dict] = []
            async for chunk in retrieve_async(
                conn,
                user_id=current_user,
                patient_hash=(
                    research_scope_dict.get("focus_patient_hash")
                    if research_scope_dict else req.patient_hash
                ),
                question=question_for_retrieval,
                attachment_images=attachment_images,
                research_scope=research_scope_dict,
                session_id=req.session_id,
                skills_block=skills_block,
                force_t3=force_t3,
            ):
                if chunk.kind == "final_answer_chunk":
                    collected_answer.append(chunk.data.get("text", ""))
                if chunk.kind == "citations":
                    collected_refs = chunk.data.get("refs", [])
                yield _sse({"type": chunk.kind, **chunk.data})
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0)   # cooperative yield

            # 3. Persist the assistant response verbatim per Rev-8
            full_text = "".join(collected_answer)
            assistant_idx = store.emit_and_apply(
                kind=EventKind.ASSISTANT_RESPONSE,
                payload={
                    "text":          full_text,
                    "session_id":    req.session_id,
                    "model":         "tier-orchestrator@1.0",
                    "prompt_id":     "chat_tiered_v1",
                    "prompt_version":"1.0",
                    "citations":     collected_refs,
                },
                apply_fn=_h_assistant_response,
                user_id=current_user, patient_hash=req.patient_hash,
                caused_by=user_idx,
            )
            logger.info(
                "chat: ASSISTANT_RESPONSE persisted idx=%d session_id=%r "
                "answer_chars=%d citations=%d",
                assistant_idx, req.session_id, len(full_text),
                len(collected_refs),
            )

            # Scheduled-task intent extraction. Runs after the
            # assistant emit so the medic sees the answer first, then
            # the proposal card (if any) is rendered above the
            # turn-complete marker. Heuristic-only in Phase 1 — see
            # schedule_intent.extract_proposal docstring for which
            # phrasings match. The UI clicks Confirm to actually
            # persist via POST /api/v1/schedule/confirm.
            try:
                from nexus_server import schedule_intent
                # Phase 1 always uses UTC for user_tz at extraction
                # time; the desktop sends its tz on /schedule/confirm
                # and the medic can adjust the time in the card. Phase 2
                # threads the user_tz into ChatRequest.
                proposal = schedule_intent.extract_proposal(
                    user_text=req.text,
                    user_tz="UTC",
                    session_id=req.session_id,
                    patient_hash=req.patient_hash,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("schedule extract crashed: %s", exc)
                proposal = None
            if proposal is not None:
                # Audit-log the proposal so we can measure
                # heuristic precision / recall later.
                try:
                    store.emit_and_apply(
                        kind=EventKind.SCHEDULED_TASK_PROPOSED,
                        payload={
                            "proposal_id":     proposal.proposal_id,
                            "kind":            proposal.kind,
                            "payload_json":    proposal.payload,
                            "fire_at":         proposal.fire_at,
                            "user_tz":         proposal.user_tz,
                            "summary":         proposal.summary,
                            "session_id":      proposal.session_id or "",
                            "patient_hash":    proposal.patient_hash or "",
                            "recurrence_cron": proposal.recurrence_cron or "",
                        },
                        apply_fn=lambda *_a, **_k: None,
                        user_id=current_user,
                        patient_hash=req.patient_hash,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "SCHEDULED_TASK_PROPOSED emit failed (UI "
                        "card still rendered): %s", exc,
                    )
                yield _sse({
                    "type":              "scheduled_task_proposed",
                    "proposal_id":       proposal.proposal_id,
                    "kind":              proposal.kind,
                    "fire_at":           proposal.fire_at,
                    "user_tz":           proposal.user_tz,
                    "summary":           proposal.summary,
                    "payload":           proposal.payload,
                    "recurrence_cron":   proposal.recurrence_cron,
                    "session_id":        proposal.session_id,
                    "patient_hash":      proposal.patient_hash,
                    "needs_user_input":  list(proposal.needs_user_input),
                })

            yield _sse({
                "type": "turn_complete",
                "assistant_event_idx": assistant_idx,
            })

            # 3.5 — Rolling session summary (context redesign phase 1).
            #    When this session has outgrown the 12-message history
            #    window by >= 6 messages and the stored summary is
            #    stale, fire an out-of-band task that re-summarises the
            #    fallen-out messages into chat_session_summaries. The
            #    turn NEVER waits on it — a stale/missing summary just
            #    means the next turn's window behaves like today.
            try:
                from nexus_server import context_builder
                context_builder.schedule_session_summary_update(
                    current_user, req.session_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "session summary schedule failed (non-fatal): %s", exc,
                )

            # 4. Fire the chat_ingester so this turn's clinical entities
            #    populate Layer 1 of the patient graph. Without this, the
            #    Memory tab stays at (0) and yield_t3_llm's next call has
            #    no PATIENT CONTEXT to ground in. Best-effort — failure
            #    here must not break the SSE stream we already finished.
            #
            #    We surface the outcome as an SSE event so the medic sees
            #    a small chip under the answer ("✓ 已记忆 6 项" / "本轮未
            #    记忆"). Without this surface the user has no signal that
            #    extraction is even attempted, and the Memory tab silently
            #    staying empty is one of the worst kinds of "feels broken
            #    but I don't know why" bug in the product.
            if req.patient_hash:
                ingester_outcome: dict = {"ok": False, "node_count": 0}
                try:
                    ingester_outcome = _run_chat_ingester_safe(
                        user_id=current_user,
                        patient_hash=req.patient_hash,
                        session_id=req.session_id,
                        source_event_idx=assistant_idx,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("chat_ingester failed (non-fatal): %s", exc)
                    ingester_outcome = {
                        "ok": False, "node_count": 0,
                        "error": str(exc)[:200],
                    }
                yield _sse({
                    "type":       "memory_ingested",
                    "ok":         bool(ingester_outcome.get("ok")),
                    "node_count": int(ingester_outcome.get("node_count", 0)),
                    "raw_count":  int(ingester_outcome.get("raw_count", 0)),
                    "error":      ingester_outcome.get("error", ""),
                })

            # 5. Layer 2 — run the practitioner heuristic extractor over
            #    the user's text, emit any matching observations, then
            #    run a distillation pass for this user.
            #
            #    Coverage policy (F9): style/workflow/practice/calibration
            #    patterns describe HOW THE MEDIC THINKS, not which patient.
            #    They are useful regardless of whether the turn is bound
            #    to a specific patient. So we fire the extractor on
            #    EVERY chat turn — patient-bound, per-study research, or
            #    cross-research — and synthesize a scope-tagged sentinel
            #    patient_hash when no real one is present. This keeps the
            #    schema's NOT NULL patient_hash constraint happy AND
            #    lets the distiller's distinct_patient_count aggregate
            #    across distinct scopes (per-study sentinel = different
            #    counts per study; cross-research sentinel = its own
            #    bucket). The patient-bound path remains the strongest
            #    signal because each real patient is one distinct row;
            #    research chats reinforce style/calibration mainly.
            try:
                obs_patient_hash = (
                    req.patient_hash
                    or _scope_sentinel_patient_hash(req.scope)
                )
                _run_practitioner_observation_safe(
                    user_id=current_user,
                    patient_hash=obs_patient_hash,
                    session_id=req.session_id,
                    user_text=req.text,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "practitioner observation failed (non-fatal): %s", exc,
                )

            # 6. Layer 2b — session takeaway distillation (LLM-based).
            #    Fires on a cadence (every 3rd assistant turn after the
            #    2nd) so we don't spam the LLM. Per-user only, scoped
            #    by patient / study / cross-research so retrieval can
            #    pick the right ones at next-turn assembly. Best-
            #    effort; never breaks the SSE turn.
            try:
                _run_session_takeaway_safe(
                    user_id=current_user,
                    patient_hash=req.patient_hash,
                    scope=req.scope,
                    session_id=req.session_id,
                    source_event_idx=assistant_idx,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "session_takeaway failed (non-fatal): %s", exc,
                )

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _run_chat_ingester_safe(
    *, user_id: str, patient_hash: str, session_id: str,
    source_event_idx: int,
) -> dict:
    """Run the chat_ingester for one encounter, log how it went, AND
    return a small outcome dict for the caller to surface to the UI.

    Returns ``{"ok": bool, "node_count": int, "raw_count": int}``.
    ``ok`` is True when at least one node was emitted; the medic chip
    treats ``ok=False`` with ``raw_count>0`` (LLM saw entities but all
    got rejected) differently from ``raw_count=0`` (LLM extractor
    failed outright — probably API key / quota).

    ``source_event_idx`` MUST be a real existing event_idx (typically
    the just-committed ASSISTANT_RESPONSE). chat_ingester passes it as
    ``caused_by`` on the INGESTION_STARTED event, and the event_log
    has a FK from caused_by → events.event_idx. Passing 0 produces
    "FOREIGN KEY constraint failed" and the whole ingest aborts.

    Idempotent: re-running on the same encounter just produces a
    second batch of NODE_ADDED events (the handler dedupes by
    (user_id, patient_hash, evidence_quote))."""
    from nexus_server.event_sourcing import Store, init_event_sourcing_schema
    from nexus_server.memorization.chat_ingester import ChatIngester
    from nexus_server.memorization.llm_extractor import (
        llm_chat_extractor, EXTRACTION_MODEL_TAG, EXTRACTION_PROMPT_ID,
    )

    with get_db_connection() as conn:
        init_event_sourcing_schema(conn)
        store = Store(conn)
        ingester = ChatIngester(
            store=store, conn=conn,
            extractor=llm_chat_extractor,
            extraction_model=EXTRACTION_MODEL_TAG,
            extraction_prompt_id=EXTRACTION_PROMPT_ID,
        )
        # Hack: read the most recent INGESTION_LLM_RESPONSE event's
        # raw entity count, so we can tell apart "LLM produced nothing"
        # from "LLM produced N but all got dropped". We do this by
        # reading the LLM-response row we just wrote.
        node_idxs = ingester.ingest_encounter(
            user_id=user_id,
            patient_hash=patient_hash,
            encounter_id=session_id or "(no-session)",
            source_event_idx=source_event_idx,
        )
        # Read back the most recent INGESTION_LLM_RESPONSE for this
        # patient to compute raw_count for the chip.
        raw_count = 0
        try:
            row = conn.execute(
                "SELECT payload_json FROM twin_event_log "
                "WHERE user_id = ? AND patient_hash = ? "
                "  AND event_kind = 'ingestion_llm_response' "
                "ORDER BY event_idx DESC LIMIT 1",
                (user_id, patient_hash),
            ).fetchone()
            if row:
                import json as _json
                payload = _json.loads(row[0])
                raw_text = payload.get("raw_output_text", "")
                # Cheap heuristic: count entity entries by parsing the
                # raw output JSON. Falls back to 0 on parse failure.
                try:
                    parsed = _json.loads(raw_text.strip().lstrip("```json").rstrip("```").strip())
                    raw_count = len(parsed.get("entities", []))
                except Exception:  # noqa: BLE001
                    raw_count = 0
        except Exception as e:  # noqa: BLE001
            logger.debug("ingestion diagnostics collection failed: %s", e)

        logger.info(
            "chat_ingester: user=%s patient=%s emitted %d node(s) "
            "from %d raw entities",
            user_id, patient_hash[:12], len(node_idxs), raw_count,
        )
        return {
            "ok": bool(node_idxs),
            "node_count": len(node_idxs),
            "raw_count": raw_count,
        }


def _scope_sentinel_patient_hash(scope) -> str:
    """Synthesize a stable, schema-valid placeholder for ``patient_hash``
    when a chat turn isn't tied to a real patient (cross-research or
    per-study research chat).

    Why a sentinel instead of allowing NULL: the practitioner_observations
    + practitioner_facts tables PK on ``(user_id, patient_hash, ...)``
    and the event_log row also requires patient_hash. Loosening that
    schema would touch ~12 indexes and a migration; a sentinel is one
    constant. Per-scope flavour gives the distiller a meaningful
    ``distinct_patient_count`` even outside the patient path:

      - per-study research chat → ``__study:<study_id_first8>__``
      - cross-research chat     → ``__cross_research__``
      - everything else         → ``__no_patient__``

    Prefixed with ``__`` so they sort/filter cleanly and never collide
    with real SHA256 hashes (which are hex-only).
    """
    if scope is None:
        return "__no_patient__"
    kind = getattr(scope, "kind", None)
    sid = getattr(scope, "study_id", None)
    if kind == "research" and sid:
        return f"__study:{sid[:8]}__"
    if kind == "cross_patient" or kind == "research":
        return "__cross_research__"
    return "__no_patient__"


def _run_session_takeaway_safe(
    *, user_id: str, patient_hash: Optional[str], scope,
    session_id: str, source_event_idx: int,
) -> None:
    """Cadence-gated LLM distillation of "what did Nexus learn about
    HOW this medic reasons?" — Layer 2b.

    See ``nexus_server/practitioner/session_takeaway.py`` for the
    cadence rules + LLM prompt. Best-effort; we log + swallow.
    """
    from nexus_server.event_sourcing import init_event_sourcing_schema
    from nexus_server.practitioner.session_takeaway import (
        should_distill_this_turn,
        distill_session_takeaways,
        scope_tuple_from_request,
    )

    with get_db_connection() as conn:
        init_event_sourcing_schema(conn)
        if not should_distill_this_turn(
            conn, user_id=user_id, session_id=session_id,
        ):
            return  # not our turn yet — silent skip
        scope_kind, scope_ref = scope_tuple_from_request(
            patient_hash=patient_hash, scope=scope,
        )
        row_ids = distill_session_takeaways(
            conn, user_id=user_id,
            scope_kind=scope_kind, scope_ref=scope_ref,
            session_id=session_id,
            source_event_idx=source_event_idx,
        )
        logger.info(
            "session_takeaway: user=%s scope=%s/%s session=%s — kept %d",
            user_id, scope_kind, scope_ref[:24], session_id,
            len(row_ids),
        )


def _run_practitioner_observation_safe(
    *, user_id: str, patient_hash: str, session_id: str, user_text: str,
) -> None:
    """Run the heuristic practitioner extractor over one user message.

    Two-step pipeline:

      1. Heuristic extractor returns 0..N Candidate objects for the
         user_text (see ``heuristic_extractor._RULES`` for the
         taxonomy).
      2. ``extract_from_encounter`` emits one
         PRACTITIONER_OBSERVATION_EMITTED event per candidate; the
         handler projects each into ``practitioner_observations``.
      3. ``distill`` walks observations and promotes any (kind,
         pattern_key) that has crossed N_THRESHOLDS to
         ``practitioner_facts``. Idempotent — re-running on the same
         user is a no-op when no new observations have landed.

    Logged at INFO so we can see in nexus_server.log whether the
    extractor is firing (often a "Layer 2 still empty" diagnosis is
    actually "no rules matched" rather than the older "writer never
    ran" failure mode this hook fixed).
    """
    from nexus_server.event_sourcing import Store, init_event_sourcing_schema
    from nexus_server.practitioner import (
        extract_from_encounter,
        distill,
    )
    from nexus_server.practitioner.heuristic_extractor import (
        extract_from_user_text,
    )

    # Run the regex pass against the raw user text. We do this BEFORE
    # touching the DB so an empty result set is a cheap no-op (no
    # connection, no transaction).
    encounter_id = session_id or "(no-session)"
    candidates = extract_from_user_text(
        user_text, source_encounter_id=encounter_id,
    )
    if not candidates:
        return

    # Use an in-line extractor lambda that returns the precomputed list
    # so we don't have to re-query event_log. This also threads the
    # source_encounter_id from above without the round-trip the
    # default ``heuristic_practitioner_extractor`` would do.
    def _replay_extractor(conn, _u, _p, _enc):
        return candidates

    with get_db_connection() as conn:
        init_event_sourcing_schema(conn)
        store = Store(conn)
        obs_idxs = extract_from_encounter(
            store, conn,
            user_id=user_id,
            patient_hash=patient_hash,
            source_encounter_id=encounter_id,
            extractor=_replay_extractor,
        )
        result = distill(store, conn, user_id=user_id)
        conn.commit()

    logger.info(
        "practitioner: user=%s emitted %d observation(s); "
        "distill surfaced=%d reinforced=%d",
        user_id, len(obs_idxs),
        result.candidates_surfaced, result.candidates_reinforced,
    )
