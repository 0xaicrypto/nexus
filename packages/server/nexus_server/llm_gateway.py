"""LLM Gateway router with tool execution loop.

Routes requests to configured LLM providers (Gemini, OpenAI, Anthropic,
Kimi/Moonshot AI — OpenAI-compatible).
When the LLM returns tool calls (web search, URL read, file generate),
the server executes them and feeds results back until a final text response.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus_server.auth import get_current_user

# #113: contextvar so provider-call helpers can read the calling
# user_id without threading it through every function signature.
# Twin chat handlers set this at turn-start; delegate sub-agents
# inherit it via the same async context. None when there's no
# authenticated caller (tests, dev runs) — usage metering bails.
import contextvars as _cv
_current_user_var: _cv.ContextVar[Optional[str]] = _cv.ContextVar(
    "nexus_current_user", default=None,
)
from nexus_server.config import get_config
from nexus_server.middleware import check_rate_limit

logger = logging.getLogger(__name__)
config = get_config()

router = APIRouter(prefix="/api/v1/llm", tags=["llm"])

# Maximum tool call rounds to prevent infinite loops
MAX_TOOL_ROUNDS = 5


def _twin_enabled() -> bool:
    """Phase D feature flag — when on, /llm/chat goes through TwinManager
    (Nexus DigitalTwin per-user) instead of the direct LLM gateway."""
    return bool(getattr(config, "USE_TWIN", False))


# ───────────────────────────────────────────────────────────────────────────
# Request/Response Models
# ───────────────────────────────────────────────────────────────────────────


class LLMMessage(BaseModel):
    """Message for LLM chat."""
    role: str = Field(..., pattern="^(user|assistant|system|tool)$")
    # min_length=0 (was 1) so the user can paste a file and hit Send
    # without typing anything — empty content is a valid user intent
    # when attachments are present. _fold_attachments_into_messages
    # handles "" content cleanly: it prepends the [Attachments] block
    # whether the existing user message has text or not.
    content: str = Field(..., min_length=0)
    tool_call_id: Optional[str] = None


class ToolCallInfo(BaseModel):
    """Tool call returned by the LLM."""
    id: str
    name: str
    arguments: dict = {}


# ── Attachments ───────────────────────────────────────────────────────────
#
# Two-tier policy:
#
# 1. UPLOAD CAP — how large a payload the server will *accept* on the wire
#    and durably store. Defaults to 100 MB; configurable via env so an
#    operator can tighten it (or a test can drop it dramatically).
#
# 2. INLINE-TEXT CAP — how much of an attachment's text content is folded
#    into the LLM prompt. Above this we splice in only a head excerpt + a
#    "[truncated …]" marker so we don't blow the model's context window
#    (Gemini 2.5 Flash is 1M tokens, but a single multi-MB doc will still
#    starve the rest of the conversation, and most models cost-per-token
#    scales with whatever we send). The full content stays on the wire so
#    downstream sync/anchor code can still durably store it.
import os as _os

MAX_ATTACHMENT_BYTES_TOTAL = int(
    # 2 GB default (was 100 MB) so DICOM CT zips — routinely 500 MB
    # to 1.5 GB — pass Pydantic's size_bytes field validation. Bytes
    # never sit inline in the chat request after #114 (thin-client
    # uses file_id), so the actual JSON payload is tiny; this bound
    # only constrains what the client is allowed to claim a file
    # is for the Attachment model.
    _os.environ.get("NEXUS_MAX_ATTACHMENT_BYTES", str(2 * 1024 * 1024 * 1024))
)
MAX_INLINE_TEXT_BYTES = int(
    _os.environ.get("NEXUS_MAX_INLINE_TEXT_BYTES", str(256 * 1024))
)


class Attachment(BaseModel):
    """A file attached to a chat turn.

    Either ``content_text`` (for text-decodable files) or ``content_base64``
    (for binary) should be set. The server folds text content into the last
    user message; binary content is summarised as metadata-only for now.
    """

    name: str = Field(..., min_length=1, max_length=512)
    mime: str = Field("application/octet-stream", max_length=255)
    # Per-attachment size matches the total cap — a single big file is fine,
    # only the *sum* across attachments triggers 413.
    size_bytes: int = Field(..., ge=0, le=MAX_ATTACHMENT_BYTES_TOTAL)
    content_text: Optional[str] = None
    content_base64: Optional[str] = None
    # Round 2-B (thin client): the modern path is the desktop uploads
    # files via /api/v1/files/upload, gets a file_id back, and references
    # it here. Server resolves the id, reads bytes from disk, and runs
    # distill. Old path (content_text / content_base64 set inline) still
    # works during transition.
    file_id: Optional[str] = None


class LLMChatRequest(BaseModel):
    """LLM chat request."""
    messages: list[LLMMessage] = Field(..., min_length=1, max_length=100)
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    # 32k upper bound covers Gemini 2.5 Pro (8k) + Claude (8k+) +
    # GPT-4o (16k). 8k was the old cap and was tight for long-form
    # writeups; raising it doesn't change defaults (None = provider
    # default) but lets future workflows request larger replies
    # without a schema-validation 422.
    max_tokens: Optional[int] = Field(None, ge=1, le=32768)
    enable_tools: bool = True
    # Optional file attachments; folded into the last user message
    attachments: list[Attachment] = Field(default_factory=list, max_length=20)
    # Multi-session support: route this turn to a specific chat thread.
    # See sessions.py / sessions_router.py for details.
    #   * omitted / None / "" — twin's current thread (the synthetic
    #     "default" pre-multi-session conversation).
    #   * any other id returned from POST /api/v1/sessions — twin
    #     saves a checkpoint of its current thread, switches to this
    #     one (loading message history filtered by it), and runs the
    #     turn there. The events twin appends are tagged with this id
    #     so subsequent /agent/messages?session_id=… reads see them.
    session_id: Optional[str] = None


def _fold_attachments_into_messages(
    messages: list[dict], attachments: list[Attachment]
) -> list[dict]:
    """Prepend a synthetic [Attachments] block to the last user message.

    Returns a *new* list; does not mutate the caller's. Each text attachment
    is wrapped in a ``--- name (mime, size) ---`` fence. Binary-only
    attachments get a one-line summary so the model at least knows they
    were sent (and can suggest the user paste the relevant bit).
    """
    if not attachments:
        return messages

    # Locate the last user message to attach to. Fall back to appending
    # a fresh one if there is none.
    out = list(messages)
    target_idx = next(
        (i for i in range(len(out) - 1, -1, -1) if out[i].get("role") == "user"),
        None,
    )

    blocks: list[str] = ["[Attachments]"]
    for att in attachments:
        size_kb = att.size_bytes / 1024
        header = f"--- {att.name} ({att.mime}, {size_kb:.1f} KB) ---"
        if att.content_text is not None:
            blocks.append(header)
            if len(att.content_text) > MAX_INLINE_TEXT_BYTES:
                # Send the head only; the rest still rides through to the
                # event log via the original Attachment object,
                # so durable copies are intact even when the LLM only sees
                # a snippet.
                head = att.content_text[:MAX_INLINE_TEXT_BYTES]
                truncated = len(att.content_text) - len(head)
                blocks.append(head)
                blocks.append(
                    f"[truncated — {truncated} more characters not shown to "
                    f"the model; full content is durably stored]"
                )
            else:
                blocks.append(att.content_text)
            blocks.append(f"--- end {att.name} ---")
        else:
            blocks.append(
                f"{header}\n[binary content omitted — {att.size_bytes} bytes]\n"
                f"--- end {att.name} ---"
            )
    folded = "\n".join(blocks)

    if target_idx is None:
        out.append({"role": "user", "content": folded})
    else:
        original = out[target_idx]["content"]
        out[target_idx] = {
            **out[target_idx],
            "content": f"{folded}\n\n{original}",
        }
    return out


def _validate_attachment_total(attachments: list[Attachment]) -> None:
    """Raise 413 if attachments collectively exceed the cap."""
    total = sum(
        (len(a.content_text) if a.content_text is not None else 0)
        + (len(a.content_base64) if a.content_base64 is not None else 0)
        for a in attachments
    )
    if total > MAX_ATTACHMENT_BYTES_TOTAL:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Attachments total {total} bytes exceeds limit of "
                f"{MAX_ATTACHMENT_BYTES_TOTAL} bytes."
            ),
        )


class AttachmentSummary(BaseModel):
    """A distilled summary of one attachment, returned to the client.

    The desktop appends each of these as an ``attachment_distilled`` event
    in its local event log so subsequent conversation turns naturally
    "remember" what the file was about, even when the file isn't attached
    again.
    """
    name: str
    mime: str
    size_bytes: int
    summary: str
    source: str  # 'text' / 'pdf' / 'binary-stub' / …
    sync_id: Optional[int] = None


class SideEffectEvent(BaseModel):
    """Event that the agent's tools wrote to the event log mid-turn —
    not a normal user_message / assistant_response, but something the
    chat surface needs to render inline. The wire field is retained
    after #91 so future tools that genuinely emit inline cards can use
    it without a protocol break; currently no shipped tool emits."""
    sync_id: int
    event_type: str
    content: str
    timestamp: str
    metadata: dict = {}


class LLMChatResponse(BaseModel):
    """LLM chat response."""
    role: str
    content: str
    model: str
    stop_reason: Optional[str] = None
    tool_calls_executed: list[str] = []
    attachment_summaries: list[AttachmentSummary] = []
    # Any side-effect events the agent's tools wrote during this turn.
    # After #91, no current tools emit these (workflow_run cards are
    # gone). The list ships empty but the field stays so future inline
    # tool surfaces can use it without a wire-format break.
    side_effect_events: list[SideEffectEvent] = []


# ─────────────────────────────────────────────────────────────────────
# Workflow rescue / hallucination detection was deleted in #91 along
# with the run_workflow tool itself. Workflows are now recipe-based
# (executed by the agent via delegate() calls), so there's no
# fire-and-forget tool semantic for the LLM to hallucinate.


def _build_uploads_memory_block(user_id: str) -> str:
    """Memory Fix A: list the user's recent uploaded files so the
    agent doesn't forget about them across sessions.

    Without this, you upload paper.pdf on Day 1, open a new chat on
    Day 2, and the agent has no idea the file exists — even though
    the bytes + extracted text are still cached server-side. With
    this block, the agent sees a "Recent files you've processed:"
    list and can call ``read_uploaded_file(name)`` to pull the full
    text on demand.

    Returns "" when there are no uploads.
    """
    try:
        from nexus_server import files as _files
        recent = _files.list_recent_files_for_prompt(user_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("uploads probe failed for %s: %s", user_id, e)
        return ""

    if not recent:
        return ""

    from nexus_server.files import format_size_hint as _fmt_size

    lines: list[str] = []
    for f in recent:
        ts = f["created_at"] or "?"
        size_hint = _fmt_size(f["size_bytes"])
        head = f"- {f['name']!r} ({f['mime']}, {size_hint}, uploaded {ts})"
        if f["has_text"] and f["snippet"]:
            head += f"\n    Excerpt: {f['snippet']}"
        lines.append(head)

    return (
        "[CONTEXT — FILES YOU'VE PROCESSED BEFORE]\n"
        "The user has uploaded these files in past sessions. They are "
        "still available — call `read_uploaded_file(name=…)` to pull "
        "the full text on demand. Do NOT ask the user to re-upload "
        "something that's already in this list.\n\n"
        + "\n".join(lines)
    )


# #97 round-2: phrases Gemini uses when it ad-libs "the workflow is
# running" instead of actually calling delegate(). Detected in the
# post-turn handler and rewritten to a useful failure message so the
# user knows the agent flaked instead of staring at fake prose.
_FAKE_WORKFLOW_NARRATION_PHRASES: tuple[str, ...] = (
    # Chinese
    "工作流正在", "工作流已启动", "正在后台运行", "后台运行",
    "最终输出将", "结果会作为单独", "完成后作为单独", "作为单独的消息",
    "已启动工作流", "我将启动", "我会运行", "正在为你",
    # English
    "the workflow is running", "the workflow is now running",
    "running in the background", "the pipeline is running",
    "i've started", "i have started the", "has been started",
    "i'll run the", "output will arrive", "result will be sent",
    "will be sent as a separate", "as a separate message",
)


# #173 — same hallucination class but for the defer_to_background
# tool. Agent paraphrases "I'll work on this and email you" without
# ever emitting the function_call → task never starts → user waits
# forever. Detected post-turn: if the reply contains any of these
# phrases AND no defer_to_background tool_call was executed this
# turn AND no row landed in async_tasks for this user just now, we
# rewrite the reply to flag the failure so the medic doesn't think
# work is happening when it isn't.
_FAKE_DEFER_NARRATION_PHRASES: tuple[str, ...] = (
    # Chinese
    "我会跑一下", "做完邮件通知", "做完邮件告诉",
    "完成后邮件", "完成后会通过邮件", "做完后邮件",
    "我会跑这个", "好的，我会跑", "好的，预计",
    "我会在后台", "邮件通知您", "邮件通知你",
    "我会处理这个", "做完后通知你",
    # English
    "i'll work on this", "i'll work on that", "i will work on this",
    "i'll email you when", "i will email you when",
    "email you when done", "email you the result",
    "i'll run it in the background", "running this in the background",
    "run this in the background", "run it in the background",
    "in the background and email", "background and notify",
    "i'll get back to you", "expect an email", "you'll get an email",
    "scheduled in the background", "scheduled this in the background",
)


def _looks_like_fake_defer_narration(reply: str) -> bool:
    """#173 — companion to _looks_like_fake_workflow_narration but
    for defer_to_background. Returns True if the reply reads like
    Gemini paraphrased a "I'll work on it + email later" promise
    without actually calling the tool.

    Caller is responsible for cross-checking that NO
    defer_to_background tool_call actually executed this turn
    (otherwise this returns true on legitimate confirmation text
    too — which is fine, but no rewrite needed). Cheap substring
    scan; false positives are tolerable because the rewrite text
    just adds a nudge, doesn't replace the reply."""
    if not reply:
        return False
    lc = reply.lower()
    return any(p.lower() in lc for p in _FAKE_DEFER_NARRATION_PHRASES)


def _looks_like_fake_workflow_narration(reply: str) -> bool:
    """Return True if ``reply`` reads like Gemini hallucinated a
    workflow status without calling delegate(). False positives are
    cheap — a real reply about Starknet, the weather, or movie reviews
    won't trip any of these phrases."""
    if not reply:
        return False
    lc = reply.lower()
    return any(p.lower() in lc for p in _FAKE_WORKFLOW_NARRATION_PHRASES)


def _maybe_rewrite_dicom_to_png(att: "Attachment") -> "Attachment":
    """#141 — detect a single-instance DICOM upload by magic bytes
    and rewrite the Attachment as a rendered PNG so the rest of the
    multimodal pipeline (image branch → caption distill → vision
    call) sees it as a normal medical image.

    Returns the original Attachment unchanged when:
      * No content_base64 (server-side resolution failed).
      * Magic bytes don't match "DICM" at offset 128.
      * pydicom isn't available (broken venv).
      * Rendering raises for any reason.

    .zip-shaped DICOM archives are NOT handled here — they need
    full extraction to disk + per-series rendering and produce
    multiple image_parts. That's the next layer (a separate path
    that runs before the per-attachment loop, planned for the next
    PR). For now zip + DICOM goes through normal "binary stub"
    distill — agent gets a message saying "DICOM archive uploaded,
    {N} bytes" without pixel access. Acceptable for v1 because the
    main clinical case we have (per the failing screenshot) is
    single-instance .dcm.
    """
    if not att.content_base64:
        return att

    import base64 as _b64
    try:
        raw = _b64.b64decode(att.content_base64)
    except Exception:  # noqa: BLE001
        return att

    # Magic-byte check — cheap. Skip everything else when this fails.
    try:
        from nexus_server.dicom import looks_like_dicom_bytes
    except ImportError:
        return att
    if not looks_like_dicom_bytes(raw):
        return att

    # Render single instance to PNG via pydicom + numpy + Pillow.
    # Wrapped broadly because PACS exports occasionally use unusual
    # transfer syntaxes / multi-frame layouts; we don't want a single
    # bad upload to kill the chat turn.
    try:
        import io as _io
        import pydicom
        import numpy as _np
        from PIL import Image as _Image
        from nexus_server.dicom import (
            _hu_array, _window_to_uint8, _resolve_window,
        )

        ds = pydicom.dcmread(_io.BytesIO(raw))
        modality = str(getattr(ds, "Modality", "") or "")
        # Pick a sensible window per modality. Lung is the most
        # common useful default for chest CT; for other modalities
        # we fall back to whatever the DICOM tags carry or min/max.
        body_part = str(getattr(ds, "BodyPartExamined", "") or "").upper()
        preset = "lung" if (modality == "CT" and "CHEST" in body_part) else "default"
        wl, ww = _resolve_window(ds, modality, preset)
        arr = _hu_array(ds)
        img8 = _window_to_uint8(arr, wl, ww)

        # Multi-frame DICOM (rare for diagnostic CT, common for US/
        # angio): _hu_array may give a 3-D array. Pick middle frame
        # for the inline render — a future iteration can render all
        # frames into a grid like we do for series.
        if img8.ndim == 3:
            img8 = img8[img8.shape[0] // 2]

        img = _Image.fromarray(img8, mode="L")
        buf = _io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        png_bytes = buf.getvalue()

        # Rewrite the attachment in place: same identity (name +
        # file_id stay so referenced_file_ids on the assistant
        # response still binds correctly) but content + mime now
        # look like a regular image upload. The caption distill
        # downstream will see the rendered slice — much more
        # useful than "DICOM archive uploaded".
        rendered_name = att.name
        # Tag the name to make it obvious in the UI / event log
        # that this was DICOM-derived, not a raw PNG.
        if not rendered_name.lower().endswith(".png"):
            rendered_name = f"{rendered_name}.dicom.png"
        return type(att)(
            name=rendered_name,
            mime="image/png",
            size_bytes=len(png_bytes),
            content_text=None,
            content_base64=_b64.b64encode(png_bytes).decode("ascii"),
            file_id=att.file_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "DICOM render failed for %s (%s); falling back to "
            "binary stub. Agent will say 'I couldn't read this' "
            "rather than crash.",
            att.name, type(e).__name__,
        )
        return att


def _maybe_rewrite_dicom_archive_to_pngs(
    att: "Attachment",
    disk_path: str,
    user_id: str,
    *,
    prerender_status: str = "",
    prerender_preview_dir: str = "",
) -> list["Attachment"]:
    """#148 — detect a DICOM zip archive and expand it into 3 rendered
    PNG attachments (MIP + middle slice + 4×4 grid) so the rest of the
    multimodal pipeline sees medical imaging instead of an unreadable
    binary blob.

    Returns ``[att]`` unchanged when:
      * The file isn't a zip (mime / extension hints fail).
      * The zip doesn't contain DICOM (looks_like_dicom_archive false).
      * Parsing / rendering raises for any reason (corrupt archive,
        unsupported transfer syntax, etc.) — we fall back rather than
        crash the chat turn.

    The original ``file_id`` is preserved on each output attachment so
    the assistant's ``referenced_file_ids`` (#128) still binds the
    feedback loop back to the original upload. Names are tagged
    (``.mip.png`` / ``.slice-N.png`` / ``.grid-4x4.png``) so memory
    chips show which view the agent was looking at.
    """
    name_lower = (att.name or "").lower()
    is_zip = (
        att.mime == "application/zip"
        or name_lower.endswith(".zip")
    )
    if not is_zip:
        return [att]

    # #152 — fast path: upload route already prerendered this archive
    # and stored MIP/slice/grid PNGs on disk. Load them directly,
    # bypassing parse + render. This eliminates the chat-time race
    # the medic was hitting ("agent says empty right after upload").
    if prerender_status == "rendered" and prerender_preview_dir:
        try:
            from nexus_server.dicom import load_prerendered_previews
            previews = load_prerendered_previews(prerender_preview_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "DICOM-rewrite: prerender load_previews failed for %s: "
                "%s — falling through to inline render", att.name, e,
            )
            previews = []
        if previews:
            import base64 as _b64
            result: list[Attachment] = []

            # #162 — patient context block: looked up by study_id
            # derived from the file_id of the upload. Goes FIRST as
            # a text attachment so it appears at the top of the
            # folded user message body, before the rendered PNGs
            # are introduced. The agent then reads patient identity
            # + study timeline BEFORE looking at the images.
            try:
                from nexus_server.dicom import (
                    find_study_by_upload, get_patient_context_block,
                )
                bound = find_study_by_upload(user_id, att.file_id or "")
                if bound is not None:
                    study_id, _extract_dir = bound
                    ctx = get_patient_context_block(user_id, study_id)
                    if ctx:
                        result.append(type(att)(
                            name=f"{att.name}.patient-context.txt",
                            mime="text/plain",
                            size_bytes=len(ctx.encode("utf-8")),
                            content_text=ctx,
                            content_base64=None,
                            file_id=att.file_id,
                        ))
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "DICOM-rewrite: patient context lookup failed for "
                    "%s: %s — proceeding without context block",
                    att.name, e,
                )

            for label, png in previews:
                result.append(type(att)(
                    name=f"{att.name}.{label}.png",
                    mime="image/png",
                    size_bytes=len(png),
                    content_text=None,
                    content_base64=_b64.b64encode(png).decode("ascii"),
                    file_id=att.file_id,
                ))
            logger.info(
                "DICOM-rewrite: %s served from prerender cache "
                "(%d previews + %d context blocks)",
                att.name, sum(1 for r in result if r.mime != "text/plain"),
                sum(1 for r in result if r.mime == "text/plain"),
            )
            return result
        # Fall through if previews couldn't be loaded — better to
        # try again now than to return [att] with no medical context.

    # #178 — in-flight prerender. The medic just uploaded the zip
    # and dropped a chat turn before the background prerender finished
    # writing previews to disk. Without this branch the code falls
    # through to the synchronous parse path, races with the background
    # writer mid-extract, and emits "empty attachment" downstream —
    # which is what the LLM then paraphrases as "PET-CT.zip 似乎是
    # 空的". Tell the agent EXPLICITLY: "still rendering, don't say
    # empty, ask the medic to wait ~30-60s." Idempotent: if prerender
    # finishes between this check and the next turn, the "rendered"
    # fast path above takes over.
    #
    # Gate this on "looks like DICOM" so non-DICOM zips (code archives,
    # backups) don't get hallucinated as "DICOM uploaded". We do a
    # cheap magic-byte peek; if the archive is non-DICOM we fall
    # through to the normal path which will return [att] unchanged
    # for non-medical zips.
    if prerender_status in ("prerendering", "queued", "pending", ""):
        # Cheap DICOM-or-medical-named check so we only emit the
        # medical in-flight stub for files that ARE DICOM.
        looks_dicom_here = False
        try:
            from nexus_server.dicom import looks_like_dicom_archive
            from pathlib import Path as _Path
            if disk_path and _Path(disk_path).exists():
                looks_dicom_here = looks_like_dicom_archive(_Path(disk_path))
        except Exception as exc:
            logger.debug("DICOM archive check failed: %s", exc)
        medical_name_hints = (
            "dicom", "dcm", "ct", "mr", "mri", "pet", "xr",
            "ultrasound", "us-", "study", "scan", "imag",
        )
        looks_medical = looks_dicom_here or any(
            h in name_lower for h in medical_name_hints
        )
        if looks_medical:
            size_mb = max(1, att.size_bytes // (1024 * 1024))
            # Best-effort progress sniff so the assistant message can
            # quote a meaningful progress hint rather than a flat wait.
            progress_hint = ""
            try:
                from nexus_server.dicom import _get_prerender_progress
                p = _get_prerender_progress(att.file_id or "")
                if p:
                    stage = p.get("stage", "") or ""
                    done = int(p.get("current", 0) or 0)
                    total = int(p.get("total", 0) or 0)
                    if total > 0 and done >= 0:
                        pct = max(0, min(100, int(100 * done / total)))
                        progress_hint = (
                            f" Current progress: {pct}% "
                            f"({done}/{total} {stage or 'slices'})."
                        )
                    elif stage:
                        progress_hint = f" Current stage: {stage}."
            except Exception as exc:
                logger.debug("reading prerender progress failed: %s", exc)
            return [type(att)(
                name=att.name,
                mime=att.mime,
                size_bytes=att.size_bytes,
                content_text=(
                    f"The medic uploaded a {size_mb} MB DICOM imaging "
                    f"archive named {att.name!r}. The server has "
                    f"ACCEPTED the file and the background DICOM "
                    f"prerender is CURRENTLY RUNNING (extracting "
                    f"series, computing MIP + middle-slice + 4×4 "
                    f"grid previews).{progress_hint}\n\n"
                    f"CRITICAL: DO NOT TELL THE MEDIC THE FILE IS "
                    f"EMPTY. The file is fine — preview rendering "
                    f"just hasn't finished yet (PET-CT studies with "
                    f"500+ slices typically take 20-60 seconds on "
                    f"first ingest).\n\n"
                    f"Tell the medic in their language: 'Got it — "
                    f"your DICOM study uploaded ({size_mb} MB) and "
                    f"I am rendering previews in the background. "
                    f"Give me about 30-60 seconds, then ask me "
                    f"again or say \"看一下\". You can also open "
                    f"the dedicated DICOM viewer now to scroll "
                    f"through slices while I finish rendering.'"
                ),
                content_base64=None,
                file_id=att.file_id,
            )]
        # Non-medical zip mid-prerender: fall through. The synchronous
        # path below will handle it cleanly (typically returning the
        # raw zip att unchanged, since looks_like_dicom_archive will
        # also be false there).

    # #152 — surface upload-time render failures to the LLM as a
    # structured stub instead of letting them silently bottom out
    # in distill_attachment ("[empty]"). The medic uploaded a DICOM
    # archive that the prerender path detected but couldn't render
    # (vendor-specific layout, codec issue, etc.). Tell the agent
    # explicitly so it apologises usefully + points the medic at
    # the dedicated viewer.
    if prerender_status == "render_failed":
        size_mb = max(1, att.size_bytes // (1024 * 1024))
        return [type(att)(
            name=att.name,
            mime=att.mime,
            size_bytes=att.size_bytes,
            content_text=(
                f"The medic uploaded a {size_mb} MB DICOM imaging "
                f"archive named {att.name!r}. The server detected it "
                f"as DICOM and ingested the bytes (DO NOT TELL THE "
                f"MEDIC THE FILE IS EMPTY) but the automatic preview "
                f"renderer hit a compatibility issue at upload time "
                f"(typically a compressed transfer syntax or multi-"
                f"frame layout). Tell the medic: 'Your DICOM study "
                f"uploaded successfully ({size_mb} MB). I couldn't "
                f"auto-render previews due to a codec issue. Please "
                f"open the study in the in-app DICOM viewer, mark a "
                f"key slice, and click \"Send to agent\" — I'll then "
                f"see the actual image and can analyse it.'"
            ),
            content_base64=None,
            file_id=att.file_id,
        )]

    if not disk_path:
        logger.warning(
            "DICOM-rewrite: %s looks like zip but no disk_path — "
            "skipping (file_id=%s)",
            att.name, att.file_id,
        )
        return [att]
    # INFO-level: ALWAYS log when we enter the DICOM rewrite path so
    # the server log unambiguously shows what happened to medic uploads.
    logger.info(
        "DICOM-rewrite: probing %s (%d bytes) at %s (prerender_status=%r)",
        att.name, att.size_bytes, disk_path, prerender_status,
    )

    from pathlib import Path as _Path
    zip_path = _Path(disk_path)
    if not zip_path.exists():
        logger.warning(
            "dicom rewrite: disk_path %s does not exist for %s",
            disk_path, att.name,
        )
        return [att]

    try:
        from nexus_server.dicom import (
            looks_like_dicom_archive, parse_dicom_archive,
            render_mip_png, render_slice_png, render_grid_png,
            persist_study,
        )
    except ImportError:
        return [att]

    # Medical-archive name heuristics — if the filename hints medical
    # imaging (PET-CT, MR, DICOM, etc.) but our magic-byte detector
    # missed it, we still emit an informative stub instead of letting
    # distill_attachment fall back to "[empty attachment]".
    medical_name_hints = (
        "dicom", "dcm", "ct", "mr", "mri", "pet", "xr", "ultrasound",
        "us-", "study", "scan", "imag",
    )
    name_looks_medical = any(h in name_lower for h in medical_name_hints)

    if not looks_like_dicom_archive(zip_path):
        if name_looks_medical and att.size_bytes > 10 * 1024 * 1024:
            # Name screams medical + big file but the zip didn't have
            # DICM magic anywhere we probed. Could be a structured
            # report archive, an unusual transfer syntax, or a
            # mislabelled archive. Emit a useful stub instead of
            # letting it become "empty file" downstream.
            size_mb = att.size_bytes // (1024 * 1024)
            logger.warning(
                "DICOM detector failed on medical-named archive %s "
                "(%d MB) — emitting informative stub instead of empty.",
                att.name, size_mb,
            )
            stub = type(att)(
                name=att.name,
                mime=att.mime,
                size_bytes=att.size_bytes,
                content_text=(
                    f"The medic uploaded a {size_mb} MB DICOM medical "
                    f"imaging archive named {att.name!r}. The full study "
                    f"data is stored and available — DO NOT TELL THE MEDIC "
                    f"THE FILE IS EMPTY. Automatic preview rendering "
                    f"could not be generated for this particular archive "
                    f"(likely vendor-specific layout or unusual "
                    f"compression). Tell the medic: 'I see your DICOM "
                    f"study uploaded ({size_mb} MB). I couldn't auto-"
                    f"generate previews — please open it in the DICOM "
                    f"viewer in chat, mark a key slice, and click "
                    f"\"Send to agent\" — I'll then see the actual "
                    f"image and can analyse it.'"
                ),
                content_base64=None,
                file_id=att.file_id,
            )
            return [stub]
        return [att]

    import tempfile
    import shutil
    import base64 as _b64
    tmp = _Path(tempfile.mkdtemp(prefix="nexus-dicom-render-"))
    try:
        study = parse_dicom_archive(zip_path, tmp)
        if not study.series:
            return [att]
        # Pick the largest series (most slices = most clinical content).
        # Multi-series studies still produce one set of 3 PNGs per
        # chat turn; the medic uses the dedicated viewer to see other
        # series interactively.
        series = max(study.series, key=lambda s: s.slice_count)
        modality = (study.modality or "").upper()
        body = (series.body_part or "").upper()
        preset = "lung" if (modality == "CT" and "CHEST" in body) else "default"

        # #148 — persist the study to dicom_studies so the medic can
        # open it later in the dedicated viewer (#143) by study_id.
        # Without this, every chat turn would re-parse from disk.
        try:
            study_id = persist_study(
                user_id, att.file_id or "", study, tmp,
            )
            logger.info(
                "DICOM archive %s persisted as study_id=%s "
                "(%d series, %d total instances)",
                att.name, study_id[:8], len(study.series),
                study.total_instances,
            )
            # Don't delete tmp dir — persist_study points
            # extract_dir at it. Caller MUST NOT clean up.
            cleanup = False
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "persist_study failed for %s: %s — viewer won't have "
                "this study but chat still gets the 3 PNGs.",
                att.name, e,
            )
            cleanup = True

        mip = render_mip_png(series, preset=preset)
        middle_idx = max(0, series.slice_count // 2)
        mid = render_slice_png(series, middle_idx, preset=preset)
        grid = render_grid_png(series, rows=4, cols=4)

        def _png_to_att(png: bytes, label: str) -> "Attachment":
            return type(att)(
                name=f"{att.name}.{label}.png",
                mime="image/png",
                size_bytes=len(png),
                content_text=None,
                content_base64=_b64.b64encode(png).decode("ascii"),
                file_id=att.file_id,
            )

        result = [
            _png_to_att(mip,  "mip"),
            _png_to_att(mid,  f"slice-{middle_idx}"),
            _png_to_att(grid, "grid-4x4"),
        ]
        return result
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "DICOM archive render failed for %s: %s — emitting "
            "informative stub.", att.name, e,
        )
        cleanup = True
        size_mb = att.size_bytes // (1024 * 1024)
        # Don't let distill_attachment turn this into "[empty attachment]".
        # Give the agent something actionable so it can ask the medic to
        # open the dedicated viewer.
        stub = type(att)(
            name=att.name,
            mime=att.mime,
            size_bytes=att.size_bytes,
            content_text=(
                f"The medic uploaded a {size_mb} MB DICOM imaging "
                f"archive named {att.name!r}. The full study data is "
                f"stored — DO NOT TELL THE MEDIC THE FILE IS EMPTY. "
                f"We recognised it as DICOM but the renderer hit a "
                f"compatibility issue ({type(e).__name__}) — often a "
                f"compressed transfer syntax that needs extra codecs, "
                f"or multi-frame layout. Tell the medic: 'Your DICOM "
                f"study uploaded successfully ({size_mb} MB). Server-"
                f"side auto-preview ran into a codec issue. Please "
                f"open the study in the DICOM viewer and click "
                f"\"Send to agent\" on a key slice — I will analyse "
                f"the actual image then.'"
            ),
            content_base64=None,
            file_id=att.file_id,
        )
        return [stub]
    finally:
        # When persist_study succeeded the tmp dir IS the extract_dir
        # — keep it. When it failed (cleanup=True) we own the dir.
        if locals().get("cleanup", True):
            shutil.rmtree(tmp, ignore_errors=True)


async def _build_related_context_block(
    user_id: str,
    bare_text: str,
    image_captions: list[str] | None = None,
) -> str:
    """#138 — pull semantically related prior chunks and format them
    as a context block injected into the LLM prompt.

    Search query:
      * Always the user's bare text (what they just typed).
      * If image captions were produced this turn (#128), append them
        — this lets ``vision_search`` find prior images of the same
        domain even when the user typed nothing specific (e.g. they
        just paste a CT and say "看一下").

    Returns "" silently when:
      * The vector backend is missing / down.
      * No hits.
      * Embedding API failed.

    Cost: one embedding call per chat turn (≈ ¥0.001). Skipped when
    bare_text + captions are both empty.
    """
    parts: list[str] = []
    if bare_text:
        parts.append(bare_text)
    for c in image_captions or []:
        if c:
            parts.append(c)
    query = "\n\n".join(parts).strip()
    if not query:
        return ""

    try:
        from nexus_server.vector_index import (
            search_chunks, EmbeddingUnavailable,
        )
    except ImportError:
        return ""

    try:
        hits = await search_chunks(user_id=user_id, query=query, k=5)
    except EmbeddingUnavailable as e:
        logger.debug(
            "RAG grounding skipped — embedding backend unavailable: %s", e,
        )
        return ""
    except Exception as e:  # noqa: BLE001
        logger.debug("RAG grounding search failed: %s", e)
        return ""

    if not hits:
        return ""

    # Dedupe: same source_id within the same kind only shows once.
    # Vector search can return multiple chunks from the same original
    # source (e.g. a long PDF split into 3 chunks), and emitting all
    # of them just bloats context. Keep the closest one per source.
    seen: set[tuple[str, str]] = set()
    deduped = []
    for h in hits:
        key = (h.source_kind, h.source_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(h)

    # Format: one line per hit, distance prefix so the agent can tell
    # confident matches from soft ones. Trim each text to ~200 chars
    # so the block doesn't blow out the context window.
    lines = ["[CONTEXT — RELATED PRIOR INTERACTIONS]"]
    lines.append(
        "These are the most semantically similar past chunks from "
        "your interactions with this user. Treat them as background "
        "memory; cite them when relevant, but don't list them back to "
        "the user mechanically."
    )
    lines.append("")
    for h in deduped[:5]:
        snippet = (h.text_chunk or "").strip().replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:240] + "…"
        lines.append(
            f"- [{h.source_kind}:{h.source_id}, sim={1 - h.distance:.2f}] "
            f"{snippet}"
        )
    return "\n".join(lines)


def _build_email_capability_block() -> str:
    """#118: nudge the agent to PROACTIVELY use email + calendar
    tools in clinical / professional chat contexts.

    Without this nudge, Gemini sees the tools in the function-call
    schema and will use them WHEN ASKED — but won't proactively
    suggest "I can email Dr Smith for you?" when the user says
    "let Dr Smith know the patient's scan is clear". This block
    surfaces three things:

      1. Capability awareness — the agent knows what's available
         on macOS (calendar + draft + direct send)
      2. Proactive trigger phrases — common user intents that
         imply email
      3. Hard confirmation rule for send_email_now (irreversible)

    Returns "" when no email backend is configured so the block
    doesn't pollute context with capabilities that don't work.
    """
    import os as _os
    relay_configured = bool(
        _os.environ.get("NEXUS_RELAY_URL", "").strip()
        and _os.environ.get("NEXUS_RELAY_API_KEY", "").strip()
    )
    smtp_configured = bool(
        _os.environ.get("NEXUS_SMTP_HOST", "").strip()
        and _os.environ.get("NEXUS_SMTP_USER", "").strip()
        and _os.environ.get("NEXUS_SMTP_PASSWORD", "").strip()
    )
    if not (relay_configured or smtp_configured):
        return ""

    backend_note = (
        "Sends go through the hosted Nexus relay with per-user "
        "rate limiting and recipient allow-list — failures bubble "
        "up as clear errors you should pass to the user verbatim."
        if relay_configured else
        "Sends go directly via SMTP (DEV MODE) — no rate limit "
        "or allow-list. Be extra cautious with confirmation."
    )

    return (
        "[EMAIL + CALENDAR CAPABILITY]\n"
        "Three tools you can use on macOS:\n"
        "  • `read_calendar` — read the user's local Calendar.app "
        "events (default: next 7 days).\n"
        "  • `send_email_now` — send directly through the relay. "
        "IRREVERSIBLE. Use ONLY after explicit user confirmation.\n"
        "  • `compose_email_draft` — open the user's default mail "
        "client with a pre-filled draft. RARELY USED — only when the "
        "user EXPLICITLY says they want to edit in their mail app "
        "(e.g. '我要在邮件 app 里改一下再发', 'open it in Mail', "
        "'I'll finish writing it myself'). Default is to NOT use this.\n"
        "\n"
        "═══ LANGUAGE-MIRRORING RULE ═══\n"
        "Whatever language the user wrote their message in, you reply "
        "in. If they wrote Chinese, reply Chinese. English, English. "
        "Mixed Chinese+English, mirror their mix. Apply this rule to "
        "BOTH (a) your prose around the draft AND (b) the email "
        "Subject/Body itself (unless they explicitly tell you the "
        "email language e.g. '用英文写').\n"
        "\n"
        "═══ EMAIL FIRST-TURN PATTERN (memorise this) ═══\n"
        "When the user asks you to draft / write / send an email — "
        "triggers in any language, including:\n"
        "  • '帮我 draft / 起草 / 草拟 / 写 / 发 一封邮件'\n"
        "  • '给 <人> 发邮件 / 通知 / 告诉'\n"
        "  • 'draft / write / send an email to <person>'\n"
        "  • 'tell <person> ...' / 'let <person> know ...'\n"
        "  • 'follow up with <person>'\n"
        "  • 'send the report / summary / note to <person>'\n"
        "\n"
        "...your VERY FIRST REPLY must contain ALL THREE of:\n"
        "  (1) the proposed email body inline in your chat reply "
        "(To, Subject, Body — Body is the actual full text, not a "
        "summary).\n"
        "  (2) a 'send this now?' question, in the USER'S LANGUAGE "
        "('现在发吗？' for Chinese, 'Send this now?' for English).\n"
        "  (3) NOTHING ELSE — no tool call, no 'I've drafted it', no "
        "'check your mail client'.\n"
        "\n"
        "DO NOT call `compose_email_draft` unless the user explicitly "
        "asked for mail-client editing.\n"
        "DO NOT call `send_email_now` on the first turn — wait for "
        "the user's explicit confirm ('发' / 'send' / 'yes' / "
        "'confirm' / 'ok' / '确认').\n"
        "\n"
        "═══ EXAMPLES (notice language mirroring) ═══\n"
        "\n"
        "── EN example ──\n"
        "USER: 'Draft an email to alice@x.com saying hi, you decide "
        "the content.'\n"
        "\n"
        "✗ WRONG:\n"
        "  'I've drafted the email — please review in your mail "
        "client.' (also wrong: calling compose_email_draft.)\n"
        "\n"
        "✓ RIGHT (English mirror):\n"
        "  Here's the draft:\n"
        "\n"
        "  To: alice@x.com\n"
        "  Subject: Hello\n"
        "  Body:\n"
        "  Hi Alice,\n"
        "\n"
        "  Hope you've been well — wanted to check in and see how "
        "things are going.\n"
        "\n"
        "  Best,\n"
        "  Jimmy\n"
        "\n"
        "  Send this now?\n"
        "\n"
        "── ZH example ──\n"
        "USER: '帮我 draft 一封邮件，发给 alice@x.com，主题问好，"
        "内容你自己发挥'\n"
        "\n"
        "✓ RIGHT (Chinese mirror):\n"
        "  好的，草稿如下：\n"
        "\n"
        "  收件人: alice@x.com\n"
        "  主题: 问好\n"
        "  正文:\n"
        "  Alice 你好，\n"
        "\n"
        "  好久没联系，想跟你打个招呼看看最近怎么样。\n"
        "\n"
        "  Jimmy\n"
        "\n"
        "  现在发吗？\n"
        "\n"
        "── Confirmation turn (either language) ──\n"
        "USER: '发' / 'send' / 'yes' / '确认' / 'ok'\n"
        "→ NOW call `send_email_now(to=..., subject=..., body=...)`\n"
        "→ Reply briefly in the user's language: "
        "'✓ Sent.' / '✓ 已发送。'\n"
        "\n"
        "── Revision turn ──\n"
        "USER: 'change it, add the meeting next week' / '改一下，"
        "加上下周开会的事'\n"
        "→ Generate a NEW draft inline (same shape) and re-ask.\n"
        "\n"
        "═══ CONFIRMATION RULE (HARD) ═══\n"
        "`send_email_now` is irreversible. Calling it without the "
        "user confirming AFTER seeing the full Body is a BUG. "
        + backend_note
    )


def _build_workflow_recipes_block(user_id: str) -> str:
    """#91: For each installed workflow, render a "recipe" the main
    agent can execute by chaining ``delegate(skill, task)`` calls.

    This replaces the old fire-and-forget ``run_workflow`` tool. The
    agent reads the recipe inline in its context and traverses each
    step itself, which means:

      * No "background promise" semantic to hallucinate (#74/#77/#90).
      * Each step is a visible tool call in the chat, not a hidden
        async task. The user sees real progress.
      * The agent can adapt mid-flow — drop a step, ask the user a
        question, or escalate to its own tools (web_search, etc.) if
        a step needs capabilities ``delegate()`` doesn't give.

    Returns "" when no workflows are installed (the agent falls back
    to its general skills + simple tools).
    """
    try:
        from nexus_server import workflows as _wf
        installed = _wf.list_workflows(user_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("recipes probe failed for %s: %s", user_id, e)
        return ""

    if not installed:
        return ""

    blocks: list[str] = []
    for wf in installed:
        # Required-input list, e.g. "topic*, audience*, platform*"
        if wf.definition.inputs:
            in_parts: list[str] = []
            for spec in wf.definition.inputs:
                marker = "*" if spec.required else ""
                if spec.type == "select" and spec.options:
                    opts = " | ".join(spec.options)
                    in_parts.append(f"{spec.key}{marker} ({opts})")
                else:
                    in_parts.append(f"{spec.key}{marker}")
            inputs_line = "  Inputs to collect from user: " + ", ".join(in_parts)
        else:
            inputs_line = "  Inputs: (none)"

        # Steps rendered as delegate() calls. Each line is one step.
        # #106 D-3: if a step has a verifier, append a verifier line
        # right after it so the orchestrating agent runs the
        # check-and-retry loop inline.
        step_lines: list[str] = []
        for idx, step in enumerate(wf.definition.steps, start=1):
            label = step.label or step.skill
            step_lines.append(
                f"    {idx}. delegate(skill_name=\"{step.skill}\", "
                f"task=<{label} task derived from inputs + prior step output>)"
            )
            if step.verifier is not None:
                v = step.verifier
                criteria_blurb = (
                    f" Acceptance criteria: {v.criteria}"
                    if v.criteria else ""
                )
                step_lines.append(
                    f"       ↳ VERIFY: delegate(skill_name=\"{v.skill}\", "
                    f"task=\"Check the prior step's output.{criteria_blurb} "
                    f"Return JSON {{pass: bool, issues: [str], "
                    f"suggestions: [str]}}.\"). If pass=false AND retries "
                    f"used < {v.max_retries}, re-run step {idx} with the "
                    f"verifier's suggestions injected into the task."
                )
        steps_block = "\n".join(step_lines)

        # Gatekeeper note (v2.1 iterative packs)
        gk_note = ""
        if wf.definition.mode == "iterative" and wf.definition.gatekeeper:
            gk_note = (
                f"\n  After all steps, call delegate(skill_name="
                f"\"{wf.definition.gatekeeper.skill}\", task=<gatekeeper "
                f"prompt>). If its JSON verdict says pass=false and "
                f"max_iterations not yet reached, re-run the recipe with "
                f"its remaining_issues injected. Max "
                f"{wf.definition.max_iterations} iterations."
            )

        desc = (wf.description or "").strip().splitlines()[0] if wf.description else ""
        blocks.append(
            f"▸ {wf.name}\n"
            f"  Purpose: {desc}\n"
            f"{inputs_line}\n"
            f"  Recipe (call delegate() for each step IN ORDER, "
            f"feeding prior step's output forward):\n"
            f"{steps_block}"
            f"{gk_note}\n"
            f"  After the last step, return the final output to the user."
        )

    return (
        "[WORKFLOW RECIPES — execute by chaining delegate() calls]\n"
        "If the user's request matches one of the workflows below, "
        "execute its recipe by calling delegate(skill_name, task) for "
        "each step IN ORDER. Pass the previous step's output forward "
        "as part of the next step's task string.\n"
        "\n"
        "CRITICAL — DO NOT STOP MID-RECIPE\n"
        "  A workflow is N steps and ONLY a complete N-step run "
        "produces a usable deliverable. Stopping at step 2 of 5 "
        "wastes everything (researcher's data without writer/editor "
        "is just raw notes). After EACH delegate() returns, your VERY "
        "NEXT action MUST be either:\n"
        "    (a) call delegate() for the next step in the recipe, OR\n"
        "    (b) IF this was the LAST step, write the final deliverable "
        "as your text reply.\n"
        "  Do NOT 'check in', 'pause', 'see how things are going', or "
        "produce a partial summary mid-recipe. The user is waiting for "
        "the final piece, not a status update.\n"
        "\n"
        "Other rules:\n"
        "  1. DO NOT announce 'I'll run X workflow' as plain text — "
        "JUST start calling delegate. Each delegate call shows up as "
        "a tool card in chat, which IS the progress UI.\n"
        "  2. After the LAST delegate() returns, you MUST write the "
        "final deliverable as your TEXT REPLY. Copy the last sub-agent's "
        "output verbatim, or lightly restructure it for chat readability. "
        "Returning an empty text reply at the end is a BUG — the user "
        "will see a blank bubble. The tool result is NOT visible as the "
        "assistant message; you have to surface it yourself.\n"
        "  3. If a delegate() call fails (skill not found, sub-agent "
        "errored), STOP the recipe and explain the failure to the user "
        "in plain text. Don't keep calling delegate() against a missing "
        "skill — that just wastes rounds.\n"
        "  4. If no recipe matches the user's request, handle it with "
        "your own knowledge + simple tools (web_search, read_url). "
        "Don't force a workflow that doesn't fit.\n"
        "  5. VERIFIER LOOPS (D-3): when a step has a ↳ VERIFY line, "
        "after the step's main delegate() returns, run the verifier "
        "delegate(). Parse its JSON verdict. If pass=true, proceed to "
        "the next step. If pass=false AND retries-used < max_retries, "
        "re-run the same step with the verifier's suggestions appended "
        "to the task string (e.g. 'Address these issues from review: "
        "...'). If retries exhausted, accept the last output and move "
        "on — note the unresolved issues briefly in your final reply.\n\n"
        + "\n\n".join(blocks)
    )


# ───────────────────────────────────────────────────────────────────────────
# Tool Definitions & Execution
# ───────────────────────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "web_search",
        "description": "Search the web for current information. Use when the user asks about recent events, facts, or anything that requires up-to-date knowledge.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_url",
        "description": "Read and extract content from a URL. Use when the user provides a link or you need to fetch a specific web page.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to read"},
            },
            "required": ["url"],
        },
    },
]


async def execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool and return the result as text.

    Used only by the legacy USE_TWIN=0 direct-LLM path (test-only).
    Production goes through the twin's own ToolRegistry — see
    ``twin_manager.register_workflow_tools`` and friends.
    """
    if name == "web_search":
        return await _web_search(arguments.get("query", ""))
    elif name == "read_url":
        return await _read_url(arguments.get("url", ""))
    else:
        return f"Unknown tool: {name}"


async def _web_search(query: str) -> str:
    """Execute web search via Tavily API."""
    if not config.TAVILY_API_KEY:
        return "Web search unavailable: TAVILY_API_KEY not configured on server."

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": config.TAVILY_API_KEY,
                    "query": query,
                    "max_results": 5,
                    "include_answer": True,
                },
            )
            data = resp.json()
            # Build a concise result
            parts = []
            if data.get("answer"):
                parts.append(f"Answer: {data['answer']}")
            for r in data.get("results", [])[:5]:
                parts.append(f"- {r.get('title', '')}: {r.get('content', '')[:200]}")
                parts.append(f"  URL: {r.get('url', '')}")
            return "\n".join(parts) if parts else "No results found."
    except Exception as e:
        logger.warning("Web search failed: %s", e)
        return f"Web search error: {e}"


async def _read_url(url: str) -> str:
    """Read URL content via Jina Reader API."""
    if not config.JINA_API_KEY:
        # Fallback: direct fetch
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, follow_redirects=True)
                return resp.text[:5000]
        except Exception as e:
            return f"URL read error: {e}"

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://r.jina.ai/{url}",
                headers={"Authorization": f"Bearer {config.JINA_API_KEY}"},
            )
            return resp.text[:5000]
    except Exception as e:
        logger.warning("URL read failed: %s", e)
        return f"URL read error: {e}"


# ───────────────────────────────────────────────────────────────────────────
# LLM Calls (with tool support)
# ───────────────────────────────────────────────────────────────────────────


async def call_llm(
    messages: list[dict],
    system_prompt: Optional[str],
    model: Optional[str],
    temperature: Optional[float],
    max_tokens: Optional[int],
    tools: Optional[list[dict]] = None,
) -> tuple[str, str, str, list[dict]]:
    """Call LLM provider. Returns (content, model, stop_reason, tool_calls).

    #103: also handles MAX_TOKENS auto-continuation. When the provider
    returns text ending with the truncation marker (and no tool calls),
    we re-issue the call with a "continue from where you left off"
    nudge and stitch the chunks together. Bounded by
    ``MAX_AUTO_CONTINUATIONS`` to keep token cost predictable.
    """
    from nexus_core.llm.client import (
        MAX_AUTO_CONTINUATIONS, _TRUNCATION_MARKER, _CONTINUATION_NUDGE,
    )

    model = model or config.DEFAULT_LLM_MODEL
    provider = config.DEFAULT_LLM_PROVIDER

    async def _dispatch(msgs):
        if provider == "gemini":
            return await call_gemini(msgs, system_prompt, model, temperature, max_tokens, tools)
        elif provider == "openai":
            return await call_openai(msgs, system_prompt, model, temperature, max_tokens, tools)
        elif provider == "anthropic":
            return await call_anthropic(msgs, system_prompt, model, temperature, max_tokens, tools)
        elif provider == "kimi":
            return await call_kimi(msgs, system_prompt, model, temperature, max_tokens, tools)
        else:
            raise ValueError(f"Unknown LLM provider: {provider}")

    content, model_used, stop_reason, tool_calls = await _dispatch(messages)

    # If the call produced tool_calls, the text is intermediate "thinking"
    # before a function call — not user-facing prose, so no need to
    # continue. Same for clean stops.
    if tool_calls or not content.endswith(_TRUNCATION_MARKER):
        return content, model_used, stop_reason, tool_calls

    # Strip the marker before stitching; we'll re-append at the end
    # only if we still couldn't get a clean finish.
    stitched = content[: -len(_TRUNCATION_MARKER)]
    working = list(messages)
    for attempt in range(MAX_AUTO_CONTINUATIONS):
        logger.warning(
            "call_llm: delegate response truncated (attempt %d, chars=%d) — "
            "auto-continuing",
            attempt, len(stitched),
        )
        working = working + [
            {"role": "assistant", "content": stitched},
            {"role": "user", "content": _CONTINUATION_NUDGE},
        ]
        try:
            chunk, _m, _sr, chunk_tools = await _dispatch(working)
        except Exception as e:  # noqa: BLE001
            logger.warning("call_llm auto-continue failed: %s", e)
            return stitched + _TRUNCATION_MARKER, model_used, stop_reason, tool_calls
        # If the continuation produced tool_calls, accept the chunk's
        # leading text and stop — tool flow takes over.
        if chunk_tools:
            stitched += (chunk or "").lstrip()
            return stitched, model_used, stop_reason, chunk_tools
        clean_chunk = (chunk or "").lstrip()
        if not clean_chunk:
            return stitched + _TRUNCATION_MARKER, model_used, stop_reason, tool_calls
        # Strip any trailing marker the inner call appended — we'll
        # re-append only if we still need to bail.
        if clean_chunk.endswith(_TRUNCATION_MARKER):
            stitched += clean_chunk[: -len(_TRUNCATION_MARKER)]
            continue  # another round, if budget remains
        stitched += clean_chunk
        logger.info(
            "call_llm: auto-continue completed after %d round(s), "
            "total %d chars stitched.", attempt + 1, len(stitched),
        )
        return stitched, model_used, stop_reason, tool_calls
    # Exhausted budget; surface the marker.
    return stitched + _TRUNCATION_MARKER, model_used, stop_reason, tool_calls


async def call_gemini(messages, system_prompt, model, temperature, max_tokens, tools):
    """Call Google Gemini API with tool support.

    Critical: this used to *build* a tool config + gen_config and then
    silently drop them — Gemini never saw the function declarations,
    so the agent permanently answered "I can't search the internet"
    even though TAVILY_API_KEY and the web_search tool were configured.
    The fix below threads tools / temperature / max_tokens through to
    google-genai's `config=` argument and parses function_call parts
    out of the response so the outer tool loop can execute them.
    """
    try:
        from google import genai
    except ImportError:
        raise ValueError("google-genai not installed. Install with: pip install google-genai")

    if not config.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not configured")

    client = genai.Client(api_key=config.GEMINI_API_KEY)

    # Build the unified config the way google-genai expects.
    gen_config: dict = {
        "system_instruction": system_prompt or "You are a helpful assistant.",
    }
    if temperature is not None:
        gen_config["temperature"] = temperature
    if max_tokens is not None:
        gen_config["max_output_tokens"] = max_tokens

    # Tool declarations — the dict shape google-genai accepts for
    # function calling. The function_calling_config "AUTO" lets Gemini
    # decide on its own when to invoke a tool vs. answer directly.
    if tools:
        gen_config["tools"] = [{
            "function_declarations": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                }
                for t in tools
            ]
        }]
        gen_config["tool_config"] = {
            "function_calling_config": {"mode": "AUTO"}
        }

    try:
        import asyncio
        # google-genai's generate_content is sync — run in thread.
        def _call():
            return client.models.generate_content(
                model=model,
                contents=[
                    {
                        "role": "user" if m["role"] == "user" else "model",
                        "parts": [{"text": m["content"]}],
                    }
                    for m in messages
                ],
                config=gen_config,
            )

        response = await asyncio.get_event_loop().run_in_executor(None, _call)

        # Parse response: text parts go into `content`, function_call parts
        # become tool_calls for the outer tool loop to dispatch + feed back.
        text_chunks: list[str] = []
        tool_calls: list[dict] = []
        try:
            candidates = getattr(response, "candidates", None) or []
            for cand in candidates:
                content_obj = getattr(cand, "content", None)
                if content_obj is None:
                    continue
                for part in getattr(content_obj, "parts", None) or []:
                    text = getattr(part, "text", None)
                    if text:
                        text_chunks.append(text)
                    fc = getattr(part, "function_call", None)
                    if fc is not None and getattr(fc, "name", None):
                        # google-genai returns args as a Mapping; coerce to dict
                        raw_args = getattr(fc, "args", {}) or {}
                        try:
                            args_dict = dict(raw_args)
                        except Exception:
                            args_dict = {}
                        tool_calls.append({
                            "id": f"gemini-{len(tool_calls)}",
                            "name": fc.name,
                            "arguments": args_dict,
                        })
        except Exception as parse_err:
            # Fall back to the legacy convenience accessor if structured
            # parsing throws (older google-genai versions sometimes do).
            logger.debug("Gemini response parse warning: %s", parse_err)

        content = "".join(text_chunks) if text_chunks else (response.text or "")
        # Detect MAX_TOKENS truncation so we don't silently return a
        # half-sentence reply. Same _is_max_tokens_truncation helper
        # the SDK uses; import lazily to keep gateway boot cheap.
        from nexus_core.llm.client import (
            _is_max_tokens_truncation, _TRUNCATION_MARKER,
        )
        finish_reason = None
        if response.candidates:
            finish_reason = getattr(response.candidates[0], "finish_reason", None)
        if not tool_calls and _is_max_tokens_truncation(finish_reason):
            logger.warning(
                "Gemini (gateway) response truncated by max_tokens "
                "(finish_reason=%s, text_chars=%d)",
                finish_reason, len(content),
            )
            content = (content or "") + _TRUNCATION_MARKER
        stop_reason = "tool_calls" if tool_calls else "stop"
        logger.info(
            "Gemini raw response: %d chars, %d tool_calls",
            len(content), len(tool_calls),
        )
        # #113: meter token usage if we have a calling user.
        _record_call_usage_safe("gemini", model, response, "gemini")
        return content, model, stop_reason, tool_calls

    except Exception as e:
        logger.error("Gemini API error: %s", e, exc_info=True)
        raise


def _record_call_usage_safe(
    provider: str, model: str, response, extractor_key: str,
) -> None:
    """Best-effort token metering. Never throws — usage tracking is a
    nice-to-have, the chat path is the real product. Extractor_key
    selects which provider's response shape parser to use."""
    try:
        user_id = _current_user_var.get()
        if not user_id:
            return  # called outside an authenticated chat (tests, etc.)
        from nexus_server import llm_usage
        if extractor_key == "gemini":
            p, c = llm_usage.extract_gemini_usage(response)
        elif extractor_key == "openai":
            p, c = llm_usage.extract_openai_usage(response)
        elif extractor_key == "anthropic":
            p, c = llm_usage.extract_anthropic_usage(response)
        else:
            return
        if p == 0 and c == 0:
            return  # provider didn't report usage
        llm_usage.record_usage(user_id, provider, model, p, c)
    except Exception as e:  # noqa: BLE001
        logger.debug("usage metering failed: %s", e)


async def call_openai(messages, system_prompt, model, temperature, max_tokens, tools):
    """Call OpenAI API with tool support."""
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise ValueError("openai not installed. Install with: pip install openai")

    if not config.OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY not configured")

    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return await _call_openai_compatible(
        client, "openai", "OpenAI",
        messages, system_prompt, model, temperature, max_tokens, tools,
    )


# Moonshot runs two disjoint key namespaces: the international platform
# (platform.moonshot.ai → api.moonshot.ai) and the China platform
# (platform.moonshot.cn → api.moonshot.cn). A key from one returns 401
# invalid_authentication_error on the other. When the operator hasn't
# pinned KIMI_BASE_URL explicitly, we try the default first and on a
# 401 retry the sibling region ONCE; the winner is cached here for the
# process lifetime (and exported to os.environ so the SDK's twin path
# resolves the same region).
_KIMI_REGION_SIBLING = {
    "https://api.moonshot.ai/v1": "https://api.moonshot.cn/v1",
    "https://api.moonshot.cn/v1": "https://api.moonshot.ai/v1",
}
_kimi_resolved_base: str | None = None


async def call_kimi(messages, system_prompt, model, temperature, max_tokens, tools):
    """Call Moonshot AI Kimi — OpenAI-compatible Chat Completions API.

    Same wire protocol as OpenAI (chat, tool calling, finish_reason
    "length" on truncation), just a different base_url + key. Key is
    KIMI_API_KEY (MOONSHOT_API_KEY accepted as fallback in config);
    endpoint defaults to https://api.moonshot.ai/v1, overridable via
    KIMI_BASE_URL. On 401 with the default endpoint, auto-falls back
    to the sibling region (.cn/.ai) once and remembers the winner.
    """
    global _kimi_resolved_base
    try:
        from openai import AsyncOpenAI, AuthenticationError
    except ImportError:
        raise ValueError("openai not installed. Install with: pip install openai")

    if not config.KIMI_API_KEY:
        raise ValueError(
            "KIMI_API_KEY not configured (MOONSHOT_API_KEY is also accepted)"
        )

    explicit_override = bool(_os.getenv("KIMI_BASE_URL", "").strip())
    base = _kimi_resolved_base or config.KIMI_BASE_URL

    # kimi-k2.7-code (and possibly other Kimi models) rejects any
    # temperature other than 1 with 400 "invalid temperature: only 1
    # is allowed for this model". Omit the parameter entirely and let
    # the API apply the model's own default — deterministic-ish output
    # is achieved via prompting for these models, not temperature.
    async def _attempt(base_url: str):
        client = AsyncOpenAI(api_key=config.KIMI_API_KEY, base_url=base_url)
        return await _call_openai_compatible(
            client, "kimi", "Kimi",
            messages, system_prompt, model, None, max_tokens, tools,
        )

    try:
        result = await _attempt(base)
        _kimi_resolved_base = base
        return result
    except AuthenticationError:
        sibling = _KIMI_REGION_SIBLING.get(base)
        if explicit_override or not sibling:
            raise
        logger.info(
            "Kimi 401 on %s — retrying sibling region %s "
            "(key likely from the other Moonshot platform)", base, sibling,
        )
        result = await _attempt(sibling)
        _kimi_resolved_base = sibling
        # Let the SDK/twin path resolve the same region.
        _os.environ["KIMI_BASE_URL"] = sibling
        logger.info("Kimi region resolved to %s (cached for process)", sibling)
        return result


async def _call_openai_compatible(
    client, provider_label, display_name,
    messages, system_prompt, model, temperature, max_tokens, tools,
):
    """Shared Chat Completions round-trip for OpenAI and any
    OpenAI-compatible provider (Kimi). ``provider_label`` is used for
    usage metering; ``display_name`` only for log lines."""
    chat_messages = []
    if system_prompt:
        chat_messages.append({"role": "system", "content": system_prompt})
    chat_messages.extend(messages)

    kwargs = {"model": model, "messages": chat_messages}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if tools:
        kwargs["tools"] = [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools
        ]

    try:
        response = await client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        content = choice.message.content or ""
        tool_calls = []
        if choice.message.tool_calls:
            import json
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, ValueError):
                    args = {}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": args,
                })
        stop_reason = "tool_calls" if tool_calls else (choice.finish_reason or "stop")
        if not tool_calls:
            from nexus_core.llm.client import (
                _is_max_tokens_truncation, _TRUNCATION_MARKER,
            )
            if _is_max_tokens_truncation(choice.finish_reason):
                logger.warning(
                    "%s (gateway) response truncated by max_tokens "
                    "(finish_reason=%s, text_chars=%d)",
                    display_name, choice.finish_reason, len(content),
                )
                content = (content or "") + _TRUNCATION_MARKER
        _record_call_usage_safe(provider_label, model, response, "openai")
        return content, model, stop_reason, tool_calls
    except Exception as e:
        logger.error("%s API error: %s", display_name, e)
        raise


async def call_anthropic(messages, system_prompt, model, temperature, max_tokens, tools):
    """Call Anthropic Claude API with tool support."""
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        raise ValueError("anthropic not installed. Install with: pip install anthropic")

    if not config.ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not configured")

    client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

    kwargs = {"model": model, "messages": messages, "max_tokens": max_tokens or 8192}
    if system_prompt:
        kwargs["system"] = system_prompt
    if temperature is not None:
        kwargs["temperature"] = temperature
    if tools:
        kwargs["tools"] = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in tools
        ]

    try:
        response = await client.messages.create(**kwargs)
        text_parts = []
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input,
                })
        content = "\n".join(text_parts)
        stop_reason = "tool_calls" if tool_calls else (response.stop_reason or "stop")
        if not tool_calls:
            from nexus_core.llm.client import (
                _is_max_tokens_truncation, _TRUNCATION_MARKER,
            )
            if _is_max_tokens_truncation(response.stop_reason):
                logger.warning(
                    "Anthropic (gateway) response truncated by max_tokens "
                    "(stop_reason=%s, text_chars=%d)",
                    response.stop_reason, len(content),
                )
                content = (content or "") + _TRUNCATION_MARKER
        _record_call_usage_safe("anthropic", model, response, "anthropic")
        return content, model, stop_reason, tool_calls
    except Exception as e:
        logger.error("Anthropic API error: %s", e)
        raise


# ───────────────────────────────────────────────────────────────────────────
# Route with Tool Loop
# ───────────────────────────────────────────────────────────────────────────


@router.post("/chat", response_model=LLMChatResponse)
async def llm_chat(
    request: LLMChatRequest,
    current_user: str = Depends(get_current_user),
) -> LLMChatResponse:
    """Chat with LLM, executing tools server-side when needed.

    The server runs a tool loop: if the LLM requests a tool call (web search,
    URL read), the server executes it and feeds the result back to the LLM.
    This repeats up to MAX_TOOL_ROUNDS times until the LLM gives a final text response.
    """
    if not check_rate_limit(
        current_user, "/api/v1/llm/chat",
        config.RATE_LIMIT_LLM_REQUESTS_PER_MINUTE,
    ):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")

    # #113: stamp the user_id on the async context so call_gemini /
    # call_openai / call_anthropic can record token usage against
    # the right account. delegate sub-agents inherit via asyncio's
    # contextvar copy-on-task semantics — no manual threading needed.
    _current_user_var.set(current_user)

    # Run quick validation OUTSIDE the broad try/except below so structured
    # 4xx responses (413, 400, …) aren't accidentally swallowed and remapped
    # to 500 by the generic exception handler.
    _validate_attachment_total(request.attachments)

    # ── Attachment distillation ──────────────────────────────────────
    # If the user attached files, run them through the distiller BEFORE
    # the main chat call. We replace each attachment's content_text with
    # the distilled summary so the model sees a curated view (saves tokens
    # AND lets future turns reference these files even if not re-attached).
    summaries: list[AttachmentSummary] = []
    if request.attachments:
        from nexus_server.attachment_distiller import (
            distill_attachment, distill_image,
        )
        from nexus_server import files as files_mod

        # Resolve any attachments that reference uploaded files by id —
        # thin-client path. We swap content_base64 in for downstream
        # processing so the rest of the loop stays unchanged.
        #
        # #148 — for large files (DICOM CT zips routinely 500 MB-1.5 GB),
        # we MUST NOT slurp them into memory just to base64-encode them.
        # Above this cap we leave content_base64 unset and carry only
        # disk_path; downstream handlers (DICOM archive renderer, etc.)
        # read the file directly without round-tripping through memory.
        # 50 MB chosen because Gemini's hard limit on a single image is
        # ~20 MB and any text-only attachment that big shouldn't be
        # base64-encoded anyway (distill works on text).
        ids_to_resolve = [a.file_id for a in request.attachments if a.file_id]
        resolved_by_id: dict[str, dict] = {}
        BASE64_INLINE_CAP_BYTES = 50 * 1024 * 1024
        if ids_to_resolve:
            for row in files_mod.resolve_files(current_user, ids_to_resolve):
                size_bytes = int(row.get("size_bytes") or 0)
                entry = {
                    "name": row["name"],
                    "mime": row["mime"],
                    "size_bytes": size_bytes,
                    "disk_path": row.get("disk_path") or "",
                    "content_base64": None,
                    # #152 — DICOM prerender outputs from /files/upload.
                    # When status=="rendered", chat-time skips the
                    # parse+render and attaches the pre-saved PNGs.
                    "dicom_status":      row.get("dicom_status") or "",
                    "dicom_study_id":    row.get("dicom_study_id") or "",
                    "dicom_preview_dir": row.get("dicom_preview_dir") or "",
                }
                if size_bytes <= BASE64_INLINE_CAP_BYTES:
                    raw = files_mod.read_file_bytes(row["disk_path"])
                    if raw is None:
                        # File gone? Skip — distill will fall back to a stub.
                        continue
                    import base64 as _b64
                    entry["content_base64"] = _b64.b64encode(raw).decode("ascii")
                else:
                    logger.info(
                        "Skipping base64 inline for large file %s (%d MB) — "
                        "downstream handler will read disk_path directly.",
                        row["name"], size_bytes // (1024 * 1024),
                    )
                resolved_by_id[row["file_id"]] = entry

        # Phase Q fix REPLACED by the three-layer file store
        # (nexus_server.files.resolve_file_text + uploads.extracted_text):
        # cross-turn read no longer relies on the twin's in-memory
        # _file_reader cache, so we don't need to hand-stash here.
        # The /files/upload route already wrote the bytes to disk +
        # emitted file_uploaded into the EventLog, and
        # the SDK's ReadUploadedFileTool delegates to the SQL store
        # via the resolver wired up in twin_manager._create_twin.

        # #148 — pre-expand DICOM-zip attachments into 3 rendered PNGs
        # (MIP + middle slice + 4×4 grid) BEFORE the per-attachment
        # loop. This way the image branch below treats them as normal
        # image uploads. Non-zip / non-DICOM attachments pass through
        # unchanged so PDF / docx / etc. still hit distill_attachment.
        expanded_attachments: list[Attachment] = []
        for _att in request.attachments:
            if _att.file_id and _att.file_id in resolved_by_id:
                _rb = resolved_by_id[_att.file_id]
                _disk = _rb.get("disk_path") or ""
                _pre_status = _rb.get("dicom_status") or ""
                _pre_dir = _rb.get("dicom_preview_dir") or ""
            else:
                _disk = ""
                _pre_status = ""
                _pre_dir = ""
            expanded_attachments.extend(
                _maybe_rewrite_dicom_archive_to_pngs(
                    _att, _disk, current_user,
                    prerender_status=_pre_status,
                    prerender_preview_dir=_pre_dir,
                ),
            )
        # Replace the request.attachments-derived list we iterate over
        # below. Original request.attachments is left alone (still used
        # later for referenced_file_ids / chip metadata).
        attachments_iter: list[Attachment] = expanded_attachments

        distilled_attachments: list[Attachment] = []
        # #123 — Image attachments take a different route: skip distill
        # (no point summarising raw pixel bytes via an LLM call), and
        # don't fold them as text either. They ride through as
        # multimodal ``images`` parts on the user message so Gemini
        # vision sees them directly. Each entry below carries the raw
        # base64 bytes + MIME so the SDK client can emit an
        # ``inline_data`` Blob part (see _messages_to_gemini_contents).
        image_parts: list[dict] = []
        for att in attachments_iter:
            if att.file_id and att.file_id in resolved_by_id:
                r = resolved_by_id[att.file_id]
                att = Attachment(
                    name=r["name"],
                    mime=r["mime"],
                    size_bytes=r["size_bytes"],
                    content_text=None,
                    content_base64=r["content_base64"],
                )

            # ── #141: DICOM pre-processing ──────────────────────────
            # PACS exports often have no .dcm extension (filename is
            # the SOPInstanceUID — looks like "1.2.156..." just
            # numbers + dots), so the desktop's GuessMime falls back
            # to application/octet-stream and the image branch below
            # never fires. We detect by magic-byte at offset 128 +
            # "DICM" and rewrite the attachment as a rendered PNG so
            # the rest of the pipeline (caption distill → memory →
            # vision call) sees it as a normal medical image.
            #
            # Single .dcm: render the slice with the modality's
            # default window → one PNG.
            # DICOM .zip: parse → render MIP + middle slice + grid →
            # three image_parts. Handled separately further down
            # because zip needs full archive extraction.
            att = _maybe_rewrite_dicom_to_png(att)

            # ── Image branch: vision multimodal, no text distill ─────
            if att.mime and att.mime.startswith("image/") and att.content_base64:
                image_parts.append({
                    "mime": att.mime,
                    "data_b64": att.content_base64,
                    "name": att.name,
                    "size_bytes": att.size_bytes,
                    # #128: carry the upstream file_id through so
                    # downstream layers (twin.chat / event_log) can
                    # bind the assistant's reply to this specific image
                    # via referenced_file_ids without re-parsing chips.
                    "file_id": att.file_id,
                })
                # #128 — run a vision-driven caption distill so the
                # attachment_distilled event records WHAT this image
                # is, not just THAT one was sent. The caption is what
                # Memory Fix A surfaces in cross-session uploads
                # blocks, what memory_evolver feeds on, and (via #136)
                # what the vector index embeds for semantic recall.
                # If the vision call fails we fall back to a neutral
                # stub — never block the chat turn on caption distill.
                try:
                    caption, caption_source = await distill_image(
                        name=att.name,
                        mime=att.mime,
                        size_bytes=att.size_bytes,
                        content_base64=att.content_base64,
                        llm_fn=call_llm,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "distill_image raised for %s: %s — using stub",
                        att.name, e,
                    )
                    caption = (
                        f"[Image — {att.name} ({att.mime}, "
                        f"{att.size_bytes} bytes); caption pipeline "
                        f"crashed: {e}]"
                    )
                    caption_source = "vision-caption+error"

                summaries.append(AttachmentSummary(
                    name=att.name,
                    mime=att.mime,
                    size_bytes=att.size_bytes,
                    summary=caption,
                    source=caption_source,
                    sync_id=None,
                ))
                # IMPORTANT: don't append to distilled_attachments —
                # we don't want the fold-in path to re-encode the
                # image as an "[Attachments]" text block. The vision
                # part above is the only path the model needs for the
                # current turn; the caption above is for memory.
                continue

            # ── Non-image branch: original distill path ──────────────
            try:
                summary, source = await distill_attachment(
                    name=att.name,
                    mime=att.mime,
                    size_bytes=att.size_bytes,
                    content_text=att.content_text,
                    content_base64=att.content_base64,
                    llm_fn=call_llm,
                )
            except Exception as e:
                logger.error("Distill failed for %s: %s", att.name, e)
                summary, source = (
                    f"[Could not distill {att.name}: {e}]",
                    "error",
                )
            # Phase B: persistence to sync_events removed. The summary
            # rides back inline in the response; if the desktop wants
            # historical attachment records, twin's own EventLog
            # captures them via the chat flow's event_log.append.
            summaries.append(AttachmentSummary(
                name=att.name,
                mime=att.mime,
                size_bytes=att.size_bytes,
                summary=summary,
                source=source,
                sync_id=None,
            ))
            # Replace the attachment's payload with the distilled view for
            # the actual chat call: the model sees a curated summary, not
            # the raw bytes. This saves tokens and gives a stable reference.
            distilled_attachments.append(Attachment(
                name=att.name,
                mime=att.mime,
                size_bytes=att.size_bytes,
                content_text=summary,
                content_base64=None,
            ))
        # Substitute distilled attachments in for fold-in (image-only
        # turns leave attachments_for_fold empty — correct, no text
        # fence to add).
        attachments_for_fold = distilled_attachments
    else:
        attachments_for_fold = []
        image_parts = []

    try:
        messages = [{"role": msg.role, "content": msg.content} for msg in request.messages]
        # Fold (now distilled) attachments into the last user message
        messages = _fold_attachments_into_messages(messages, attachments_for_fold)
        if attachments_for_fold:
            logger.info(
                "Folded %d distilled attachments into chat for user %s",
                len(attachments_for_fold),
                current_user,
            )

        # ── Production path: twin.chat ─────────────────────────────
        # Twin owns the full Nexus 9-step (contract pre-check → DPM
        # projection → LLM → contract post-check → drift → event_log →
        # background evolution). Twin EventLog writes are mirrored to
        # sync_events by twin_manager._build_on_event so existing
        # /sync/anchors, /agent/timeline, /agent/memories endpoints
        # keep working without code changes.
        #
        # S1 (server cleanup): the previous "fall back to legacy LLM
        # gateway when twin throws" path is GONE. Twin failures surface
        # as 502 to the caller — better than silently producing answers
        # the agent's contract / drift / memory will never see. The
        # legacy direct-LLM tool loop below this block is *only* taken
        # when USE_TWIN=0 (i.e. tests mocking call_llm); in production
        # USE_TWIN=1 means we never reach it. S5/S6 will retire that
        # code entirely once tests migrate to twin stubs.
        if _twin_enabled():
            # Extract the BARE user message (what the user typed) from
            # the unmodified incoming request — separate from the
            # folded view (which has [Attachments] fence + distilled
            # summaries inlined and is the LLM-context view only).
            bare_messages = [
                {"role": msg.role, "content": msg.content}
                for msg in request.messages
            ]
            bare_user_msg = next(
                (m["content"] for m in reversed(bare_messages)
                 if m.get("role") == "user"),
                "",
            )
            folded_user_msg = next(
                (m["content"] for m in reversed(messages)
                 if m.get("role") == "user"),
                "",
            )
            # Image-only paste is legitimate: user drags a CT
            # screenshot and clicks Send without typing — Gemini
            # vision answers about the picture. So this guard must
            # also accept the case where text is empty but
            # image_parts (#123) carries pixels for the LLM to see.
            if not bare_user_msg and not folded_user_msg and not image_parts:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No user message in chat request",
                )

            # If the user only attached images with no text, give the
            # LLM a default prompt so it knows what to do. Without
            # this, Gemini sees `parts=[inline_data]` with no text
            # at all and often returns a one-word reply or just
            # describes pixels. The text comes from the user's
            # implicit intent — "look at this and tell me what you
            # see". Override to Chinese when the system signals that
            # locale (RAG / persona / past chats), otherwise fall
            # back to a bilingual default.
            if image_parts and not bare_user_msg and not folded_user_msg:
                bare_user_msg = "请看这张图并告诉我你看到了什么。"

            # Build the chip prefix used for chat-history persistence.
            # Looks like "📎 paper.pdf, deck.pptx" — one line, no
            # inline content. The LLM's view (folded_user_msg) still
            # has the distilled summaries fenced in.
            attachment_chips = ""
            attachments_meta: list[dict] = []
            # #123 — chip names must include image attachments too;
            # they don't go through distill but the user still sees a
            # chip for them and the history pane needs to render the
            # thumbnail.
            chip_sources: list[tuple[str, str, int]] = [
                (a.name, a.mime, a.size_bytes) for a in attachments_for_fold
            ] + [
                (ip["name"], ip["mime"], ip["size_bytes"]) for ip in image_parts
            ]
            if chip_sources:
                names = ", ".join(n for n, _, _ in chip_sources)
                attachment_chips = f"📎 {names}"
                # Structured metadata so /agent/messages can surface
                # real chip UI (not fallback text). Mirrors the
                # client's AttachmentInfo wire shape.
                attachments_meta = [
                    {"name": n, "mime": m, "size_bytes": sz}
                    for n, m, sz in chip_sources
                ]

            # ── Multi-session: validate the requested session belongs
            # to this user (forged ids must not be able to jump into
            # other users' threads via twin). The default session
            # (id="" / None) and known-owned ids both pass.
            from nexus_server import sessions as sessions_mod
            session_id = (request.session_id or "").strip()
            if session_id and not sessions_mod.ensure_session_exists(
                current_user, session_id,
            ):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Session {session_id} not found for this user",
                )

            # Phase B fix: snapshot the event-log high-water mark BEFORE
            # twin.chat runs so we can collect any side-effect events
            # (workflow_run cards from run_workflow tool, etc.) the
            # agent inserts mid-turn and ship them back to the client.
            # Without this the inline workflow card never reaches the
            # desktop until the user manually navigates away + back.
            from nexus_server import twin_event_log as _tel
            pre_turn_idx = _tel.latest_event_idx(current_user)

            # Context injections — each runs cheap and bails empty when
            # there's nothing to surface:
            #   1. Uploads memory (Memory Fix A — agent remembers files
            #      across sessions instead of pretending they don't exist)
            #   2. Workflow recipes (#91 — for each installed workflow,
            #      tell the agent the delegate() chain to execute)
            try:
                uploads_block = _build_uploads_memory_block(current_user)
            except Exception as e:  # noqa: BLE001
                logger.debug("uploads memory probe failed: %s", e)
                uploads_block = ""

            try:
                recipes_block = _build_workflow_recipes_block(current_user)
            except Exception as e:  # noqa: BLE001
                logger.debug("workflow recipes probe failed: %s", e)
                recipes_block = ""

            # #118: email + calendar capability awareness (returns "" if
            # no backend configured so it stays out of dev / test
            # contexts where the tools wouldn't do anything anyway).
            try:
                email_block = _build_email_capability_block()
            except Exception as e:  # noqa: BLE001
                logger.debug("email capability block probe failed: %s", e)
                email_block = ""

            # #138 — RAG memory grounding. Pull the top-k semantically
            # similar past chunks (chat / caption / attachment) for the
            # current user input + any image captions just produced,
            # and inject them as a [RELATED PRIOR CONTEXT] block. The
            # agent then has both the natural conversation history AND
            # the cross-session recall in one prompt, so it doesn't
            # need to actively call semantic_search every turn.
            #
            # Cheap (one embedding round-trip), best-effort (no block
            # on failure), and skipped when there's nothing useful to
            # search for (empty query, vector backend unavailable).
            try:
                related_block = await _build_related_context_block(
                    user_id=current_user,
                    bare_text=bare_user_msg,
                    image_captions=[
                        s.summary for s in summaries
                        if s.source and s.source.startswith("vision-caption")
                        and s.summary
                    ],
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("related context probe failed: %s", e)
                related_block = ""

            context_blocks = "\n\n".join(
                b for b in (
                    uploads_block, recipes_block, email_block, related_block,
                ) if b
            )

            # CRITICAL: context_blocks must ONLY go into the FOLDED
            # message (LLM-context-only), never into ``effective_bare``
            # which gets persisted to the event log and rendered as
            # the user's chat bubble. Earlier versions concatenated
            # context into effective_bare and the user ended up seeing
            # the entire scaffolding (uploads list + workflow recipes
            # + rules block) as part of their own message. Painful.
            effective_bare = bare_user_msg
            if context_blocks:
                # Inject context into folded so the LLM sees it, but
                # the user-bubble persistence stays clean.
                base = folded_user_msg if folded_user_msg else bare_user_msg
                effective_folded = f"{context_blocks}\n\n{base}"
            else:
                effective_folded = (
                    folded_user_msg if folded_user_msg != bare_user_msg else None
                )

            from nexus_server.twin_manager import get_twin
            try:
                twin = await get_twin(current_user)
                # Pass BARE user msg + chip prefix (for persistence) +
                # folded msg (for LLM context only). Twin handles the
                # rest. session_id="" → twin keeps current _thread_id.
                # #128 — collect file_ids of every attachment in this
                # turn (text + image alike) so twin can stamp them on
                # the assistant_response event's metadata. Lets
                # downstream feedback / search bind the reply to the
                # specific images / files it answered about — no
                # heuristic time-window or hashtag parsing needed.
                referenced_file_ids: list[str] = []
                for att in request.attachments:
                    if att.file_id:
                        referenced_file_ids.append(att.file_id)

                # #136 — hand the distilled summaries to twin so it can
                # write proper ``attachment_distilled`` events into the
                # event log. Without this step the chain that follows
                # (Memory Fix B → memory_evolver → curated memory) has
                # no input — summaries were previously returned to the
                # desktop and dropped on the floor (the Phase Q desktop
                # persistence path was killed in Phase B). The shape
                # mirrors what the historic sync_events row carried so
                # downstream readers don't need a migration.
                #
                # Pair each summary with its file_id when available so
                # downstream embedding (next step) can use file_id as
                # the stable source_id; otherwise we fall back to name.
                file_id_by_name = {
                    a.name: a.file_id for a in request.attachments
                    if a.file_id
                }
                distilled_for_twin = [
                    {
                        "name": s.name,
                        "mime": s.mime,
                        "size_bytes": s.size_bytes,
                        "summary": s.summary,
                        "source": s.source,
                        "file_id": file_id_by_name.get(s.name) or "",
                    }
                    for s in summaries
                ]

                reply = await twin.chat(
                    effective_bare,
                    session_id=session_id or None,
                    attachment_chips=attachment_chips,
                    attachments_meta=attachments_meta or None,
                    folded_user_message=effective_folded,
                    # #123 — vision parts ride parallel to the text;
                    # twin will attach them to the current user
                    # message dict so client._messages_to_gemini_contents
                    # emits inline_data parts. Empty list is a no-op.
                    images=image_parts or None,
                    referenced_file_ids=referenced_file_ids or None,
                    distilled_attachments=distilled_for_twin or None,
                )
            except HTTPException:
                raise
            except Exception as twin_err:
                logger.exception(
                    "Twin chat failed for %s: %s", current_user, twin_err,
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Twin chat error: {twin_err}",
                )

            # ── Session bookkeeping: bump last_message_at + count, and
            # opportunistically auto-title sessions still on their
            # placeholder ("New chat"). Best-effort — a DB hiccup
            # here must not invalidate a successful chat reply.
            if session_id:
                try:
                    # Each turn = 2 events (user_message + assistant_response).
                    sessions_mod.touch_session(
                        current_user, session_id, delta_message_count=2,
                    )
                    # Auto-title from the bare user text (no chips, no
                    # folded attachment summaries) so the rail label
                    # reads naturally. NOTE: variable was previously
                    # called `last_user_msg`; the rename to
                    # `bare_user_msg` upstream broke this silently for
                    # months — every call raised NameError which the
                    # try/except swallowed, so the rail stayed stuck
                    # on "New chat" forever.
                    sessions_mod.maybe_apply_autotitle(
                        current_user, session_id, bare_user_msg,
                    )
                except Exception as e:
                    logger.warning(
                        "session bookkeeping failed for %s/%s: %s",
                        current_user, session_id, e,
                    )

            logger.info(
                "Twin chat: %d-char reply for user %s session=%s",
                len(reply or ""), current_user, session_id or "(default)",
            )

            # #97: empty-reply guard. The SDK tool loop in client.py
            # returns "" when the LLM emits neither text nor a new
            # function_call on its final round. That manifests in the
            # desktop as a blank assistant bubble — looks like the app
            # broke, but the agent just ran out of things to say
            # (usually after a tool call returned an error, or after
            # the recipe completed and the LLM thought its job was the
            # tool result, not a text summary). Substitute a clear
            # placeholder so the user knows to retry / check logs
            # instead of staring at an empty bubble. Also overwrite the
            # event-log row so chat history doesn't preserve the empty
            # turn as if it were real assistant content.
            if reply is not None and not reply.strip():
                logger.warning(
                    "Twin chat returned empty reply for user %s session=%s "
                    "— substituting placeholder. Likely cause: LLM emitted "
                    "no text on its final tool-loop round.",
                    current_user, session_id or "(default)",
                )
                reply = (
                    "(No reply produced. The agent reached the end of "
                    "its tool loop without writing a text response — "
                    "this usually means a sub-agent / tool call failed "
                    "silently. Try rephrasing, or check the server log "
                    "at ~/Library/Application Support/RuneProtocol/server.log "
                    "for details.)"
                )
                try:
                    _tel.replace_last_assistant_response(
                        current_user, session_id or "", reply,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug(
                        "empty-reply guard: replace_last_assistant_response failed: %s", e,
                    )

            # #97 (round 2): fake-workflow-narration guard. Even with
            # run_workflow tool deleted (#91) and the recipe block
            # instructing the agent to call delegate(), Gemini will
            # SOMETIMES just ad-lib "the workflow is running, output
            # will arrive separately" as plain text — pure prior-driven
            # hallucination, no actual tool call. We detect that exact
            # failure mode (narration phrase + no delegate call this
            # turn) and rewrite the reply to a useful instruction.
            #
            # Detection uses simple substring matching against the
            # phrases Gemini reaches for. False positives are cheap —
            # legitimate replies don't talk about workflows running in
            # the background. The reply gets replaced AND the event log
            # is overwritten so chat history doesn't preserve the lie.
            elif reply is not None and _looks_like_fake_workflow_narration(reply):
                # Was any delegate() actually invoked this turn? Look
                # at the side-effect events — DelegateTool doesn't emit
                # any, but the SDK's call_tools logs INFO lines. Cheap
                # proxy: if reply talks about running a workflow but
                # the previous-step's recipe block in context would
                # have required delegate, and the agent wrote prose
                # instead, that's the bug.
                logger.warning(
                    "Detected fake-workflow narration in reply for %s "
                    "(no delegate call this turn) — replacing.",
                    current_user,
                )
                installed_names: list[str] = []
                try:
                    from nexus_server import workflows as _wf
                    installed_names = [w.name for w in _wf.list_workflows(current_user)]
                except Exception as exc:
                    logger.debug("listing workflows failed: %s", exc)
                workflows_hint = (
                    f"Installed workflows: {', '.join(installed_names)}.\n"
                    if installed_names else
                    "No workflows are installed yet. Open the Workflows "
                    "view and click 'Install' on a pack first.\n"
                )
                reply = (
                    "I narrated 'workflow running' but didn't actually "
                    "execute any sub-agent — that's a known failure mode "
                    "where I hallucinate a status update instead of "
                    "calling the recipe's delegate() steps.\n\n"
                    + workflows_hint +
                    "Try one of:\n"
                    "  • Rephrase your request more specifically "
                    "(e.g. 'Use Content Studio to write a Twitter thread "
                    "about Starknet for crypto investors').\n"
                    "  • Ask me directly without invoking a workflow.\n"
                    "  • Manually pick the pack in the Workflows view."
                )
                try:
                    _tel.replace_last_assistant_response(
                        current_user, session_id or "", reply,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug(
                        "fake-narration guard: replace failed: %s", e,
                    )

            # #173 — same guard for defer_to_background. Agent says
            # "我会跑一下，做完邮件通知你" but never emits the
            # function_call → async_tasks queue stays empty → user
            # waits for an email that never arrives. We detect by:
            #   (a) reply phrases look like a defer confirmation, AND
            #   (b) no new row in async_tasks for this user in the
            #       last ~10 seconds (covers the time window of this
            #       turn).
            # If both, prepend a clear failure notice to the reply
            # AND auto-recover by enqueueing the task ourselves
            # based on the bare user message. Cheaper than asking
            # the medic to rephrase.
            if reply is not None and _looks_like_fake_defer_narration(reply):
                no_task_landed = True
                recent_task_id = ""
                try:
                    import time as _time
                    from nexus_server.async_tasks import list_user_tasks
                    recent = list_user_tasks(current_user, limit=5)
                    cutoff = _time.time() - 30   # last 30s
                    for r in recent:
                        if r["created_at"] >= cutoff:
                            no_task_landed = False
                            recent_task_id = r["task_id"]
                            break
                except Exception as e:  # noqa: BLE001
                    logger.debug(
                        "defer-hallucination guard: list_user_tasks "
                        "failed: %s", e,
                    )
                if no_task_landed:
                    logger.warning(
                        "Detected fake-defer narration in reply for %s "
                        "(no async_task row in last 30s) — auto-"
                        "recovering via direct enqueue.",
                        current_user,
                    )
                    # Auto-recover: enqueue using the bare user text
                    # as the action_prompt. Better UX than asking
                    # the medic to retry — they already typed it.
                    try:
                        from nexus_server.async_tasks import enqueue_task
                        recent_task_id = enqueue_task(
                            user_id=current_user,
                            session_id=session_id or "",
                            description=(
                                (bare_user_msg or "(deferred task)")[:80]
                            ),
                            action_prompt=bare_user_msg
                                or folded_user_msg or "",
                            eta_seconds=300,
                            email_to=current_user,  # user_id is the email
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.exception(
                            "defer-hallucination recovery enqueue "
                            "failed: %s", e,
                        )
                    reply = (
                        "⚠ I narrated 'I'll work on this and email you' "
                        "but didn't actually emit the function_call to "
                        "defer_to_background — that's a known "
                        "hallucination class. I've now scheduled it "
                        "for real (task id "
                        f"{recent_task_id[:8] if recent_task_id else '?'}). "
                        "You should see a card appear in the "
                        "Background tasks panel within a few seconds, "
                        "and an email when it finishes.\n\n"
                        "If you'd rather I just run it inline and "
                        "block the chat until done, say "
                        "'just run it now' instead."
                    )
                    try:
                        _tel.replace_last_assistant_response(
                            current_user, session_id or "", reply,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.debug(
                            "defer-hallucination guard: replace "
                            "failed: %s", e,
                        )

            # Phase B fix: collect any side-effect events (workflow_run
            # cards) the agent's tools inserted during this turn and
            # ship them in the response. The desktop's ChatViewModel
            # renders them inline between the user bubble and the
            # assistant text bubble in chronological order.
            try:
                side_effects = _tel.list_side_effect_events_since(
                    current_user, session_id or "", pre_turn_idx,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("side-effect collection failed: %s", e)
                side_effects = []

            # #91: rescue path deleted. With recipe-based workflows
            # there's no fire-and-forget tool semantic to hallucinate —
            # if the agent doesn't call delegate(), nothing happens
            # silently. Side-effect events still ship (e.g. for future
            # tools that genuinely emit inline cards).

            try:
                side_effect_events = [
                    SideEffectEvent(
                        sync_id=ev["sync_id"],
                        event_type=ev["event_type"],
                        content=ev["content"],
                        timestamp=ev["timestamp"],
                        metadata=ev["metadata"] or {},
                    )
                    for ev in side_effects
                ]
            except Exception as e:  # noqa: BLE001
                logger.warning("side-effect SideEffectEvent build failed: %s", e)
                side_effect_events = []

            return LLMChatResponse(
                role="assistant",
                content=reply or "",
                model="twin",
                stop_reason="stop",
                tool_calls_executed=[],
                attachment_summaries=summaries,
                side_effect_events=side_effect_events,
            )

        # ── Legacy direct-LLM gateway (test-only after S1) ─────────
        # Reachable only when USE_TWIN=0. Will be removed in S5/S6
        # along with attachment_distiller, memory_service, and the
        # remaining server-side intelligence layer.
        tools = TOOL_DEFINITIONS if request.enable_tools else None
        tools_executed: list[str] = []

        for round_num in range(MAX_TOOL_ROUNDS + 1):
            content, model_used, stop_reason, tool_calls = await call_llm(
                messages, request.system_prompt, request.model,
                request.temperature, request.max_tokens, tools,
            )

            if not tool_calls:
                # Final response — no more tool calls
                logger.info("LLM reply (%d chars) for user %s via %s", len(content or ""), current_user, model_used)

                # NOTE (S3): the legacy server-side memory_service.maybe_compact
                # scheduler used to fire here. It was removed when we deleted
                # memory_service.py — twin owns compaction now via SDK's
                # EventLogCompactor + CuratedMemory. This branch is only
                # reachable in tests (USE_TWIN=0); in production every chat
                # goes through twin and never touches this code.

                return LLMChatResponse(
                    role="assistant",
                    content=content,
                    model=model_used,
                    stop_reason=stop_reason,
                    tool_calls_executed=tools_executed,
                    attachment_summaries=summaries,
                )

            # Execute tool calls and append results
            for tc in tool_calls:
                logger.info("Executing tool: %s(%s)", tc["name"], tc["arguments"])
                result = await execute_tool(tc["name"], tc["arguments"])
                tools_executed.append(tc["name"])

                # Append assistant's tool request + tool result to messages
                messages.append({"role": "assistant", "content": f"[Calling {tc['name']}]"})
                messages.append({"role": "user", "content": f"[Tool result for {tc['name']}]:\n{result}"})

            logger.info("Tool round %d complete, %d tools executed", round_num + 1, len(tool_calls))

        # Exhausted rounds — return whatever we have
        return LLMChatResponse(
            role="assistant",
            content=content or "I ran out of tool execution rounds. Please try a simpler question.",
            model=model_used,
            stop_reason="max_rounds",
            tool_calls_executed=tools_executed,
            attachment_summaries=summaries,
        )

    except HTTPException:
        # Preserve structured error codes (400, 502, …) — without this,
        # the catch-all below would remap a clean 502 ("Twin chat error")
        # into a misleading 500.
        raise
    except Exception as e:
        import traceback
        logger.error("LLM chat error: %s\n%s", e, traceback.format_exc())
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"LLM call failed: {e}")
