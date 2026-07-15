"""SAM (Segment Anything) auto-segmentation — #145.

Meta's Segment Anything Model (SAM) takes a point or bounding box
prompt + an image, and produces a precise polygon. We use the ONNX
exported encoder/decoder so it runs on CPU without GPU and without
Python's torch dependency (saves ~2 GB).

Model files (~75 MB encoder + ~10 MB decoder for ViT-B variant)
are NOT bundled into the .dmg. They download on first call into
``$RUNE_HOME/.nexus/models/sam/`` and stay there. This keeps the
desktop install lean and lets the medic opt into the download
when they actually need AI assistance.

Public surface
==============

  * :func:`segment_from_point(png_bytes, x, y)` — returns a polygon
    [[x, y], ...] in image pixel coords for the structure the
    medic clicked.
  * :func:`segment_from_box(png_bytes, box)` — same but with a
    coarse [x1, y1, x2, y2] bounding box prompt.
  * :func:`is_available()` — fast check: are the model files
    present + onnxruntime installed? Used by the HTTP endpoint to
    return a friendly "model not yet downloaded" response without
    crashing.

Failure modes
=============

When ``is_available()`` returns False the HTTP endpoint should
surface the actionable next step (run download command or wait).
We never block the import of this module on the model files
existing — that would prevent server boot every time the medic
hadn't yet downloaded SAM.
"""

from __future__ import annotations

import io
import logging
import os
import urllib.request
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── Model file locations ──────────────────────────────────────────────


# Meta's official ONNX-exported SAM ViT-B (smallest variant). Public
# Hugging Face mirror — no auth needed.
SAM_ENCODER_URL = (
    "https://huggingface.co/spaces/jbrinkma/segment-anything-onnx/"
    "resolve/main/vit_b_encoder.onnx"
)
SAM_DECODER_URL = (
    "https://huggingface.co/spaces/jbrinkma/segment-anything-onnx/"
    "resolve/main/vit_b_decoder.onnx"
)


def _models_dir() -> Path:
    rune_home = Path(os.getenv("RUNE_HOME") or str(Path.home() / ".rune"))
    d = rune_home / ".nexus" / "models" / "sam"
    d.mkdir(parents=True, exist_ok=True)
    return d


def encoder_path() -> Path:
    return _models_dir() / "sam_vit_b_encoder.onnx"


def decoder_path() -> Path:
    return _models_dir() / "sam_vit_b_decoder.onnx"


# ── Availability ──────────────────────────────────────────────────────


def is_available() -> tuple[bool, str]:
    """Returns ``(available, reason_if_not)``.

    Three conditions for True:
      1. onnxruntime installed (server venv)
      2. encoder + decoder ONNX files present on disk

    The HTTP endpoint surfaces the reason string so the medic
    knows exactly which knob to turn.
    """
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False, (
            "onnxruntime not installed. Run "
            "`pip install onnxruntime` in the server's venv."
        )
    if not encoder_path().exists():
        return False, (
            "SAM encoder model not downloaded yet. Call "
            "/api/v1/dicom/sam/download to fetch (~75 MB)."
        )
    if not decoder_path().exists():
        return False, (
            "SAM decoder model not downloaded yet. Call "
            "/api/v1/dicom/sam/download to fetch (~10 MB)."
        )
    return True, ""


# ── Download (called on-demand from desktop button) ───────────────────


def ensure_models_downloaded(progress_cb=None) -> None:
    """Download SAM ONNX files if missing.

    ``progress_cb`` (optional): callable taking ``(bytes_done,
    bytes_total, filename)`` for UI progress bars. Pass ``None`` to
    download silently.
    """
    for url, target in [
        (SAM_ENCODER_URL, encoder_path()),
        (SAM_DECODER_URL, decoder_path()),
    ]:
        if target.exists() and target.stat().st_size > 1_000_000:
            continue  # already there
        logger.info("Downloading SAM model: %s → %s", url, target)
        # Stream to disk so a 75 MB download doesn't materialise in
        # memory. Resume not implemented — full re-download on failure.
        tmp = target.with_suffix(".onnx.partial")
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length", "0") or 0)
                done = 0
                with tmp.open("wb") as fh:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        if progress_cb:
                            try:
                                progress_cb(done, total, target.name)
                            except Exception as exc:  # noqa: BLE001
                                logger.debug("progress callback failed: %s", exc)
            tmp.replace(target)
        except Exception as e:  # noqa: BLE001
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError as exc:
                    logger.debug("removing partial download failed: %s", exc)
            raise RuntimeError(
                f"SAM model download failed for {target.name}: {e}",
            ) from e


# ── Inference ─────────────────────────────────────────────────────────


_encoder_session = None
_decoder_session = None


def _load_sessions():
    """Lazy-load ONNX sessions. Both are CPU-only by default — SAM
    ViT-B runs ~1-2 sec per encode on a modern Mac CPU which is
    fine for the click-and-wait UX."""
    global _encoder_session, _decoder_session
    if _encoder_session is not None and _decoder_session is not None:
        return _encoder_session, _decoder_session
    import onnxruntime as ort
    providers = ["CPUExecutionProvider"]
    _encoder_session = ort.InferenceSession(
        str(encoder_path()), providers=providers,
    )
    _decoder_session = ort.InferenceSession(
        str(decoder_path()), providers=providers,
    )
    return _encoder_session, _decoder_session


def _preprocess_image(png_bytes: bytes) -> tuple["np.ndarray", tuple[int, int]]:
    """Decode PNG → SAM-ready 1024×1024 float32 tensor.

    Returns ``(input_tensor, (original_w, original_h))``. SAM
    expects RGB input normalised with ImageNet mean/std; we feed
    the grayscale DICOM render as R=G=B since medical images
    don't have meaningful colour channels.
    """
    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    orig_w, orig_h = img.size

    # SAM expects 1024-on-longest-side then pad to 1024×1024
    longest = max(orig_w, orig_h)
    scale = 1024 / longest
    new_w, new_h = int(orig_w * scale), int(orig_h * scale)
    img_resized = img.resize((new_w, new_h), Image.BILINEAR)

    arr = np.asarray(img_resized, dtype=np.float32)  # H, W, 3
    # ImageNet normalisation
    mean = np.array([123.675, 116.28, 103.53], dtype=np.float32)
    std = np.array([58.395, 57.12, 57.375], dtype=np.float32)
    arr = (arr - mean) / std
    # Pad to 1024×1024
    pad_h = 1024 - new_h
    pad_w = 1024 - new_w
    arr = np.pad(
        arr, ((0, pad_h), (0, pad_w), (0, 0)),
        mode="constant", constant_values=0,
    )
    # NHWC → NCHW
    arr = arr.transpose(2, 0, 1)[None, ...]
    return arr, (orig_w, orig_h)


def _mask_to_polygon(mask: "np.ndarray") -> list[list[float]]:
    """Boolean mask → polygon points using marching-squares.

    Returns the largest contour (single biggest connected region)
    as [[x, y], ...] in original-image pixel coordinates. Empty
    list when the mask is empty.
    """
    # Find boundary via skimage's contour finder. skimage is a
    # ~30 MB transitive dependency of many ML libs; if absent we
    # fall back to a simple "find non-zero pixel cluster bbox"
    # approximation.
    try:
        from skimage import measure
        contours = measure.find_contours(mask.astype(float), level=0.5)
    except ImportError:
        # Fallback: bbox of mask as a rectangle polygon
        ys, xs = mask.nonzero()
        if len(xs) == 0:
            return []
        x0, x1 = float(xs.min()), float(xs.max())
        y0, y1 = float(ys.min()), float(ys.max())
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    if not contours:
        return []
    # Largest contour by point count = most likely the target structure
    contour = max(contours, key=len)
    # skimage returns (row, col) → we want (x=col, y=row)
    # Also subsample: medical structures rarely need >200 points
    step = max(1, len(contour) // 200)
    return [[float(c[1]), float(c[0])] for c in contour[::step]]


def _run_sam_inference(
    png_bytes: bytes,
    *,
    point: Optional[tuple[float, float]] = None,
    box: Optional[tuple[float, float, float, float]] = None,
) -> list[list[float]]:
    """Core inference: image → polygon."""
    import numpy as np
    enc_sess, dec_sess = _load_sessions()
    img_tensor, (orig_w, orig_h) = _preprocess_image(png_bytes)
    # Encoder: produces 256-channel embedding
    image_embedding = enc_sess.run(None, {"images": img_tensor})[0]

    # Build prompt arrays
    if box is not None:
        # box format: [x1, y1, x2, y2] in original pixels
        scale = 1024 / max(orig_w, orig_h)
        coords = np.array([
            [box[0] * scale, box[1] * scale],
            [box[2] * scale, box[3] * scale],
        ], dtype=np.float32)
        labels = np.array([2, 3], dtype=np.float32)  # SAM box codes
    elif point is not None:
        scale = 1024 / max(orig_w, orig_h)
        coords = np.array([
            [point[0] * scale, point[1] * scale],
            [0.0, 0.0],  # dummy padding required by ONNX schema
        ], dtype=np.float32)
        labels = np.array([1, -1], dtype=np.float32)  # foreground + pad
    else:
        raise ValueError("Either point or box must be provided")

    # Decoder inputs
    onnx_inputs = {
        "image_embeddings": image_embedding,
        "point_coords":     coords[None, ...],
        "point_labels":     labels[None, ...],
        "mask_input":       np.zeros((1, 1, 256, 256), dtype=np.float32),
        "has_mask_input":   np.zeros(1, dtype=np.float32),
        "orig_im_size":     np.array([orig_h, orig_w], dtype=np.float32),
    }
    masks, scores, _ = dec_sess.run(None, onnx_inputs)
    # SAM emits 3 candidate masks per prompt; pick the highest-score one
    best_idx = int(np.argmax(scores[0]))
    mask = masks[0, best_idx] > 0  # threshold
    return _mask_to_polygon(mask)


def segment_from_point(
    png_bytes: bytes, x: float, y: float,
) -> list[list[float]]:
    """Point-prompt segmentation. Medic clicks; SAM grows the click
    into a precise polygon of the underlying structure."""
    return _run_sam_inference(png_bytes, point=(x, y))


def segment_from_box(
    png_bytes: bytes, box: tuple[float, float, float, float],
) -> list[list[float]]:
    """Box-prompt segmentation. Medic drags a coarse box around the
    structure; SAM tightens to the actual boundary."""
    return _run_sam_inference(png_bytes, box=box)
