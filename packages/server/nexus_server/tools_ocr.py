"""OCR tool — #126.

A targeted text-extraction tool for uploaded image attachments.

Why this exists when we already have vision multimodal (#123)
=============================================================
Vision is the default path: when the user pastes a screenshot the
bytes ride straight to Gemini as an ``inline_data`` part and the
model can answer questions about layout, colours, figures, hand-
writing, anything that's *visual*. So why also ship an OCR tool?

Two narrow but real cases where pure text extraction wins:

  1. **Cost / token efficiency on text-heavy images.** A 4-K screenshot
     of a Notion page is ~600 image tokens to Gemini but the actual
     content is maybe 200 text tokens. When the user just wants
     "summarize what this page says", running OCR first saves tokens
     across many subsequent turns of follow-up Q&A about the same
     content.

  2. **Precision text extraction.** Vision models occasionally
     "interpret" rather than transcribe — they correct misspellings,
     fix grammar, soften casing. For things like verification codes,
     license numbers, or "give me the exact error message from this
     log screenshot", tesseract's literal output is more reliable.

The tool surface is intentionally narrow: it takes a ``file_id`` of
a previously-uploaded image and returns its OCR'd text. The agent
decides when to call it (description below pushes vision as the
default and OCR as the niche).

Dependencies
============
Uses ``pytesseract`` which wraps the system's ``tesseract`` binary.
Both are optional installs:

  * Server-side Python: ``pip install pytesseract pillow``
  * System binary: ``brew install tesseract`` (macOS) /
    ``apt install tesseract-ocr`` (Debian)

When either is missing the tool returns a friendly error suggesting
vision (which always works), so the agent can fall back gracefully
without crashing the turn.
"""

from __future__ import annotations

import logging
from typing import Optional

from nexus_core.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


# Cache the import-probe result so we don't pay a 50 ms ImportError
# round-trip on every single tool call (which would matter for
# multi-step OCR-then-summarise flows).
_OCR_AVAILABLE: Optional[bool] = None
_OCR_ERROR: Optional[str] = None


def _probe_ocr() -> tuple[bool, Optional[str]]:
    """Check whether pytesseract + the system binary are usable.

    Returns ``(available, error_hint)``. Cached after first call.
    """
    global _OCR_AVAILABLE, _OCR_ERROR
    if _OCR_AVAILABLE is not None:
        return _OCR_AVAILABLE, _OCR_ERROR

    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as e:
        _OCR_AVAILABLE = False
        _OCR_ERROR = (
            f"OCR backend missing — {e.name} not installed. "
            "Run `pip install pytesseract pillow` server-side. "
            "Vision still works without OCR (call delegate or just "
            "describe the image directly)."
        )
        return _OCR_AVAILABLE, _OCR_ERROR

    # Probe the underlying tesseract binary by calling --version.
    # pytesseract.image_to_string raises TesseractNotFoundError on
    # missing binary but only when actually called; do an upfront
    # check so we can give a better message at tool registration time
    # if needed (e.g. for fresh installs without `brew install
    # tesseract`).
    try:
        import pytesseract
        _ = pytesseract.get_tesseract_version()
    except Exception as e:  # noqa: BLE001
        _OCR_AVAILABLE = False
        _OCR_ERROR = (
            f"Tesseract binary not found — {e}. "
            "Install with `brew install tesseract` (macOS) or "
            "`apt install tesseract-ocr` (Debian). "
            "Vision still works without OCR."
        )
        return _OCR_AVAILABLE, _OCR_ERROR

    _OCR_AVAILABLE = True
    _OCR_ERROR = None
    return _OCR_AVAILABLE, _OCR_ERROR


class OcrImageTool(BaseTool):
    """OCR an uploaded image attachment to plain text.

    The agent should reach for this only when it specifically needs
    LITERAL text extraction — verification codes, exact error
    messages, content for a follow-up that doesn't need pixel
    context. For general "what's in this image" questions, vision
    multimodal already sees the picture and answers natively (no
    tool call needed).
    """

    def __init__(self, user_id: str):
        self._user_id = user_id

    @property
    def name(self) -> str:
        return "ocr_image"

    @property
    def description(self) -> str:
        avail, err = _probe_ocr()
        suffix = (
            "OCR backend ready (pytesseract + tesseract binary detected)."
            if avail else
            f"⚠ OCR backend not ready: {err}"
        )
        return (
            "Extract plain text from an uploaded image via OCR "
            "(tesseract). USE THIS ONLY for narrow text-extraction "
            "needs:\n"
            "\n"
            "  - Verification codes, license numbers, exact strings "
            "    that vision might paraphrase.\n"
            "  - Long-form text screenshots (Notion pages, blog "
            "    posts, code listings) where you want plain text for "
            "    subsequent processing.\n"
            "\n"
            "DO NOT use OCR for:\n"
            "  - Describing an image (`what's in this picture` etc.) — "
            "    you can already SEE the image natively via vision "
            "    multimodal. Just answer.\n"
            "  - Layout, colours, figures, charts — OCR drops all of "
            "    those; you'd lose the very thing the user wants.\n"
            "\n"
            f"{suffix}\n"
            "\n"
            "Returns raw OCR text. Pass it to your next reasoning step."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": (
                        "ID of the uploaded image file to OCR. Get this "
                        "from the [CONTEXT — FILES YOU'VE PROCESSED BEFORE] "
                        "block or the chat's attachment chips."
                    ),
                },
                "language": {
                    "type": "string",
                    "description": (
                        "Tesseract language code (e.g. 'eng', 'chi_sim', "
                        "'chi_tra+eng'). Default 'eng+chi_sim' covers most "
                        "of our user base. Use '+'-separated codes for "
                        "multilingual images."
                    ),
                    "default": "eng+chi_sim",
                },
            },
            "required": ["file_id"],
        }

    async def execute(
        self,
        file_id: str = "",
        language: str = "eng+chi_sim",
        **kwargs,
    ) -> ToolResult:
        file_id = (file_id or "").strip()
        if not file_id:
            return ToolResult(
                success=False, error="`file_id` is required.",
            )

        avail, err = _probe_ocr()
        if not avail:
            return ToolResult(success=False, error=err or "OCR unavailable")

        try:
            from nexus_server import files as files_mod
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                success=False, error=f"Files module unavailable: {e}",
            )

        # Resolve the file by id, scoped to this user.
        rows = files_mod.resolve_files(self._user_id, [file_id])
        if not rows:
            return ToolResult(
                success=False,
                error=(
                    f"File {file_id!r} not found for this user. "
                    "Check the attachment chip / files listing for "
                    "the correct id."
                ),
            )
        row = rows[0]
        mime = row.get("mime") or ""
        if not mime.startswith("image/"):
            return ToolResult(
                success=False,
                error=(
                    f"File {row.get('name')!r} is not an image "
                    f"(mime={mime!r}). OCR only works on image MIME "
                    "types. For PDFs / text docs use "
                    "read_uploaded_file instead."
                ),
            )

        disk_path = row.get("disk_path")
        if not disk_path:
            return ToolResult(
                success=False, error="File missing disk_path; storage broken?",
            )

        try:
            import pytesseract
            from PIL import Image
        except ImportError as e:
            # Should be caught by _probe_ocr already; defensive
            # double-check in case the cache stales out.
            return ToolResult(
                success=False, error=f"OCR backend missing: {e}",
            )

        try:
            with Image.open(disk_path) as img:
                # Apply minimal preprocessing: convert to RGB (PNG
                # alpha channel confuses tesseract on some builds)
                # and let it auto-orient. We deliberately don't do
                # binarisation / scaling here — tesseract 5 handles
                # most modern screenshots fine, and aggressive
                # preprocessing makes hand-written text worse.
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                text = pytesseract.image_to_string(img, lang=language)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "ocr_image crashed for user %s, file %s: %s",
                self._user_id, file_id, e,
            )
            return ToolResult(
                success=False,
                error=(
                    f"OCR failed: {e}. The image may be corrupted, "
                    "or the language pack {language!r} may not be "
                    "installed. Try the default `eng` language."
                ),
            )

        # Trim trailing whitespace blocks (tesseract emits a lot of
        # blank lines from PDF-derived images). Keep internal
        # spacing — useful for code listings and tables.
        text = (text or "").strip()
        if not text:
            return ToolResult(
                output=(
                    f"[OCR returned empty text for {row.get('name')!r}. "
                    "The image may not contain readable text, or it's "
                    "in a script tesseract can't read. Try vision "
                    "directly — describe the image and the model will "
                    "see it natively.]"
                ),
            )

        return ToolResult(
            output=(
                f"[OCR of {row.get('name')!r}, "
                f"language={language}, {len(text)} chars]\n"
                f"{text}"
            ),
        )


def register_ocr_tools(twin, user_id: str) -> None:
    """Register the OCR tool onto the given twin.

    Tool registration is best-effort — when pytesseract / tesseract
    isn't installed the tool still registers but its execute() will
    return a friendly "OCR unavailable" error if invoked. This is
    intentional: the agent's prompt mentions OCR as an option, so
    the agent should ALWAYS see the tool exists, even if calling
    it will fail. That way the agent knows the failure mode is "no
    backend" rather than "no such tool".
    """
    twin.register_tool(OcrImageTool(user_id=user_id))
    avail, err = _probe_ocr()
    if avail:
        logger.info("OCR tool registered for user %s (backend ready)", user_id)
    else:
        logger.info(
            "OCR tool registered for user %s (backend not ready: %s)",
            user_id, err,
        )
