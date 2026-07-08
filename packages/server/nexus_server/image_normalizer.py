"""#160 — Image format normalizer for Gemini-incompatible formats.

Gemini's vision endpoint accepts a narrow set of MIME types (officially:
JPEG, PNG, WebP, HEIC, HEIF, GIF). Uploads outside that set silently
fail through the rest of the pipeline:

    100 MB TIFF → BASE64_INLINE_CAP_BYTES (50MB) skip → content_base64=None
                → image branch (requires content_base64) doesn't fire
                → falls to distill_attachment text path
                → TIFF isn't text → "[empty attachment]" stub
                → agent says "the file you uploaded is empty"

Same empty-failure mode the DICOM fix (#152) addressed, but for
ordinary medical / pathology / RAW image uploads instead of archives.

Fix design — mirror the DICOM prerender:

  1. At upload time, detect formats Gemini doesn't accept (TIFF, RAW,
     and very large HEIC/HEIF where the conversion isn't free).
  2. Transcode to JPEG (downsized to max-side ≤ 2048 px so we don't
     ship 100 MB to Gemini's vision model, which would 413 anyway).
  3. Persist the JPEG copy alongside the original. The original stays
     on disk so the medic can still download the lossless source.
  4. When chat-time resolution sees an upload with a normalized copy,
     it swaps the mime to image/jpeg + serves the normalized bytes.

Multi-page TIFF (typical pathology / scanned reports): take the first
page. We could render a grid of all pages for richer context, but
that's a separate task (#163-D when it lands).

This module is INTENTIONALLY decoupled from the DICOM prerender —
DICOM uses pydicom, this uses plain Pillow. The two paths are exercised
by different upload types and share nothing but utility patterns.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────


# MIME / extension hits that trigger transcoding. We intentionally do
# NOT transcode formats Gemini already handles cleanly (jpeg, png,
# webp, gif) — that would be wasted work + lossy round-trip.
NORMALIZE_MIMES = {
    "image/tiff", "image/tif",
    "image/x-canon-cr2", "image/x-canon-cr3",  # Canon RAW
    "image/x-nikon-nef",                       # Nikon RAW
    "image/x-sony-arw",                        # Sony RAW
    "image/x-adobe-dng",                       # Adobe DNG (universal RAW)
    "image/x-portable-pixmap",                 # PPM
    "image/bmp",                               # BMP — accepted by Gemini
                                               # but expensive to ship
                                               # uncompressed
}
NORMALIZE_EXTS = {
    ".tif", ".tiff",
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raw",
    ".ppm", ".pgm", ".pbm", ".pnm",
    ".bmp",
}

# Target max-side for the transcoded JPEG. Gemini's vision encoder
# downsamples to ~768 internally for most images; sending 4K wastes
# upload bandwidth + tokens. 2048 px keeps clinically-relevant
# detail (matches what radiologists call "fit to viewport at 1×")
# while staying under Gemini's per-image cap (3072 px short side).
JPEG_TARGET_MAX_SIDE = 2048

# Quality 88: visually lossless on grayscale + low-color medical
# imagery, but ~10% smaller file vs 95.
JPEG_QUALITY = 88


# Public status strings — mirror DICOM_STATUS_* convention.
NORM_STATUS_NOT_APPLICABLE = "not_applicable"   # not a normalizable format
NORM_STATUS_CONVERTED      = "converted"        # JPEG copy saved successfully
NORM_STATUS_FAILED         = "failed"           # detected but PIL choked


# ── Public API ───────────────────────────────────────────────────────


def looks_normalizable(name: str, mime: str) -> bool:
    """Cheap pre-check: does the upload look like a format Gemini
    can't handle (so we should transcode)?

    Returns True when EITHER the MIME or the filename extension hits
    our blocklist. We accept both because:
      * The desktop's GuessMime falls back to application/octet-stream
        on uncommon extensions (RAW, DNG), making the MIME alone
        useless.
      * Some clients send wrong MIME (e.g. image/jpeg for a .tif from
        a quick rename) — extension alone catches that.
      * The name may have arrived RFC 2047 encoded-word (CJK
        filenames) — files.py decodes those before calling us,
        but if anything bypasses that path we ALSO try a permissive
        decode + substring match below as a last-ditch detection.
    """
    n_raw = name or ""
    nl = n_raw.lower()
    ml = (mime or "").lower()
    if ml in NORMALIZE_MIMES:
        return True
    suffix = ""
    if "." in nl:
        suffix = "." + nl.rsplit(".", 1)[-1]
    if suffix in NORMALIZE_EXTS:
        return True
    # Defensive: try decoding any RFC 2047 encoded-word chunks
    # ourselves so a missed upstream decode doesn't drop the upload
    # into the silent-empty path. Costs ~microseconds and only runs
    # when the literal substring check already failed.
    if "=?" in n_raw and "?=" in n_raw:
        try:
            import email.header as _eh
            parts = _eh.decode_header(n_raw)
            decoded = "".join(
                p.decode(c or "utf-8", errors="replace")
                if isinstance(p, bytes) else p
                for p, c in parts
            ).lower()
            if any(decoded.endswith(ext) or ext in decoded
                   for ext in NORMALIZE_EXTS):
                return True
        except Exception:  # noqa: BLE001
            pass
    # Last resort: scan the literal lowered name for any known
    # extension as a substring (catches uncommon path-style names).
    for ext in NORMALIZE_EXTS:
        if ext in nl:
            return True
    return False


def transcode_to_jpeg(
    *,
    source_path: Path,
    dest_path: Path,
    max_side: int = JPEG_TARGET_MAX_SIDE,
    quality: int = JPEG_QUALITY,
) -> dict:
    """Read ``source_path`` (whatever format Pillow can decode),
    downscale so longest side ≤ ``max_side``, and write JPEG to
    ``dest_path``.

    Multi-page TIFF: takes the first page. Pillow's TIFF reader
    exposes pages via ``Image.seek(n)``; we don't iterate past 0
    here because PET pathology / report scans use page 0 for the
    primary view and putting all of a 50-page report into a single
    JPEG isn't useful for a vision LLM anyway.

    Returns a dict the caller stamps into the uploads row::

        {
          "status":           one of NORM_STATUS_* strings,
          "src_bytes":        size of original file (for logging),
          "out_bytes":        size of transcoded JPEG,
          "out_width":        pixel width of output,
          "out_height":       pixel height of output,
          "original_mode":    PIL mode of the source (RGB / L / I;16 / ...),
          "page_count":       number of pages in source (1 for non-TIFF),
          "error":            short description when status="failed",
        }

    Never raises — failures become structured ``status="failed"``
    so the upload route can keep the upload alive (the medic still
    has the original on disk).
    """
    out: dict = {
        "status":        NORM_STATUS_FAILED,
        "src_bytes":     0,
        "out_bytes":     0,
        "out_width":     0,
        "out_height":    0,
        "original_mode": "",
        "page_count":    1,
        "error":         "",
    }
    try:
        out["src_bytes"] = source_path.stat().st_size
    except OSError:
        pass

    try:
        from PIL import Image, ImageOps
    except ImportError as e:
        out["error"] = f"PIL unavailable: {e}"
        return out

    # Read the source. For TIFF + multi-page formats this returns
    # page 0 by default. Pillow's "draft" mode lets it skip pixel
    # decoding for some formats (JPEG, mainly) when we know the
    # target size — not always available for TIFF but harmless to
    # try.
    try:
        img = Image.open(source_path)
        out["original_mode"] = img.mode
        # Multi-page detection (TIFF, GIF, animated WebP).
        try:
            from PIL.Image import Image as _PILImage
            if hasattr(img, "n_frames"):
                out["page_count"] = int(img.n_frames)
        except Exception:  # noqa: BLE001
            pass
        # EXIF-aware orientation correction. Phone photos in HEIC /
        # JPG often have an EXIF rotate tag that, if ignored,
        # displays the image sideways. Apply once.
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:  # noqa: BLE001
            pass

        # 16-bit grayscale TIFF (pathology, medical) → JPEG needs 8-bit.
        # Use auto-contrast so the JPEG conversion doesn't crush
        # everything to black.
        if img.mode in ("I", "I;16", "I;16B", "I;16L", "F"):
            import numpy as np
            arr = np.asarray(img, dtype=np.float32)
            lo, hi = float(np.percentile(arr, 2)),  float(np.percentile(arr, 98))
            if hi - lo < 1e-3:
                hi = lo + 1.0
            arr = np.clip((arr - lo) / (hi - lo) * 255.0, 0, 255).astype("uint8")
            img = Image.fromarray(arr, mode="L")
        # Pillow can't write JPEG from RGBA / P modes — coerce.
        if img.mode in ("RGBA", "LA"):
            # Composite alpha onto white so transparent corners look
            # natural instead of black.
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode == "P":
            img = img.convert("RGB")
        elif img.mode == "L":
            pass    # JPEG handles grayscale natively
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Downscale if needed (preserve aspect ratio).
        w, h = img.size
        if max(w, h) > max_side:
            img.thumbnail((max_side, max_side), Image.LANCZOS)

        # Write atomically: write to .tmp then rename. Avoids a
        # half-written JPEG being readable by the chat-time resolver
        # if the server crashes mid-write.
        tmp = dest_path.with_suffix(dest_path.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        img.save(
            tmp, format="JPEG", quality=quality,
            optimize=True, progressive=True,
        )
        tmp.replace(dest_path)

        out["out_bytes"] = dest_path.stat().st_size
        out["out_width"] = img.width
        out["out_height"] = img.height
        out["status"] = NORM_STATUS_CONVERTED
        logger.info(
            "image_normalizer: %s → %s (%d B → %d B, %dx%d, mode=%s, "
            "pages=%d)",
            source_path.name, dest_path.name,
            out["src_bytes"], out["out_bytes"],
            out["out_width"], out["out_height"],
            out["original_mode"], out["page_count"],
        )
    except Exception as e:  # noqa: BLE001
        out["status"] = NORM_STATUS_FAILED
        out["error"] = f"{type(e).__name__}: {e}"
        logger.warning(
            "image_normalizer: failed on %s: %s",
            source_path.name, out["error"],
        )
        # Best-effort cleanup of any partial tmp file.
        try:
            tmp = dest_path.with_suffix(dest_path.suffix + ".tmp")
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    return out


def derive_normalized_path(disk_path: Path) -> Path:
    """Conventional sibling path for the transcoded copy.

    ``foo.tif`` → ``foo.tif.normalized.jpg``

    Keeping the original filename + a stable suffix means the chat-time
    resolver can find the normalized copy without an extra DB column
    if the row gets dropped (defensive layering — we also store the
    explicit path in uploads.image_normalized_path, but reconstruction
    works if that's missing).
    """
    return disk_path.with_suffix(disk_path.suffix + ".normalized.jpg")
