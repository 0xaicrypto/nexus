"""F-pdf-ocr-fallback — three-layer PDF text extraction with status.

Layer 1: ``pypdf`` text layer (deterministic, fast, no LLM cost).
Layer 2: ``Gemini Vision`` direct PDF input (Gemini 2.5 supports
         ``application/pdf`` inline parts natively — handles scanned
         PDFs that pypdf can't read because there's no text layer).
Layer 3: failure → status='unreadable', medic gets a UI prompt to
         re-upload a text version.

Each call persists the result (text + status) back into
``uploads.extracted_text`` and ``uploads.text_extraction_status`` so
subsequent reads (chat prompt build, listing) hit the SQL cache
without re-running OCR.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Per-file extraction cap. PDFs over this are truncated at the page
# whose cumulative chars exceed the budget. 60k matches the SDK
# distiller's DISTILL_INPUT_CHAR_BUDGET — keeps prompt size sane.
EXTRACTION_CHAR_BUDGET = 60_000

# Threshold below which we consider pypdf to have "failed" and try
# Vision. Empty PDFs and scanned PDFs both come back from pypdf with
# 0-50 chars of garbage (whitespace from page breaks); 100 is a safe
# floor for "real text was extracted".
TEXT_LAYER_MIN_CHARS = 100

# Status constants (mirror schema docstring in migration 0005).
S_PENDING    = "pending"
S_TEXT_LAYER = "text_layer"
S_VISION_OCR = "vision_ocr"
S_UNREADABLE = "unreadable"
S_ENCRYPTED  = "encrypted"


# ─────────────────────────────────────────────────────────────────────
# Layer 1: pypdf text layer
# ─────────────────────────────────────────────────────────────────────


def _extract_via_pypdf(raw: bytes) -> Tuple[str, Optional[str]]:
    """Whole-PDF text-layer extraction via pypdf.

    Legacy wrapper around the per-page variant — returns concatenated
    text only, no per-page breakdown. Used by callers that don't care
    about mixed PDFs (e.g. quick scan).
    """
    per_page, err = _extract_per_page_via_pypdf(raw)
    if err is not None:
        return "", err
    chunks = [f"\n--- Page {i} ---\n{t}" for i, t in per_page if t]
    return "".join(chunks), None


# Per-page text threshold below which we'll send the page to Vision.
# 50 chars catches "just a page number / running header" garbage; a
# real text-layer page has at least a paragraph (~300+ chars).
PER_PAGE_TEXT_LAYER_MIN = 50


def _extract_per_page_via_pypdf(
    raw: bytes,
) -> "tuple[list[tuple[int, str]], Optional[str]]":
    """Per-page pypdf extraction. Returns (pages, error_status) where
    pages is a list of (page_num_1based, text_extracted_or_empty)
    tuples in document order. error_status is None on success,
    'encrypted'/'parse_error' on known failures.

    The caller uses the per-page result to decide which pages need
    Vision OCR vs which already have a usable text layer. Mixed
    PDFs end up with both: text-layer pages stay as-is, scanned
    pages get Vision OCR sent to them only.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf not installed — Layer 1 disabled")
        return [], "parse_error"

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:
        logger.info("pypdf could not open file: %s", exc)
        return [], "parse_error"

    if getattr(reader, "is_encrypted", False):
        try:
            if not reader.decrypt(""):
                return [], S_ENCRYPTED
        except Exception:
            return [], S_ENCRYPTED

    pages: list[tuple[int, str]] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        pages.append((i, t.strip()))
    return pages, None


# ─────────────────────────────────────────────────────────────────────
# Layer 2: Gemini Vision PDF read
# ─────────────────────────────────────────────────────────────────────


_VISION_PROMPT = (
    "Extract ALL text from this document, preserving structure where "
    "obvious (headings, bullet lists, tables as markdown). Output ONLY "
    "the extracted text -- no preamble, no commentary, no explanation. "
    "If the document has multiple pages, separate them with `--- Page "
    "N ---` markers. If the document is unreadable (blank / corrupt / "
    "not a document), return exactly `[UNREADABLE]`."
)


def _get_genai_client() -> "tuple[object, object] | tuple[None, str]":
    """Return (client, types_mod) or (None, error_status)."""
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        logger.warning("google-genai not installed -- Vision fallback disabled")
        return None, "vision_failed"

    api_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    )
    if not api_key:
        logger.info(
            "Vision fallback skipped -- no GEMINI_API_KEY (medic must "
            "configure in Settings > LLM before scanned PDFs work)"
        )
        return None, "no_api_key"
    try:
        client = genai.Client(api_key=api_key)
        return (client, genai_types)
    except Exception as exc:
        logger.warning("Gemini client init failed: %s", exc)
        return None, "vision_failed"


async def _vision_extract_image_batch(
    images_jpeg: "list[tuple[int, bytes]]",
    batch_label: str,
) -> "tuple[str, Optional[str]]":
    """Send a batch of page images to Gemini Vision in ONE call.

    Each tuple is (page_number_1based, jpeg_bytes). The prompt asks
    Gemini to label its output with the page numbers we passed in so
    we can reassemble the document order even if pages are missing
    or out of order.

    Returns (text, error_status_or_None).
    """
    client_or_err = _get_genai_client()
    if client_or_err[0] is None:
        return "", client_or_err[1]
    client, genai_types = client_or_err

    parts: list = [genai_types.Part.from_text(text=(
        "You are receiving " + str(len(images_jpeg))
        + " image(s) from a medical document. Each image is one page; "
        + "the page numbers are: "
        + ", ".join(str(n) for n, _ in images_jpeg) + ".\n\n"
        + "Extract ALL text from EVERY page. Preserve obvious structure "
          "(headings, bullet lists, tables as markdown). Chinese stays in "
          "Chinese; English stays in English. For each page output a "
          "header `--- Page N ---` (use the page number I gave you), "
          "followed by the page text. If a page is blank or unreadable, "
          "output `--- Page N ---\\n[UNREADABLE]`. Do NOT add preamble, "
          "commentary, or summary -- just the extracted text per page."
    ))]
    for _, jpeg in images_jpeg:
        parts.append(genai_types.Part.from_bytes(
            data=jpeg, mime_type="image/jpeg",
        ))

    contents = [genai_types.Content(role="user", parts=parts)]
    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.5-flash",
            contents=contents,
            config=genai_types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=16_000,
            ),
        )
        text = (response.text or "").strip()
        if not text:
            return "", "vision_failed"
        return text, None
    except Exception as exc:
        logger.warning(
            "Vision batch %s failed: %s", batch_label, exc,
        )
        return "", "vision_failed"


async def _extract_via_vision_per_page(
    raw: bytes, name: str,
    max_pages: int = 30,
    pages_per_batch: int = 5,
    parallelism: int = 3,
    pages_filter: "Optional[list[int]]" = None,
) -> "tuple[dict[int, str], Optional[str]]":
    """Rasterize selected PDF pages to JPEG, batch them to Gemini
    Vision, return a {page_num: extracted_text} dict.

    pages_filter
        Optional list of 1-based page numbers to OCR. When None we
        process the first ``max_pages`` pages (the all-scanned case).
        When provided, ONLY those pages get rasterized + OCR'd — used
        by the hybrid path when pypdf already covered some pages.

    Why per-page instead of feeding the whole PDF as inline_data:
      * Gemini's inline_data has a 20 MB cap; many hospital PDFs
        exceed that, especially scan-heavy ones (the medic's 59-page
        12.8 MB case file we tested with).
      * Batching N pages/call instead of 1/call cuts round-trip
        overhead ~Nx; running batches in parallel cuts wall-clock
        latency another `parallelism`x.
      * Capping at max_pages keeps cost predictable.

    With pages_per_batch=5 + parallelism=3 a 30-page cap finishes in
    ~10-20s in practice (Gemini Flash latency dominated).
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        logger.warning("pypdfium2 missing -- can't rasterize PDF pages")
        return {}, "vision_failed"

    try:
        pdf = pdfium.PdfDocument(io.BytesIO(raw))
    except Exception as exc:
        logger.info("pypdfium2 could not open PDF: %s", exc)
        return {}, "vision_failed"

    n_total = len(pdf)
    # Decide which pages to rasterize.
    if pages_filter is not None:
        # Hybrid mode — only the pages pypdf couldn't extract.
        target_pages = [p for p in pages_filter if 1 <= p <= n_total]
        # Still respect the cap (defensive — the caller MAY have asked
        # for too many).
        target_pages = target_pages[:max_pages]
        truncation_note_pages = (
            len(pages_filter), len(target_pages),
        )
    else:
        # All-scanned mode — first max_pages of the doc.
        target_pages = list(range(1, min(n_total, max_pages) + 1))
        truncation_note_pages = (n_total, len(target_pages))
    if not target_pages:
        return {}, "vision_failed"

    # Rasterize the chosen pages.
    pages_jpeg: list[tuple[int, bytes]] = []
    for page_num in target_pages:
        try:
            img = pdf[page_num - 1].render(scale=2.0).to_pil()  # ~144 dpi
        except Exception as exc:
            logger.debug("page %d render failed: %s", page_num, exc)
            continue
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        pages_jpeg.append((page_num, buf.getvalue()))
    if not pages_jpeg:
        return {}, "vision_failed"

    batches: list[list[tuple[int, bytes]]] = [
        pages_jpeg[i:i + pages_per_batch]
        for i in range(0, len(pages_jpeg), pages_per_batch)
    ]
    logger.info(
        "PDF Vision OCR: %s -- %d pages in %d batches (%d parallel)%s",
        name, len(pages_jpeg), len(batches), parallelism,
        " [hybrid mode]" if pages_filter is not None else "",
    )

    sem = asyncio.Semaphore(parallelism)

    async def _run_batch(idx: int, batch: list[tuple[int, bytes]]):
        async with sem:
            label = f"batch {idx + 1}/{len(batches)} (pages " \
                    f"{batch[0][0]}-{batch[-1][0]})"
            return await _vision_extract_image_batch(batch, label)

    results = await asyncio.gather(
        *[_run_batch(i, b) for i, b in enumerate(batches)],
        return_exceptions=True,
    )

    # Parse Gemini's --- Page N --- response per batch and bucket by
    # page number. Gemini may sometimes mislabel pages, so we re-anchor
    # against the page numbers we sent in each batch.
    out: dict[int, str] = {}
    failures = 0
    import re as _re
    page_header_re = _re.compile(r"--- Page (\d+) ---\s*\n?", _re.IGNORECASE)
    for batch_idx, r in enumerate(results):
        if isinstance(r, BaseException):
            failures += 1
            continue
        text, err = r
        if err is not None or not text:
            failures += 1
            continue
        # Split the batch response on --- Page N --- markers.
        parts = page_header_re.split(text)
        # parts = ['', '1', 'text of page 1', '2', 'text of page 2', ...]
        # Re-zip into (page_num, text) pairs.
        it = iter(parts[1:])
        for page_num_str, body in zip(it, it):
            try:
                pn = int(page_num_str)
            except ValueError:
                continue
            body = body.strip()
            if body and body != "[UNREADABLE]":
                out[pn] = body

    if truncation_note_pages[0] > truncation_note_pages[1]:
        # Caller can inspect the dict; the truncation note is rendered
        # at stitch time by extract_pdf_text. Stash a hint here for
        # logging only.
        logger.info(
            "PDF OCR truncated for %s: %d requested, %d OCR'd",
            name, truncation_note_pages[0], truncation_note_pages[1],
        )
    if not out and failures:
        return {}, "vision_failed"
    return out, None


async def _extract_via_vision_single_image(
    raw: bytes, name: str, mime: str,
) -> "tuple[str, Optional[str]]":
    """Direct image upload (.jpg/.png/.heic/.webp): hand the bytes to
    Gemini Vision verbatim. No rasterization needed."""
    client_or_err = _get_genai_client()
    if client_or_err[0] is None:
        return "", client_or_err[1]
    client, genai_types = client_or_err

    # Normalise HEIC/HEIF to JPEG so Gemini accepts it (Vision API
    # rejects HEIC at the inline_data layer).
    send_mime = mime
    send_bytes = raw
    if mime in ("image/heic", "image/heif"):
        try:
            from PIL import Image
            import pillow_heif  # noqa: F401 (registers the HEIF opener)
            img = Image.open(io.BytesIO(raw))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=88)
            send_mime, send_bytes = "image/jpeg", buf.getvalue()
        except Exception as exc:
            logger.warning("HEIC->JPEG conversion failed for %s: %s", name, exc)
            return "", "vision_failed"

    parts = [
        genai_types.Part.from_text(text=(
            "Extract ALL text from this image (medical document or "
            "report). Preserve structure (headings, bullets, tables as "
            "markdown). Chinese stays Chinese, English stays English. "
            "Output ONLY the extracted text -- no preamble or commentary. "
            "If the image is blank/unreadable, return exactly `[UNREADABLE]`."
        )),
        genai_types.Part.from_bytes(data=send_bytes, mime_type=send_mime),
    ]
    contents = [genai_types.Content(role="user", parts=parts)]
    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.5-flash",
            contents=contents,
            config=genai_types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=16_000,
            ),
        )
        text = (response.text or "").strip()
        if not text or text == "[UNREADABLE]":
            return "", "gemini_marked_unreadable"
        return text[:EXTRACTION_CHAR_BUDGET], None
    except Exception as exc:
        logger.warning("Single-image Vision failed for %s: %s", name, exc)
        return "", "vision_failed"


async def _extract_via_vision(
    raw: bytes, name: str, mime: str = "application/pdf",
) -> "tuple[str, Optional[str]]":
    """Compat shim: returns (concatenated_text, error). Used by callers
    that don't care about per-page granularity. Internally re-stitches
    the per-page dict from ``_extract_via_vision_per_page``.

    For PDFs the per-page result comes back as
    {1: 'page-1 text', 3: 'page-3 text', ...}; we stitch with
    ``--- Page N ---`` headers in numeric order.
    """
    if mime != "application/pdf":
        return await _extract_via_vision_single_image(raw, name, mime)
    pages, err = await _extract_via_vision_per_page(raw, name)
    if err is not None or not pages:
        return "", err or "vision_failed"
    chunks = [
        f"--- Page {n} ---\n{pages[n]}"
        for n in sorted(pages.keys())
    ]
    return "\n\n".join(chunks)[:EXTRACTION_CHAR_BUDGET], None


# ─────────────────────────────────────────────────────────────────────
# Top-level dispatcher
# ─────────────────────────────────────────────────────────────────────


async def extract_pdf_text(
    raw: bytes, name: str, mime: str = "application/pdf",
) -> Tuple[str, str]:
    """Per-page hybrid PDF extraction. Returns (text, status).

    Status is one of:
      * 'text_layer' — pypdf alone covered every page
      * 'vision_ocr' — at least one page needed Gemini Vision (the
                       mixed case ALSO returns this; the UI treats
                       "any Vision involvement" as one badge)
      * 'encrypted'  — PDF is password-protected, can't read
      * 'unreadable' — neither pypdf nor Vision yielded anything

    Strategy
    --------
    1. Per-page pypdf — for each page, try the text layer. Pages
       with >= PER_PAGE_TEXT_LAYER_MIN chars stay as-is.
    2. For pages BELOW the threshold (header-only / scanned image /
       empty), batch them off to Gemini Vision and rasterize ONLY
       those pages.
    3. Stitch the two halves back together in page order. Vision
       pages are marked `--- Page N (per OCR) ---` so the LLM can
       tell which content is OCR'd vs deterministic.

    Why hybrid: a hospital PDF that's e.g. 5 typed pages + 10
    attached scans used to either lose the scans (pypdf success
    threshold passed) OR re-OCR the typed pages (Vision ran on
    everything). Now each page goes through the cheapest path that
    works, and the medic pays for Vision only on the pages that
    actually need it.
    """
    # Phase 1: per-page pypdf
    per_page, err = _extract_per_page_via_pypdf(raw)
    if err == S_ENCRYPTED:
        return "", S_ENCRYPTED
    if err is not None or not per_page:
        # pypdf couldn't even open the file. Try Vision on everything.
        pages_dict, vision_err = await _extract_via_vision_per_page(
            raw, name,
        )
        if pages_dict:
            return _stitch_pages(
                pages_dict, vision_only=True,
            ), S_VISION_OCR
        return "", S_UNREADABLE

    # Identify pages that need Vision (text-layer below threshold).
    text_pages: dict[int, str] = {}
    needs_vision: list[int] = []
    for page_num, t in per_page:
        if len(t) >= PER_PAGE_TEXT_LAYER_MIN:
            text_pages[page_num] = t
        else:
            needs_vision.append(page_num)

    # Phase 2: Vision OCR only the pages that lacked a text layer.
    vision_pages: dict[int, str] = {}
    if needs_vision:
        vision_pages, _v_err = await _extract_via_vision_per_page(
            raw, name,
            pages_filter=needs_vision,
        )

    # Stitch.
    all_page_nums = sorted(set(text_pages) | set(vision_pages))
    if not all_page_nums:
        return "", S_UNREADABLE

    chunks: list[str] = []
    char_budget = EXTRACTION_CHAR_BUDGET
    for pn in all_page_nums:
        if pn in text_pages:
            body = text_pages[pn]
            header = f"--- Page {pn} ---"
        elif pn in vision_pages:
            body = vision_pages[pn]
            header = f"--- Page {pn} (per OCR) ---"
        else:
            continue
        piece = f"{header}\n{body}"
        if len(piece) > char_budget:
            chunks.append(piece[:char_budget])
            chunks.append("\n[note: per-page budget exhausted; "
                          "later pages truncated]")
            break
        chunks.append(piece)
        char_budget -= len(piece)

    # Decide status:
    #   * Vision touched ANY page → 'vision_ocr' (the UI shows 🤖)
    #   * Otherwise pure text-layer → 'text_layer'
    if vision_pages:
        status_ = S_VISION_OCR
    elif text_pages:
        status_ = S_TEXT_LAYER
    else:
        status_ = S_UNREADABLE

    # Note about pages that nobody could extract (pypdf below
    # threshold AND Vision didn't return them either — usually
    # blank pages or rasterize failures).
    requested_vision = set(needs_vision)
    got_vision = set(vision_pages.keys())
    missing = sorted(requested_vision - got_vision)
    if missing:
        chunks.append(
            f"\n[note: {len(missing)} page(s) failed extraction "
            f"(pages {missing[:10]}{'…' if len(missing) > 10 else ''}); "
            f"the medic can re-trigger via the file library UI]"
        )

    full = "\n\n".join(chunks)
    return full, status_


def _stitch_pages(
    pages_dict: "dict[int, str]", *, vision_only: bool,
) -> str:
    """Render a {page_num: text} dict as a single document.
    Helper used by the fallback path when pypdf can't even open the
    file (so every page is Vision)."""
    out: list[str] = []
    total = 0
    for pn in sorted(pages_dict.keys()):
        body = pages_dict[pn]
        header = (
            f"--- Page {pn} (per OCR) ---"
            if vision_only else f"--- Page {pn} ---"
        )
        piece = f"{header}\n{body}"
        if total + len(piece) > EXTRACTION_CHAR_BUDGET:
            out.append(piece[: EXTRACTION_CHAR_BUDGET - total])
            out.append("\n[note: budget exhausted; later pages truncated]")
            break
        out.append(piece)
        total += len(piece)
    return "\n\n".join(out)


# Mime types we route through the single-image Vision path.
_IMAGE_MIMES = frozenset({
    "image/jpeg", "image/jpg", "image/png", "image/webp",
    "image/heic", "image/heif", "image/gif", "image/bmp",
})


async def extract_image_text(
    raw: bytes, name: str, mime: str,
) -> Tuple[str, str]:
    """Direct-image OCR (.jpg / .png / .heic etc). Vision is the ONLY
    path -- there's no text layer to try first. Returns (text, status)
    same shape as ``extract_pdf_text``.
    """
    text, err = await _extract_via_vision_single_image(raw, name, mime)
    if text and len(text.strip()) >= 20:
        return text, S_VISION_OCR
    logger.info(
        "Image OCR failed for %s (%s): vision_err=%s", name, mime, err,
    )
    return "", S_UNREADABLE


# ─────────────────────────────────────────────────────────────────────
# Persistence wrapper for the /reextract endpoint
# ─────────────────────────────────────────────────────────────────────


async def extract_and_persist(
    *,
    user_id: str,
    file_id: str,
    name: str,
    mime: str,
    disk_path: str,
) -> Tuple[str, str]:
    """Run extraction on the on-disk file and write the result back
    into the ``uploads`` row. Returns (text, status).

    Used by:
      * upload-time async extractor (Phase 1 ingestion hook)
      * ``POST /api/v1/chat/files/{file_id}/reextract`` (manual retry)
    """
    p = Path(disk_path)
    if not p.exists():
        logger.warning("extract_and_persist: file missing on disk: %s", p)
        return "", S_UNREADABLE
    try:
        raw = p.read_bytes()
    except OSError as exc:
        logger.warning(
            "extract_and_persist: read failed for %s: %s", p, exc,
        )
        return "", S_UNREADABLE

    name_lc = str(name).lower()
    if mime == "application/pdf" or name_lc.endswith(".pdf"):
        text, status_ = await extract_pdf_text(raw, name, mime)
    elif (
        mime in _IMAGE_MIMES
        or name_lc.endswith((".jpg", ".jpeg", ".png", ".webp",
                             ".heic", ".heif", ".gif", ".bmp"))
    ):
        # F-pdf-perpage-vision — direct image uploads (medic dragged a
        # screenshot of a lab report or an external photo of a paper
        # chart into chat). No text layer exists; the SDK distiller
        # returns 'binary-stub'. Send to Gemini Vision verbatim.
        # Normalise mime when only the filename hints at type.
        if mime not in _IMAGE_MIMES:
            ext_to_mime = {
                ".jpg":  "image/jpeg", ".jpeg": "image/jpeg",
                ".png":  "image/png",  ".webp": "image/webp",
                ".heic": "image/heic", ".heif": "image/heif",
                ".gif":  "image/gif",  ".bmp":  "image/bmp",
            }
            for ext, m in ext_to_mime.items():
                if name_lc.endswith(ext):
                    mime = m
                    break
        text, status_ = await extract_image_text(raw, name, mime)
    else:
        # Non-PDF non-image: docx / xlsx / utf-8 text / etc. Use the
        # SDK distiller's text-mode extractor.
        try:
            from nexus_core.distiller import extract_text
            b64 = base64.b64encode(raw).decode("ascii")
            text, src = extract_text(name, mime, None, b64)
            status_ = S_TEXT_LAYER if (text and src != "binary-stub") else S_UNREADABLE
        except Exception as exc:
            logger.warning("non-pdf extract_text failed: %s", exc)
            text, status_ = "", S_UNREADABLE

    # Persist back into uploads.
    try:
        from nexus_server.database import get_db_connection
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE uploads "
                "   SET extracted_text = ?, "
                "       text_extraction_status = ? "
                " WHERE user_id = ? AND file_id = ?",
                (text or "", status_, user_id, file_id),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("persist after extract failed: %s", exc)

    return text or "", status_
