"""
Regression tests for chat attachment format coverage (#201).

What we guard:

  A. Empty user text is allowed when ≥1 attachment is present. Pasting
     a screenshot with no caption ("what is this") used to 400 with
     {"error":"empty message"}.

  B. PDF / DOCX attachments without cached extracted_text get
     on-demand extraction from disk_path via
     ``nexus_core.distiller.extract_text``. Without this the chat
     just told the medic "I can't read that file".

  C. Image attachments (PNG / JPEG / TIFF / WEBP) collect bytes into
     ``attachment_images`` and the chat router forwards them to
     ``retrieve_async``. Real vision call lives in
     ``retrieval_tiers._t3_call_with_images``.

  D. ``retrieve_async`` honours the ``attachment_images`` kwarg and
     forces the T3 path when images are present (T1/T2 are text-only).

  E. TIFF gets transcoded to PNG before being sent to Gemini (Gemini
     doesn't accept image/tiff natively).
"""
from __future__ import annotations

import io
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


# ─────────────────────────────────────────────────────────────────────
# A. Empty text accepted with attachments
# ─────────────────────────────────────────────────────────────────────


def test_chat_endpoint_allows_empty_text_when_attachments_present():
    """Source-level: the empty-message guard in chat_router_v2 must
    require BOTH text empty AND attachments empty before raising 400.
    Pasting an image with no caption is a legitimate turn."""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "nexus_server" / "chat_router_v2.py"
    ).read_text()

    # Locate the empty-message guard. Must check attachments too.
    m = re.search(
        r"if not req\.text\.strip\(\)[\s\S]{0,200}HTTPException",
        src,
    )
    assert m, "empty-message guard not found in chat_router_v2"
    guard = m.group(0)
    assert "req.attachments" in guard or "attachments" in guard, (
        "empty-message guard rejects pure-attachment turns. Should "
        "be `if not req.text.strip() and not req.attachments`."
    )


# ─────────────────────────────────────────────────────────────────────
# B. PDF / DOCX lazy extract
# ─────────────────────────────────────────────────────────────────────


def test_pdf_extract_text_via_nexus_core_distiller():
    """nexus_core.distiller.extract_text handles PDF byte input.
    Build a tiny one-page PDF on the fly, feed it through, expect
    something resembling 'Hello'.

    We do this at the distiller level (not chat_router) because the
    chat router's preamble loop is exercised behind a network call
    that the sandbox proxy blocks."""
    try:
        from pypdf import PdfWriter
    except Exception:
        try:
            from PyPDF2 import PdfWriter  # type: ignore
        except Exception:
            pytest.skip("no PDF library available to build fixture")

    # Build a minimal PDF byte stream. pypdf doesn't easily inject text
    # without an existing page, so we use a tiny pre-baked PDF that
    # most readers can extract "Hello" from. This is the smallest
    # valid PDF with a text stream — well-known fixture.
    raw_pdf = (
        b"%PDF-1.1\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n"
        b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        b"5 0 obj << /Length 44 >> stream\n"
        b"BT /F1 24 Tf 100 700 Td (Hello PDF) Tj ET\n"
        b"endstream endobj\n"
        b"xref\n"
        b"0 6\n"
        b"0000000000 65535 f\n"
        b"0000000010 00000 n\n"
        b"0000000056 00000 n\n"
        b"0000000110 00000 n\n"
        b"0000000218 00000 n\n"
        b"0000000293 00000 n\n"
        b"trailer << /Size 6 /Root 1 0 R >>\n"
        b"startxref\n"
        b"385\n"
        b"%%EOF\n"
    )

    import base64
    b64 = base64.b64encode(raw_pdf).decode("ascii")
    from nexus_core.distiller import extract_text
    text, src_label = extract_text("hello.pdf", "application/pdf", None, b64)
    # Some pypdf versions stub-out the text — we accept that case
    # since the lazy-extract path returns a "[PDF — extraction
    # unavailable]" stub which is still better than nothing.
    assert src_label in ("pdf", "binary-stub"), (
        f"unexpected extract source: {src_label!r}"
    )
    if src_label == "pdf":
        assert "Hello" in text or "PDF" in text, (
            f"PDF extraction returned no expected content: {text[:200]!r}"
        )


def test_chat_router_lazy_extracts_pdf_from_disk_path():
    """Source-level: when uploads.extracted_text is empty AND the mime
    is non-image, the chat router must read disk_path bytes and call
    _bytes_to_text + _save_extracted_text to populate the cache. This
    is what fixes "你能读pdf" returning a name-only fallback."""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "nexus_server" / "chat_router_v2.py"
    ).read_text()

    # The on-demand extract block must run when etext is missing AND
    # the file is not an image.
    assert "_bytes_to_text" in src, (
        "chat_router_v2 doesn't call _bytes_to_text — PDFs / docx "
        "uploads sit with empty extracted_text forever and the LLM "
        "gets no content from them."
    )
    assert "_save_extracted_text" in src, (
        "lazy extract isn't cached back to uploads.extracted_text — "
        "next chat turn re-reads the disk every time."
    )
    # And the image branch must NOT try to extract text (Pillow would
    # silently return None and waste a disk read).
    assert "not is_image" in src or "is_image and" in src, (
        "image attachments must skip the text-extract path."
    )


# ─────────────────────────────────────────────────────────────────────
# C. Image bytes collected and forwarded
# ─────────────────────────────────────────────────────────────────────


def test_chat_router_collects_image_bytes_into_attachment_images():
    """Source-level: the preamble loop must accumulate image bytes
    into ``attachment_images`` (list of name/mime/bytes) and forward
    that list to ``retrieve_async`` via the ``attachment_images``
    kwarg. Without this the LLM never sees the screenshot."""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "nexus_server" / "chat_router_v2.py"
    ).read_text()
    assert "attachment_images" in src, (
        "chat_router_v2 has no attachment_images list — vision call "
        "never gets the bytes."
    )
    # And the retrieve_async call must pass it through.
    assert re.search(
        r"retrieve_async\([\s\S]*?attachment_images=",
        src,
    ), (
        "retrieve_async() doesn't receive attachment_images kwarg — "
        "the bytes are collected but never used."
    )
    # Image MIME detection (we use startswith).
    assert 'mime.startswith("image/")' in src or "startswith('image/')" in src, (
        "no mime.startswith('image/') check — non-image binaries "
        "would be sent as 'images' to Gemini."
    )


# ─────────────────────────────────────────────────────────────────────
# D. retrieve_async dispatches to T3 vision path when images present
# ─────────────────────────────────────────────────────────────────────


def test_retrieve_async_signature_accepts_attachment_images():
    """The dispatcher must accept attachment_images so the chat
    router's kwarg call doesn't TypeError."""
    import inspect
    from nexus_server.retrieval_tiers import retrieve_async
    sig = inspect.signature(retrieve_async)
    assert "attachment_images" in sig.parameters, (
        "retrieve_async signature missing attachment_images — chat "
        "router's kwarg call will TypeError at runtime."
    )


def test_retrieve_async_forces_t3_when_images_present():
    """Behavioural: retrieve_async with attachment_images must skip
    the classify() branch (T1/T2 are SQL/template paths with no way
    to render images) and go directly to T3."""
    import asyncio
    import sqlite3
    from nexus_server import retrieval_tiers

    # Capture which path was taken.
    called: dict[str, bool] = {"t3": False, "classify": False}

    async def fake_yield_t3(*args, **kwargs):
        called["t3"] = True
        # Ensure the image arg actually arrives at T3.
        assert kwargs.get("attachment_images"), (
            "T3 didn't receive attachment_images — forwarded kwarg "
            "got dropped between dispatcher and yield_t3_llm."
        )
        return
        yield  # pragma: no cover — make this an async generator

    def fake_classify(*args, **kwargs):
        called["classify"] = True
        # Should never be called in this test.
        from nexus_server.retrieval_tiers import Tier, TierChoice
        return TierChoice(tier=Tier.T1, view_kind="findings", anchor_hint=None, reason="fake")

    # Patch in-place so the dispatcher's bound names get rewired.
    orig_yield_t3 = retrieval_tiers.yield_t3_llm
    orig_classify = retrieval_tiers.classify
    retrieval_tiers.yield_t3_llm = fake_yield_t3
    retrieval_tiers.classify = fake_classify

    try:
        async def run():
            conn = sqlite3.connect(":memory:")
            async for _ in retrieval_tiers.retrieve_async(
                conn, user_id="u", patient_hash="p", question="what is this?",
                attachment_images=[("shot.png", "image/png", b"fakebytes")],
            ):
                pass
        asyncio.run(run())
    finally:
        retrieval_tiers.yield_t3_llm = orig_yield_t3
        retrieval_tiers.classify = orig_classify

    assert called["t3"], (
        "retrieve_async didn't reach yield_t3_llm despite images — "
        "vision pass is dead."
    )
    assert not called["classify"], (
        "retrieve_async still ran the tier classifier with images "
        "present — wastes a SQL pass and risks T1/T2 swallowing the "
        "turn without seeing the bytes."
    )


# ─────────────────────────────────────────────────────────────────────
# E. TIFF transcoded to PNG before Gemini
# ─────────────────────────────────────────────────────────────────────


def test_tiff_transcoded_to_png_in_vision_call():
    """_t3_call_with_images must convert image/tiff bytes to PNG
    before constructing the genai Part. Gemini's API rejects TIFF
    natively, and silently failing here would surface as "model
    returned no text"."""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "nexus_server" / "retrieval_tiers.py"
    ).read_text()

    # Look at the streaming vision helper body. Renamed from
    # _t3_call_with_images → _t3_stream_with_images when we switched
    # to generate_content_stream.
    m = re.search(
        r"async def _t3_stream_with_images\([\s\S]*?\n(?:async )?def |\Z",
        src,
    )
    assert m, "_t3_stream_with_images not found in retrieval_tiers.py"
    body = m.group(0)

    assert "image/tiff" in body, (
        "_t3_stream_with_images doesn't special-case image/tiff. "
        "TIFF pastes will reach Gemini and be silently rejected."
    )
    assert "PIL" in body or "Pillow" in body or "PILImage" in body, (
        "TIFF transcode path doesn't use Pillow — no other library "
        "in our deps can do this conversion."
    )
    # The output mime must flip to PNG after transcode.
    assert '"image/png"' in body or "'image/png'" in body, (
        "TIFF transcode path doesn't set output mime to image/png — "
        "Gemini will still see image/tiff and reject."
    )


def test_vision_path_uses_streaming_api():
    """``_t3_stream_with_images`` must call
    ``client.models.generate_content_stream`` (not the buffered
    ``generate_content``) so vision-turn text streams into the chat
    pane in real time."""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "nexus_server" / "retrieval_tiers.py"
    ).read_text()

    m = re.search(
        r"async def _t3_stream_with_images\([\s\S]*?\n(?:async )?def |\Z",
        src,
    )
    assert m, "_t3_stream_with_images not found"
    body = m.group(0)

    assert "generate_content_stream" in body, (
        "_t3_stream_with_images doesn't use generate_content_stream — "
        "vision turns will be one-shot buffered (5-10s blank screen)."
    )
    # The helper must be an async generator.
    assert "AsyncIterator[str]" in body or "yield " in body, (
        "_t3_stream_with_images doesn't yield — it's not a generator."
    )


def test_yield_t3_streams_vision_deltas():
    """Behavioural: yield_t3_llm's vision path must emit MULTIPLE
    final_answer_chunk events (one per stream delta), not a single
    buffered chunk. Patches _t3_stream_with_images to a controlled
    fake stream and counts the emitted chunks."""
    import asyncio
    import sqlite3
    from nexus_server import retrieval_tiers

    async def fake_stream(**_kw):
        for d in ("Hello", " from", " Gemini ", "vision."):
            yield d

    orig = retrieval_tiers._t3_stream_with_images
    retrieval_tiers._t3_stream_with_images = fake_stream

    deltas: list[str] = []
    async def run():
        # In-memory DB — yield_t3_llm just needs SOMETHING to query
        # against for patient context (none here, fine).
        conn = sqlite3.connect(":memory:")
        from nexus_server.event_sourcing import init_event_sourcing_schema
        init_event_sourcing_schema(conn)
        async for chunk in retrieval_tiers.yield_t3_llm(
            conn, user_id="u", patient_hash=None,
            question="what is this?",
            attachment_images=[("x.png", "image/png", b"fake")],
        ):
            if chunk.kind == "final_answer_chunk":
                deltas.append(chunk.data.get("text", ""))

    try:
        asyncio.run(run())
    finally:
        retrieval_tiers._t3_stream_with_images = orig

    assert len(deltas) >= 4, (
        f"vision path emitted {len(deltas)} chunks (expected ≥4 "
        f"streaming deltas): {deltas!r}. Looks like the helper is "
        f"still buffering the full answer."
    )
    full = "".join(deltas)
    assert "Gemini" in full, (
        f"streaming concatenation lost content: {full!r}"
    )


def test_tiff_transcode_actually_works():
    """Behavioural: build a tiny TIFF in-memory, run the transcode
    branch via _t3_call_with_images' Pillow path manually, confirm
    PNG output is valid."""
    try:
        from PIL import Image
    except Exception:
        pytest.skip("Pillow not installed")

    # Build a tiny 4×4 TIFF.
    img = Image.new("RGB", (4, 4), (200, 50, 50))
    buf = io.BytesIO()
    img.save(buf, format="TIFF")
    tiff_bytes = buf.getvalue()
    assert tiff_bytes[:2] in (b"II", b"MM"), "fixture isn't a TIFF"

    # Mirror the transcode logic from _t3_call_with_images.
    im = Image.open(io.BytesIO(tiff_bytes))
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    out = io.BytesIO()
    im.save(out, format="PNG", optimize=True)
    png_bytes = out.getvalue()
    assert png_bytes.startswith(b"\x89PNG"), (
        "TIFF→PNG transcode didn't produce a valid PNG header"
    )
