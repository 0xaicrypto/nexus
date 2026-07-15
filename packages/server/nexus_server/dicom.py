"""DICOM medical-imaging parser + renderer — #140.

The desktop uploads .dcm singles or zip packages via the same
/files/upload route as any other attachment. This module:

  1. Detects whether an upload is DICOM (single instance, archive,
     or DICOMDIR-rooted directory).
  2. Parses the bytes into structured studies + series + slices.
  3. Renders three pre-computed views that the rest of Nexus can
     feed to vision LLMs / show to the medic:

       * single slice (axial), with caller-chosen window L/W
       * MIP (max intensity projection along z) — one image
         summarising the whole volume
       * 4×4 thumbnail grid — sampled across the series

This is the LAYER 1 of the DICOM stack. Layer 2 (#141) is a
DICOM-aware caption distiller that feeds the rendered PNGs into
Gemini vision plus the structured tags. Layer 3 (#142) is the
desktop viewer (slider + window switch + Send-to-agent).

Why pre-rendered PNGs rather than streaming raw DICOM to the
client: keeps the client simple (no DICOM toolkit), lets the
agent's vision call see the exact pixels the medic sees, and
avoids re-uploading 200 MB volumes to Gemini for every turn.

Dependencies (already in pyproject.toml ``[project.optional-dependencies]
medical-imaging`` after this PR): pydicom, pylibjpeg-libjpeg,
pylibjpeg-openjpeg, numpy, pillow.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import sqlite3
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────


# DICOM-standard 128-byte preamble + 4-byte "DICM" magic at offset 128
# is the canonical detector. We don't trust extensions alone because
# PACS-exported files often drop the .dcm suffix.
DICOM_MAGIC_OFFSET = 128
DICOM_MAGIC = b"DICM"

# Cap on slices we'll keep in memory at once. A typical chest CT is
# 200-500 instances; a high-res cardiac CT can be 1000+. We render
# everything but warn at ingest time so the medic knows their
# 600 MB upload is going to take a while. The hard ceiling protects
# the server from a malicious 100k-instance zip bomb.
MAX_INSTANCES_PER_SERIES = 2000

# Default windowing presets when the DICOM file's WindowCenter /
# WindowWidth tags are missing or unhelpful. Keyed by
# (Modality, preset_name). Lung CT especially needs these — many
# PACS exports strip the windowing tags expecting the viewer to
# pick.
DEFAULT_WINDOWS = {
    ("CT", "lung"):        (-600, 1500),   # WL, WW — air-rich
    ("CT", "mediastinum"):  (40, 400),     # soft-tissue
    ("CT", "bone"):         (400, 1800),   # cortical bone
    ("CT", "brain"):        (40, 80),      # narrow window
    ("CT", "default"):      (40, 400),
    ("MR", "default"):      (200, 400),    # MR pixel ranges vary wildly
    ("DX", "default"):      (None, None),  # X-ray: use full range
    ("CR", "default"):      (None, None),  # computed radiography
    # PET (nuclear medicine) — pixel values after RescaleSlope are in
    # SUV-like counts (typical range 0 - 10), nothing like HU. Setting
    # explicit WL/WW here would be guesswork for any given scanner /
    # tracer, so we always go through the percentile-based auto path
    # (None,None triggers _window_to_uint8's salvage branch below).
    ("PT", "default"):      (None, None),
    ("NM", "default"):      (None, None),
}


# ── Detection ─────────────────────────────────────────────────────────


def looks_like_dicom_bytes(data: bytes) -> bool:
    """Cheap magic-byte check. Doesn't read the whole file."""
    if len(data) < DICOM_MAGIC_OFFSET + 4:
        return False
    return data[DICOM_MAGIC_OFFSET:DICOM_MAGIC_OFFSET + 4] == DICOM_MAGIC


def looks_like_dicom_archive(zip_path: Path) -> bool:
    """Open the zip, find one DICOM-shaped entry, magic check.

    PET-CT and similar multi-modality exports often nest deep
    (PATIENT/STUDY/SERIES/IMG0001.dcm) and may put hundreds of
    directory entries before the first pixel data, so we can't just
    probe the first N file entries blindly. Strategy:

      1. DICOMDIR at any depth → definitely DICOM.
      2. Files with .dcm extension → probe these first (cheapest +
         most reliable signal). One DICM magic match = win.
      3. Files with no extension AND non-empty body → typical PACS
         export style where filenames are SOPInstanceUIDs ("1.2.156..").
         Probe up to 50 of these for the magic.
      4. Anything else with bytes → last-ditch scan, up to 50 entries.

    Returns False only when none of the above finds DICM magic — at
    which point this really isn't a medical archive.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            if not names:
                return False

            # 1. DICOMDIR-rooted archive
            for n in names:
                if Path(n).name.upper() == "DICOMDIR":
                    return True

            # Filter macOS Finder cruft so AppleDouble metadata files
            # (._foo.dcm) don't poison detection — they have .dcm
            # extension but no DICM magic, and zip exports made on macOS
            # ship them by default. Same for .DS_Store sidecars.
            non_dirs = [
                n for n in names
                if not n.endswith("/")
                and not Path(n).name.startswith("._")
                and Path(n).name != ".DS_Store"
            ]
            # 2. Extension-tagged DICOM
            dcm_candidates = [n for n in non_dirs if n.lower().endswith(".dcm")]
            for n in dcm_candidates[:5]:
                try:
                    with zf.open(n) as fh:
                        head = fh.read(DICOM_MAGIC_OFFSET + 4)
                        if looks_like_dicom_bytes(head):
                            return True
                except Exception:  # noqa: BLE001
                    continue

            # 3. No-extension entries (PACS SOPInstanceUID style filenames)
            no_ext = [
                n for n in non_dirs
                if not Path(n).suffix and len(Path(n).name) > 0
            ]
            for n in no_ext[:50]:
                try:
                    with zf.open(n) as fh:
                        head = fh.read(DICOM_MAGIC_OFFSET + 4)
                        if looks_like_dicom_bytes(head):
                            return True
                except Exception:  # noqa: BLE001
                    continue

            # 4. Last-ditch — probe up to 50 remaining non-dir entries
            remaining = [
                n for n in non_dirs
                if n not in dcm_candidates and n not in no_ext
            ][:50]
            for n in remaining:
                try:
                    with zf.open(n) as fh:
                        head = fh.read(DICOM_MAGIC_OFFSET + 4)
                        if looks_like_dicom_bytes(head):
                            return True
                except Exception:  # noqa: BLE001
                    continue

            # Truly nothing DICOM-shaped — log a hint for next time.
            logger.info(
                "looks_like_dicom_archive: no DICM magic in %s "
                "(%d names, %d non-dir, %d .dcm-ext)",
                zip_path.name, len(names), len(non_dirs),
                len(dcm_candidates),
            )
    except (zipfile.BadZipFile, OSError) as e:
        logger.warning("dicom archive probe failed for %s: %s", zip_path, e)
    return False


# ── Data model ────────────────────────────────────────────────────────


@dataclass
class DicomInstance:
    """One .dcm file. Resolved path + minimal metadata for ordering."""
    file_path: Path
    sop_instance_uid: str
    instance_number: Optional[int] = None
    # Slice position along the patient z-axis (in mm). Used to sort
    # slices when InstanceNumber is missing or unreliable.
    z_position: Optional[float] = None


@dataclass
class DicomSeries:
    """One series within a study — typically one acquisition / view."""
    series_instance_uid: str
    series_number: Optional[int] = None
    modality: str = ""
    body_part: str = ""
    series_description: str = ""
    # Windowing pulled from the first instance; downstream renderer
    # uses these as the default before any caller override.
    default_wl: Optional[float] = None
    default_ww: Optional[float] = None
    instances: list[DicomInstance] = field(default_factory=list)

    @property
    def slice_count(self) -> int:
        return len(self.instances)


@dataclass
class DicomStudy:
    """One imaging study — all series for one acquisition session."""
    study_instance_uid: str
    study_date: str = ""       # YYYYMMDD per DICOM convention
    study_description: str = ""
    modality: str = ""
    # Hash of (PatientID + local salt) — never the raw PatientID.
    # See _hash_patient_id below for the rationale.
    patient_hash: str = ""
    # Anonymized hint for display only. Best-effort — empty when the
    # source had PHI stripped before upload.
    patient_age_group: str = ""
    patient_sex: str = ""
    series: list[DicomSeries] = field(default_factory=list)

    @property
    def total_instances(self) -> int:
        return sum(s.slice_count for s in self.series)


# ── Patient hashing ───────────────────────────────────────────────────


def _patient_salt() -> bytes:
    """Per-installation salt — used to compute patient_hash so the
    same PatientID hashes to the same string within one Nexus
    install (enables cross-session memory) but is unrecoverable
    across installs (privacy boundary).

    Persistence: $RUNE_HOME/.nexus/patient_salt — created on first
    call with 32 random bytes. Never logged, never sent to cloud
    services. If the file is wiped, all prior patient_hashes become
    orphaned (no way to retroactively rebuild them) — that's an
    acceptable cost for a privacy reset.
    """
    rune_home = Path(os.getenv("RUNE_HOME") or str(Path.home() / ".rune"))
    salt_path = rune_home / ".nexus" / "patient_salt"
    if salt_path.exists():
        try:
            return salt_path.read_bytes()
        except OSError as exc:
            logger.debug("reading patient salt failed: %s", exc)
    salt = os.urandom(32)
    try:
        salt_path.parent.mkdir(parents=True, exist_ok=True)
        salt_path.write_bytes(salt)
        # 600: medic-only readable
        try:
            os.chmod(salt_path, 0o600)
        except OSError as exc:
            logger.debug("chmod patient salt failed: %s", exc)
    except OSError as e:
        logger.warning(
            "patient_salt persist failed (%s) — using ephemeral salt; "
            "patient_hash will not be stable across server restarts.",
            e,
        )
    return salt


def _hash_patient_id(raw_patient_id: str) -> str:
    """SHA256(salt + patient_id). Empty input → empty hash."""
    if not raw_patient_id:
        return ""
    h = hashlib.sha256()
    h.update(_patient_salt())
    h.update(raw_patient_id.encode("utf-8"))
    return h.hexdigest()[:32]


# ── Parsing ───────────────────────────────────────────────────────────


def _read_tag(ds, tag: str, default=""):
    """Safe attribute read on a pydicom Dataset.

    pydicom raises AttributeError for missing tags rather than
    returning None. Using ``get`` returns the Element wrapper not
    the value, which is annoying. This helper does the right thing.
    """
    try:
        val = getattr(ds, tag)
    except (AttributeError, KeyError):
        return default
    if val is None:
        return default
    return val


def parse_dicom_archive(zip_path: Path, extract_root: Path) -> DicomStudy:
    """Extract a DICOM zip and return a fully-indexed DicomStudy.

    Multiple studies in one zip would be unusual but legal — we
    pick the largest one and emit a warning. Multi-study support
    is a v2.

    ``extract_root`` is where the .dcm files end up on disk. The
    server keeps them so subsequent render calls don't need to
    re-extract. Caller should create a per-upload subdir.
    """
    import pydicom

    extract_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        # ZipFile.extractall preserves directory structure. We
        # ignore DICOMDIR (an index file, not pixel data) and any
        # __MACOSX/ entries macOS sometimes embeds.
        zf.extractall(extract_root)

    # Walk every extracted file, classify as DICOM, group by study/series.
    studies: dict[str, DicomStudy] = {}
    series_by_uid: dict[str, DicomSeries] = {}

    for p in extract_root.rglob("*"):
        if not p.is_file():
            continue
        if "__MACOSX" in p.parts:
            continue
        # macOS AppleDouble resource fork sidecars (._foo.dcm) — they
        # have .dcm extension but aren't DICOM; skip silently to avoid
        # noisy "non-DICOM file" debug logs on every PET-CT export.
        if p.name.startswith("._") or p.name == ".DS_Store":
            continue
        if p.name.upper() == "DICOMDIR":
            continue
        try:
            with p.open("rb") as fh:
                head = fh.read(DICOM_MAGIC_OFFSET + 4)
            if not looks_like_dicom_bytes(head):
                continue
            ds = pydicom.dcmread(str(p), stop_before_pixels=True)
        except Exception as e:  # noqa: BLE001
            logger.debug("skip non-DICOM-ish file %s: %s", p.name, e)
            continue

        study_uid = str(_read_tag(ds, "StudyInstanceUID", ""))
        series_uid = str(_read_tag(ds, "SeriesInstanceUID", ""))
        if not study_uid or not series_uid:
            continue

        if study_uid not in studies:
            patient_id = str(_read_tag(ds, "PatientID", ""))
            studies[study_uid] = DicomStudy(
                study_instance_uid=study_uid,
                study_date=str(_read_tag(ds, "StudyDate", "")),
                study_description=str(_read_tag(ds, "StudyDescription", "")),
                modality=str(_read_tag(ds, "Modality", "")),
                patient_hash=_hash_patient_id(patient_id),
                patient_sex=str(_read_tag(ds, "PatientSex", "")),
                # Patient age: PatientAge is "045Y" / "012M" format
                patient_age_group=_age_to_group(str(_read_tag(ds, "PatientAge", ""))),
            )

        if series_uid not in series_by_uid:
            sn = _read_tag(ds, "SeriesNumber")
            try:
                sn_int = int(sn) if sn != "" else None
            except (TypeError, ValueError):
                sn_int = None
            wc = _read_tag(ds, "WindowCenter")
            ww = _read_tag(ds, "WindowWidth")
            series = DicomSeries(
                series_instance_uid=series_uid,
                series_number=sn_int,
                modality=str(_read_tag(ds, "Modality", "")),
                body_part=str(_read_tag(ds, "BodyPartExamined", "")),
                series_description=str(_read_tag(ds, "SeriesDescription", "")),
                default_wl=_as_float(wc),
                default_ww=_as_float(ww),
            )
            series_by_uid[series_uid] = series
            studies[study_uid].series.append(series)

        # Instance row
        inst_num = _read_tag(ds, "InstanceNumber")
        try:
            inst_num_int = int(inst_num) if inst_num != "" else None
        except (TypeError, ValueError):
            inst_num_int = None
        # ImagePositionPatient[2] is the z-coordinate in patient space
        z_pos: Optional[float] = None
        try:
            ipp = _read_tag(ds, "ImagePositionPatient")
            if ipp:
                z_pos = float(ipp[2])
        except (TypeError, ValueError, IndexError):
            z_pos = None
        series_by_uid[series_uid].instances.append(DicomInstance(
            file_path=p,
            sop_instance_uid=str(_read_tag(ds, "SOPInstanceUID", "")),
            instance_number=inst_num_int,
            z_position=z_pos,
        ))

    if not studies:
        raise ValueError(
            "DICOM archive contained no parseable instances — looked "
            "like DICOM at the surface but every file failed to read."
        )

    # Pick the largest study by instance count if multiple — warn.
    best = max(studies.values(), key=lambda s: s.total_instances)
    if len(studies) > 1:
        logger.warning(
            "DICOM archive %s contained %d studies; picked largest "
            "(%d instances). v2 will surface a study picker.",
            zip_path.name, len(studies), best.total_instances,
        )

    # Sort each series' instances by z-position (preferred) or
    # InstanceNumber (fallback). Sorted slices are what every
    # downstream renderer expects.
    for s in best.series:
        s.instances.sort(key=_instance_sort_key)
        # Cap if absurdly large — guard rail against zip bombs.
        if len(s.instances) > MAX_INSTANCES_PER_SERIES:
            logger.warning(
                "series %s capped from %d → %d instances",
                s.series_instance_uid[:12], len(s.instances),
                MAX_INSTANCES_PER_SERIES,
            )
            s.instances = s.instances[:MAX_INSTANCES_PER_SERIES]

    return best


def _instance_sort_key(inst: DicomInstance):
    """Z-position-first sort key. Fallback to InstanceNumber, then 0."""
    if inst.z_position is not None:
        return (0, inst.z_position)
    if inst.instance_number is not None:
        return (1, float(inst.instance_number))
    return (2, 0.0)


def _as_float(v) -> Optional[float]:
    """Defensive float coercion. DICOM tags can come as VR=DS (string),
    VR=FL (float), or MultiValue (lists). All produce a single float
    we use as the windowing default."""
    if v is None or v == "":
        return None
    try:
        if hasattr(v, "__iter__") and not isinstance(v, str):
            return float(v[0])
        return float(v)
    except (TypeError, ValueError, IndexError):
        return None


def _age_to_group(age_str: str) -> str:
    """DICOM PatientAge is "045Y" / "012M" / "030D". Bucket to a
    decade range to preserve clinical context without storing the
    exact age (which is PHI in some jurisdictions)."""
    if not age_str or len(age_str) < 2:
        return ""
    unit = age_str[-1].upper()
    try:
        n = int(age_str[:-1])
    except ValueError:
        return ""
    if unit == "Y":
        if n < 1:    return "<1y"
        if n < 18:   return "child"
        if n < 30:   return "20s"
        if n < 40:   return "30s"
        if n < 50:   return "40s"
        if n < 60:   return "50s"
        if n < 70:   return "60s"
        if n < 80:   return "70s"
        return "80+"
    if unit in ("M", "D"):
        return "<1y"
    return ""


# ── Rendering ─────────────────────────────────────────────────────────


def _resolve_window(
    ds, modality: str, preset: str,
) -> tuple[Optional[float], Optional[float]]:
    """Pick the windowing to apply for a render.

    Priority:
      1. Explicit ``preset`` ("lung"/"mediastinum"/"bone"/"brain")
         maps to a sensible default for the modality.
      2. DICOM-provided WindowCenter/WindowWidth tags.
      3. None,None — caller will fall back to min/max linear scaling.
    """
    if preset and preset != "default":
        wl, ww = DEFAULT_WINDOWS.get((modality, preset), (None, None))
        if wl is not None and ww is not None:
            return wl, ww
    # Pull from the dataset
    wc = _as_float(getattr(ds, "WindowCenter", None))
    ww = _as_float(getattr(ds, "WindowWidth", None))
    if wc is not None and ww is not None:
        return wc, ww
    # Fall through to modality default
    return DEFAULT_WINDOWS.get((modality, "default"), (None, None))


def _hu_array(ds) -> "np.ndarray":
    """Apply RescaleSlope / RescaleIntercept so we get HU values."""
    import numpy as np
    arr = ds.pixel_array.astype(np.float32)
    slope = _as_float(getattr(ds, "RescaleSlope", 1)) or 1.0
    intercept = _as_float(getattr(ds, "RescaleIntercept", 0)) or 0.0
    return arr * slope + intercept


def _percentile_window(arr: "np.ndarray",
                       lo_p: float = 2.0, hi_p: float = 98.0) -> tuple[float, float]:
    """Robust auto-window: clip the bottom/top percentiles of the
    pixel histogram. Works for any modality (CT, MR, PT, NM, X-ray)
    because it doesn't assume the pixel range maps to HU/SUV/etc. —
    it just shows the bulk of the signal.

    The 2/98 percentile pair is what most PACS auto-window UIs use
    when the medic clicks "auto". Matches medic expectations and
    avoids the black-image trap when DICOM tags lie about WL/WW.
    """
    import numpy as np
    flat = arr[np.isfinite(arr)] if arr.size else arr
    if flat.size == 0:
        return 0.0, 1.0
    lo = float(np.percentile(flat, lo_p))
    hi = float(np.percentile(flat, hi_p))
    if hi - lo < 1e-3:
        # Degenerate (all pixels identical) — bracket around the mean
        # so we don't divide by zero downstream.
        m = float(flat.mean())
        return m - 0.5, m + 0.5
    return lo, hi


def _window_to_uint8(arr: "np.ndarray", wl: Optional[float], ww: Optional[float],
                     *, modality: str = "", _label: str = "") -> "np.ndarray":
    """Convert pixel array → 0-255 grayscale via WL/WW.

    Three-stage fallback so we never produce a flat-zero image for a
    valid DICOM series, regardless of how exotic the modality is:

      1. If caller supplied WL/WW (DICOM tags or modality preset),
         apply that window first.
      2. Inspect the output — if it's degenerate (>=99% of pixels
         clipped to a single intensity), the supplied window
         didn't actually contain the signal. Re-render with the
         2/98 percentile window instead.
      3. If WL/WW were both None up front (PT / NM / unknown
         modality), skip step 1 entirely and go straight to
         percentile.

    Every render path logs the chosen window + output stats so we
    can diagnose render quality from server.log without having to
    eyeball the PNG.
    """
    import numpy as np

    arr_f = arr.astype(np.float32, copy=False)

    def _apply(_lo: float, _hi: float) -> "np.ndarray":
        out = np.clip((arr_f - _lo) / (_hi - _lo + 1e-9), 0.0, 1.0) * 255.0
        return out.astype(np.uint8)

    # ── Stage 1/3 — supplied window OR percentile ────────────────
    if wl is None or ww is None:
        lo, hi = _percentile_window(arr_f)
        out = _apply(lo, hi)
        used = "percentile"
    else:
        lo, hi = wl - ww / 2, wl + ww / 2
        out = _apply(lo, hi)
        used = "wl/ww"

    # ── Stage 2 — degeneracy salvage ─────────────────────────────
    # Two failure modes to catch: (a) almost all pixels saturate to
    # 0 (window too high), (b) almost all saturate to 255 (window
    # too low), (c) tiny dynamic range (nothing distinguishable).
    saturated = float((out == 0).sum() + (out == 255).sum()) / out.size
    std = float(out.std())
    if used == "wl/ww" and (saturated > 0.99 or std < 2.0):
        salvage_lo, salvage_hi = _percentile_window(arr_f)
        salvage = _apply(salvage_lo, salvage_hi)
        salvage_sat = float(
            (salvage == 0).sum() + (salvage == 255).sum()
        ) / salvage.size
        salvage_std = float(salvage.std())
        logger.info(
            "DICOM render salvage [%s mod=%s]: wl/ww %.1f/%.1f gave "
            "sat=%.2f std=%.1f → percentile %.1f/%.1f → "
            "sat=%.2f std=%.1f%s",
            _label or "(slice)", modality or "(unknown)",
            wl if wl is not None else 0.0,
            ww if ww is not None else 0.0,
            saturated, std, salvage_lo, salvage_hi,
            salvage_sat, salvage_std,
            "  USED-SALVAGE" if salvage_std > std else "  KEPT-ORIGINAL",
        )
        # Prefer whichever output has more visible variation.
        if salvage_std > std:
            return salvage
        return out

    logger.info(
        "DICOM render [%s mod=%s]: %s window lo=%.1f hi=%.1f → "
        "out range [%d..%d] mean=%.1f std=%.1f sat=%.2f",
        _label or "(slice)", modality or "(unknown)",
        used, lo, hi,
        int(out.min()), int(out.max()),
        float(out.mean()), std, saturated,
    )
    return out


def render_slice_png(
    series: DicomSeries,
    slice_idx: int,
    *,
    preset: str = "default",
    wl_override: Optional[float] = None,
    ww_override: Optional[float] = None,
) -> bytes:
    """Render one slice in ``series`` to PNG bytes.

    ``slice_idx`` is 0-based after the z-position sort. If out of
    range, clamps to nearest. ``preset`` picks a modality-default
    window when the DICOM tags don't supply one; ``wl_override``
    and ``ww_override`` win over both.
    """
    import pydicom
    from PIL import Image

    if not series.instances:
        raise ValueError("series has no instances")
    idx = max(0, min(slice_idx, len(series.instances) - 1))
    inst = series.instances[idx]
    ds = pydicom.dcmread(str(inst.file_path))
    arr = _hu_array(ds)

    if wl_override is not None and ww_override is not None:
        wl, ww = wl_override, ww_override
    else:
        wl, ww = _resolve_window(ds, series.modality, preset)

    img8 = _window_to_uint8(
        arr, wl, ww,
        modality=series.modality,
        _label=f"slice-{idx}",
    )
    img = Image.fromarray(img8, mode="L")
    # #154 — ensure shortest side >= 768 px. Many PACS export CT at
    # 512×512 which downsizes too aggressively inside Gemini's vision
    # tokeniser. Upscale with LANCZOS so the medic AND the model
    # both get a properly readable image. No-op when already big.
    img = _upscale_to_min_side(img, 768)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _upscale_to_min_side(img: "Image.Image", min_side: int) -> "Image.Image":
    """If either dimension is below ``min_side``, scale up uniformly
    so the shortest side hits exactly that value. LANCZOS keeps
    edges crisp on grayscale medical imagery — what radiologists
    actually want when zooming. Returns the image unchanged when
    already at or above the target.
    """
    from PIL import Image
    w, h = img.size
    short = min(w, h)
    if short >= min_side:
        return img
    scale = min_side / short
    return img.resize(
        (int(round(w * scale)), int(round(h * scale))),
        Image.LANCZOS,
    )


def render_mip_png(
    series: DicomSeries,
    *,
    preset: str = "default",
    sample_stride: int = 1,
) -> bytes:
    """Maximum Intensity Projection along z — one PNG summarising
    the whole volume.

    Best view for nodule / vessel / calcification distribution.
    ``sample_stride`` lets the caller subsample a huge series
    (every Nth slice) to bound compute; default 1 reads everything.
    """
    import numpy as np
    import pydicom
    from PIL import Image

    if not series.instances:
        raise ValueError("series has no instances")

    insts = series.instances[::max(1, sample_stride)]
    arrs = []
    wl_acc: Optional[float] = None
    ww_acc: Optional[float] = None
    for inst in insts:
        ds = pydicom.dcmread(str(inst.file_path))
        arrs.append(_hu_array(ds))
        if wl_acc is None:
            wl_acc, ww_acc = _resolve_window(ds, series.modality, preset)

    vol = np.stack(arrs, axis=0)
    mip = np.max(vol, axis=0)
    img8 = _window_to_uint8(
        mip, wl_acc, ww_acc,
        modality=series.modality,
        _label="mip",
    )
    img = Image.fromarray(img8, mode="L")
    # #154 — match render_slice_png's upscale floor so MIPs are
    # equally readable for the medic + vision model.
    img = _upscale_to_min_side(img, 768)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_grid_png(
    series: DicomSeries,
    *,
    preset: str = "default",
    rows: int = 4,
    cols: int = 4,
    cell_size: int = 256,
    slice_start: Optional[int] = None,
    slice_end:   Optional[int] = None,
) -> bytes:
    """N×M thumbnail grid uniformly sampled across a slice range.

    cell_size:
      #154 — bumped 128 → 256 (16 thumbs × 256² = 1024×1024 PNG). At
      128 px per cell the anatomy was too blurry for Gemini's vision
      model to extract usable features. 256 px keeps each thumb above
      the 224 px threshold most ViT-style encoders need to see
      structure, while staying well under Gemini's per-image cap of
      3072 px. Quick scan now passes 384 explicitly (1536² grid) for
      sub-cm finding detection — see ``quick_scan.QUICK_SCAN_CELL_SIZE``.

    slice_start / slice_end:
      #196 — added so Quick scan can render *dense* per-range grids
      (e.g. 16 consecutive slices = ~1.6 cm of anatomy per grid)
      instead of "16 thumbs spread over the whole 500-slice volume".

      When both are provided, sampling is uniform over the half-open
      range ``[slice_start, slice_end + 1)``. When either is None the
      sampler falls back to the full series — the original behaviour
      callers like ``llm_gateway`` rely on.

      Bug history (2026-06-14): quick_scan.py was already trying to
      pass ``slice_start=s, slice_end=e`` but the signature didn't
      accept them, so every call raised TypeError and fell back to
      a SINGLE whole-series grid — making the whole "scan 25 grids of
      16 slices each" pipeline collapse to one 16-thumbnail overview.
    """
    import pydicom
    from PIL import Image

    if not series.instances:
        raise ValueError("series has no instances")

    n_cells = rows * cols
    count = len(series.instances)

    # Determine the sampling window. Clamp to valid bounds + fall back
    # to whole-series sampling when the caller didn't specify a range.
    if slice_start is not None and slice_end is not None:
        lo = max(0, int(slice_start))
        hi = min(count - 1, int(slice_end))
        if hi < lo:
            # Empty / inverted range — match the whole-series fallback
            # rather than raise; callers in batched flows can ignore
            # the empty PNG and move on.
            lo, hi = 0, count - 1
    else:
        lo, hi = 0, count - 1

    span = hi - lo + 1
    if span <= n_cells:
        sampled = list(range(lo, hi + 1))
    else:
        step = (span - 1) / (n_cells - 1)
        sampled = [lo + int(round(i * step)) for i in range(n_cells)]

    grid_img = Image.new("L", (cols * cell_size, rows * cell_size), 0)
    wl_acc: Optional[float] = None
    ww_acc: Optional[float] = None
    for i, idx in enumerate(sampled):
        inst = series.instances[idx]
        ds = pydicom.dcmread(str(inst.file_path))
        arr = _hu_array(ds)
        if wl_acc is None:
            wl_acc, ww_acc = _resolve_window(ds, series.modality, preset)
        img8 = _window_to_uint8(
            arr, wl_acc, ww_acc,
            modality=series.modality,
            _label=f"grid-cell-{i}",
        )
        cell = Image.fromarray(img8, mode="L").resize(
            (cell_size, cell_size), Image.LANCZOS,
        )
        r, c = divmod(i, cols)
        grid_img.paste(cell, (c * cell_size, r * cell_size))

    buf = io.BytesIO()
    grid_img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── Persistence (dicom_studies / dicom_series tables) ─────────────────


def _index_db_path() -> Path:
    """Per-install DICOM index DB. Sits alongside vector_index.db
    under $RUNE_HOME/data/."""
    rune_home = Path(os.getenv("RUNE_HOME") or str(Path.home() / ".rune"))
    base = rune_home / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base / "dicom_index.db"


def init_dicom_index() -> None:
    """Idempotent schema setup. Safe to call on every server boot."""
    conn = sqlite3.connect(_index_db_path())
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dicom_studies (
                study_id            TEXT PRIMARY KEY,    -- internal UUID
                user_id             TEXT NOT NULL,
                upload_file_id      TEXT NOT NULL,       -- links to uploads.file_id
                study_instance_uid  TEXT NOT NULL,
                study_date          TEXT,
                study_description   TEXT,
                modality            TEXT,
                patient_hash        TEXT,
                patient_age_group   TEXT,
                patient_sex         TEXT,
                extract_dir         TEXT NOT NULL,
                created_at          INTEGER NOT NULL,
                UNIQUE(user_id, study_instance_uid)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_dicom_studies_user
            ON dicom_studies(user_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_dicom_studies_patient
            ON dicom_studies(user_id, patient_hash)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dicom_series (
                series_id            TEXT PRIMARY KEY,
                study_id             TEXT NOT NULL,
                series_instance_uid  TEXT NOT NULL,
                series_number        INTEGER,
                modality             TEXT,
                body_part            TEXT,
                series_description   TEXT,
                default_wl           REAL,
                default_ww           REAL,
                instance_count       INTEGER NOT NULL,
                FOREIGN KEY (study_id) REFERENCES dicom_studies(study_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_dicom_series_study
            ON dicom_series(study_id)
        """)
        # Per-instance file paths — keyed by series + ordinal so the
        # renderer can look up slice N quickly.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dicom_instances (
                series_id            TEXT NOT NULL,
                ordinal              INTEGER NOT NULL,   -- 0-based after sort
                file_path            TEXT NOT NULL,
                sop_instance_uid     TEXT,
                instance_number      INTEGER,
                z_position           REAL,
                PRIMARY KEY (series_id, ordinal)
            )
        """)
        conn.commit()
        logger.info("dicom_index: schema ready at %s", _index_db_path())
    finally:
        conn.close()


def persist_study(
    user_id: str, upload_file_id: str, study: DicomStudy, extract_dir: Path,
    *, patient_hash_override: str = "",
) -> str:
    """Persist a parsed DicomStudy + all series + all instances.

    Returns the internal ``study_id`` (UUID). Existing studies
    (same user, same StudyInstanceUID) UPDATE rather than INSERT to
    handle re-uploads cleanly.

    ``patient_hash_override``: when non-empty, used INSTEAD of the
    PatientID-derived ``study.patient_hash`` for the dicom_studies row.
    The desktop sets this when the medic has a patient open in the
    Imaging tab — uploading then attaches the study to THAT patient
    rather than minting a new one from the DICOM tag.
    """
    study_id = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    conn = sqlite3.connect(_index_db_path())
    try:
        # UPSERT pattern for studies (UNIQUE on user + StudyInstanceUID)
        existing = conn.execute(
            "SELECT study_id FROM dicom_studies "
            "WHERE user_id = ? AND study_instance_uid = ?",
            (user_id, study.study_instance_uid),
        ).fetchone()
        if existing:
            study_id = existing[0]
            # Wipe any prior series/instances so a re-upload doesn't
            # leave orphan rows pointing at a stale extract dir.
            conn.execute(
                "DELETE FROM dicom_instances WHERE series_id IN "
                "(SELECT series_id FROM dicom_series WHERE study_id = ?)",
                (study_id,),
            )
            conn.execute(
                "DELETE FROM dicom_series WHERE study_id = ?", (study_id,),
            )
            # Bug fix (2026-06-15, P0 patient safety):
            # When the same StudyInstanceUID is re-uploaded under a NEW
            # patient binding (medic switched patients between uploads,
            # PACS with stable UIDs, teaching example, etc), the prior
            # `dicom_studies.patient_hash` MUST be overwritten with the
            # new override. Previously this UPDATE silently dropped
            # `patient_hash_override`, so the row stayed bound to the
            # OLD patient and findings landed on the wrong record.
            # See: docs/design/IMAGING_PATIENT_ISOLATION_BUGFIX.md (Bug #2)
            effective_patient_hash = (
                patient_hash_override.strip()
                if patient_hash_override and patient_hash_override.strip()
                else (study.patient_hash or "")
            )
            conn.execute(
                "UPDATE dicom_studies SET upload_file_id = ?, "
                "extract_dir = ?, "
                "patient_hash = COALESCE(NULLIF(?, ''), patient_hash) "
                "WHERE study_id = ?",
                (upload_file_id, str(extract_dir),
                 effective_patient_hash, study_id),
            )
        else:
            # Honor the desktop's per-upload patient binding. When the
            # medic has a patient open, the upload form carries that
            # patient's hash and we use it directly for the dicom_studies
            # row — avoiding a stale row + retroactive UPDATE race that
            # the sidebar's polling can briefly observe.
            effective_patient_hash = (
                patient_hash_override.strip()
                if patient_hash_override and patient_hash_override.strip()
                else study.patient_hash
            )
            conn.execute("""
                INSERT INTO dicom_studies
                (study_id, user_id, upload_file_id, study_instance_uid,
                 study_date, study_description, modality, patient_hash,
                 patient_age_group, patient_sex, extract_dir, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                study_id, user_id, upload_file_id, study.study_instance_uid,
                study.study_date, study.study_description, study.modality,
                effective_patient_hash, study.patient_age_group, study.patient_sex,
                str(extract_dir), now_ms,
            ))

        for series in study.series:
            series_id = str(uuid.uuid4())
            conn.execute("""
                INSERT INTO dicom_series
                (series_id, study_id, series_instance_uid, series_number,
                 modality, body_part, series_description, default_wl,
                 default_ww, instance_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                series_id, study_id, series.series_instance_uid,
                series.series_number, series.modality, series.body_part,
                series.series_description, series.default_wl,
                series.default_ww, series.slice_count,
            ))
            for ordinal, inst in enumerate(series.instances):
                conn.execute("""
                    INSERT INTO dicom_instances
                    (series_id, ordinal, file_path, sop_instance_uid,
                     instance_number, z_position)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    series_id, ordinal, str(inst.file_path),
                    inst.sop_instance_uid, inst.instance_number,
                    inst.z_position,
                ))
        conn.commit()
    finally:
        conn.close()
    return study_id


def load_study(user_id: str, study_id: str) -> Optional[DicomStudy]:
    """Hydrate a DicomStudy + all series + all instances from the
    persisted tables. ``None`` if not found / wrong user.
    """
    conn = sqlite3.connect(_index_db_path())
    try:
        row = conn.execute(
            "SELECT study_instance_uid, study_date, study_description, "
            "modality, patient_hash, patient_age_group, patient_sex "
            "FROM dicom_studies WHERE user_id = ? AND study_id = ?",
            (user_id, study_id),
        ).fetchone()
        if not row:
            return None
        study = DicomStudy(
            study_instance_uid=row[0],
            study_date=row[1] or "",
            study_description=row[2] or "",
            modality=row[3] or "",
            patient_hash=row[4] or "",
            patient_age_group=row[5] or "",
            patient_sex=row[6] or "",
        )
        series_rows = conn.execute(
            "SELECT series_id, series_instance_uid, series_number, "
            "modality, body_part, series_description, default_wl, "
            "default_ww FROM dicom_series WHERE study_id = ? "
            "ORDER BY series_number",
            (study_id,),
        ).fetchall()
        for sr in series_rows:
            series = DicomSeries(
                series_instance_uid=sr[1],
                series_number=sr[2],
                modality=sr[3] or "",
                body_part=sr[4] or "",
                series_description=sr[5] or "",
                default_wl=sr[6],
                default_ww=sr[7],
            )
            inst_rows = conn.execute(
                "SELECT file_path, sop_instance_uid, instance_number, "
                "z_position FROM dicom_instances WHERE series_id = ? "
                "ORDER BY ordinal",
                (sr[0],),
            ).fetchall()
            for ir in inst_rows:
                series.instances.append(DicomInstance(
                    file_path=Path(ir[0]),
                    sop_instance_uid=ir[1] or "",
                    instance_number=ir[2],
                    z_position=ir[3],
                ))
            study.series.append(series)
        return study
    finally:
        conn.close()


def get_patient_context_block(
    user_id: str, study_id: str,
) -> str:
    """#162 — produce a text block describing the patient associated
    with ``study_id`` for injection into the agent's prompt.

    The block mentions:
      * a PHI-safe hash identifier (never the raw PatientName / ID),
      * demographics (sex + 10-year age band) when present,
      * every OTHER study this same patient has on file with the
        same user, so the agent treats finds across uploads as
        belonging to one timeline.

    Returns ``""`` if the study can't be found, or if the patient
    has no demographic info worth showing (e.g. anonymised export
    that stripped all tags). Callers should treat ``""`` as "skip
    the context block, just use the slices on their own."

    Shape is intentionally Markdown-flavoured plain text — the LLM
    sees this verbatim, so it must be unambiguous about which fields
    are scoped to this turn vs. carried across turns.
    """
    if not study_id:
        return ""
    conn = sqlite3.connect(_index_db_path())
    try:
        row = conn.execute(
            "SELECT patient_hash, patient_age_group, patient_sex, "
            "study_description, study_date, modality, "
            "study_instance_uid "
            "FROM dicom_studies WHERE user_id = ? AND study_id = ?",
            (user_id, study_id),
        ).fetchone()
        if not row:
            return ""
        (
            patient_hash, age_group, sex, this_desc,
            this_date, this_modality, this_uid,
        ) = row

        # All other studies for the same patient_hash from the same
        # user. We deliberately use patient_hash (not raw ID) so the
        # context survives DICOM exports that anonymise the PatientID
        # tag inconsistently — as long as the underlying hash inputs
        # remain stable, the cross-upload linkage works. We sort
        # descending by study_date so the agent sees the timeline
        # newest-first (matches how a radiologist reads).
        if patient_hash:
            others = conn.execute(
                "SELECT study_description, study_date, modality, "
                "study_instance_uid "
                "FROM dicom_studies WHERE user_id = ? "
                "AND patient_hash = ? "
                "ORDER BY study_date DESC",
                (user_id, patient_hash),
            ).fetchall()
        else:
            # No hash → can't link across uploads; just describe
            # this one study.
            others = [(this_desc, this_date, this_modality, this_uid)]
    finally:
        conn.close()

    # Compose the block.
    short_hash = (patient_hash or "(no-hash)")[:12]
    parts = [
        "[Patient Context]",
        f"You are reviewing patient PHI-hash:{short_hash}.",
    ]
    demo_bits = []
    if sex:
        demo_bits.append(f"sex={sex}")
    if age_group:
        demo_bits.append(f"age band={age_group}")
    if demo_bits:
        parts.append("Demographics: " + ", ".join(demo_bits) + ".")
    if others and len(others) > 1:
        parts.append(
            f"This patient has {len(others)} studies on file for you "
            "(newest first):"
        )
        for desc, date, mod, uid in others:
            marker = "  → CURRENT" if uid == this_uid else ""
            parts.append(
                f"  - {desc or '(no description)'} "
                f"[{mod or '?'} · {date or 'date unknown'}]"
                f"{marker}"
            )
    else:
        parts.append(
            f"Current study: {this_desc or '(no description)'} "
            f"[{this_modality or '?'} · {this_date or 'date unknown'}]"
        )
    parts.append(
        "Treat findings as referring to this same patient across "
        "turns unless the medic explicitly switches to a different "
        "patient or study."
    )
    parts.append("[/Patient Context]")
    return "\n".join(parts)


def find_study_by_upload(
    user_id: str, upload_file_id: str,
) -> Optional[tuple[str, str]]:
    """Look up a previously-persisted study by the upload it came from.

    Returns ``(study_id, extract_dir)`` if found, else ``None``. Used
    by the chat-time path to avoid re-parsing a DICOM archive that
    the upload route already ingested.
    """
    if not upload_file_id:
        return None
    conn = sqlite3.connect(_index_db_path())
    try:
        row = conn.execute(
            "SELECT study_id, extract_dir FROM dicom_studies "
            "WHERE user_id = ? AND upload_file_id = ?",
            (user_id, upload_file_id),
        ).fetchone()
        if not row:
            return None
        return row[0], row[1]
    finally:
        conn.close()


# ── #152: upload-time prerender ──────────────────────────────────────


# Public outcome strings — both the upload route response and the
# llm_gateway routing layer key off these. Keep them in sync.
DICOM_STATUS_RENDERED      = "rendered"        # MIP+slice+grid saved to disk
DICOM_STATUS_NOT_DICOM     = "not_dicom"       # zip but no DICM magic anywhere
DICOM_STATUS_NOT_ZIP       = "not_zip"         # mime/name doesn't claim zip
DICOM_STATUS_RENDER_FAILED = "render_failed"   # detected but parse/render raised
DICOM_STATUS_TOO_LARGE     = "too_large"       # over per-archive cap
DICOM_STATUS_PRERENDERING  = "prerendering"    # background task in flight (#158)


# ── #158: in-memory prerender progress tracker ──────────────────────
# Keyed by file_id; carries the latest stage / count / total so the
# desktop chip can show a progress bar from upload completion through
# the (often >30s) parse + render phase. In-memory rather than DB so
# we don't write every per-slice tick to SQLite. Survives across the
# upload route returning + the background task finishing because both
# touch the same module-level dict. Cleared when the user
# acknowledges (or after a TTL — see _prerender_progress_gc).
import threading as _threading
import time as _time

_prerender_progress: dict[str, dict] = {}
_prerender_lock = _threading.Lock()


def get_prerender_progress(file_id: str) -> Optional[dict]:
    """Snapshot the current prerender progress for a file_id.

    Returns ``None`` if there's no entry — which can mean either
    "not a DICOM upload" or "the upload happened so long ago the
    entry was GC'd" (60-min TTL).
    """
    with _prerender_lock:
        entry = _prerender_progress.get(file_id)
        if entry is None:
            return None
        return dict(entry)   # shallow copy so caller can't mutate


def _set_prerender_progress(
    file_id: str, *,
    state: str,
    stage: str = "",
    current: int = 0,
    total: int = 0,
    study_id: str = "",
    preview_dir: str = "",
    error: str = "",
) -> None:
    """Atomic update — single writer per file_id (the background
    task). Readers (the polling endpoint) grab snapshots."""
    with _prerender_lock:
        _prerender_progress[file_id] = {
            "state":       state,            # queued|parsing|rendering|done|error
            "stage":       stage,            # human label for the bar
            "current":     int(current),
            "total":       int(total),
            "study_id":    study_id,
            "preview_dir": preview_dir,
            "error":       error,
            "updated_at":  _time.time(),
        }
        # Opportunistic GC — keep table bounded.
        if len(_prerender_progress) > 200:
            cutoff = _time.time() - 60 * 60   # 1h TTL
            for k, v in list(_prerender_progress.items()):
                if v["updated_at"] < cutoff and v["state"] in ("done", "error"):
                    _prerender_progress.pop(k, None)


# Hard cap on prerender work. Bigger archives are still uploaded — we
# just skip the synchronous prerender so the upload doesn't tie up the
# request thread for minutes. The chat-time fallback still tries.
PRERENDER_MAX_BYTES = int(os.environ.get(
    "NEXUS_DICOM_PRERENDER_MAX_BYTES",
    str(3 * 1024 * 1024 * 1024),  # 3 GB
))


def prerender_archive_for_upload(
    *,
    user_id: str,
    upload_file_id: str,
    upload_name: str,
    upload_mime: str,
    upload_size: int,
    disk_path: Path,
    patient_hash_override: str = "",
) -> dict:
    """Synchronously detect+parse+render+persist a DICOM archive at
    upload time so chat-time becomes a cheap disk-read.

    Returns a dict suitable for stamping into the uploads row::

        {
          "status":         one of DICOM_STATUS_* strings,
          "study_id":       persisted study uuid (empty on miss),
          "preview_dir":    abs path where MIP/slice/grid PNGs live,
          "preview_count":  3 on success, 0 otherwise,
          "series_count":   number of series detected,
          "instance_count": total slices across all series,
          "modality":       e.g. "CT", "MR", "" if unknown,
          "error":          short message when status == render_failed,
        }

    The caller (files.py upload route) is responsible for persisting
    this metadata; this function has no DB side-effects outside the
    DICOM index tables that ``persist_study`` already writes.

    By design this function NEVER raises — any failure becomes a
    structured status string. The upload route must succeed even when
    DICOM rendering fails (the bytes are still on disk and the medic
    might still want to open the dedicated viewer later).
    """
    out: dict = {
        "status":         DICOM_STATUS_NOT_ZIP,
        "study_id":       "",
        "preview_dir":    "",
        "preview_count":  0,
        "series_count":   0,
        "instance_count": 0,
        "modality":       "",
        "error":          "",
    }

    # Report initial state to the in-memory progress tracker — even
    # for non-DICOM uploads we want the desktop to see a "done"
    # transition so the polling loop terminates instead of hanging.
    _set_prerender_progress(
        upload_file_id, state="parsing", stage="detecting", total=1,
    )

    name_lower = (upload_name or "").lower()
    is_zip = (
        upload_mime == "application/zip"
        or name_lower.endswith(".zip")
    )
    if not is_zip:
        _set_prerender_progress(
            upload_file_id, state="done", stage="not_zip",
        )
        return out

    if upload_size > PRERENDER_MAX_BYTES:
        out["status"] = DICOM_STATUS_TOO_LARGE
        logger.info(
            "DICOM prerender skipped — %s (%d bytes) over cap %d",
            upload_name, upload_size, PRERENDER_MAX_BYTES,
        )
        _set_prerender_progress(
            upload_file_id, state="done", stage="too_large",
        )
        return out

    if not disk_path.exists():
        out["status"] = DICOM_STATUS_RENDER_FAILED
        out["error"] = "disk_path missing"
        logger.warning(
            "DICOM prerender: disk_path %s does not exist for %s",
            disk_path, upload_name,
        )
        _set_prerender_progress(
            upload_file_id, state="error", stage="missing_file",
            error="disk_path missing",
        )
        return out

    # ── Detect ────────────────────────────────────────────────────
    logger.info(
        "DICOM prerender: probing %s (%d bytes) at %s",
        upload_name, upload_size, disk_path,
    )
    try:
        is_dicom = looks_like_dicom_archive(disk_path)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "DICOM prerender: detector raised on %s: %s",
            upload_name, e,
        )
        is_dicom = False
    if not is_dicom:
        out["status"] = DICOM_STATUS_NOT_DICOM
        logger.info(
            "DICOM prerender: %s rejected by detector — not DICOM",
            upload_name,
        )
        _set_prerender_progress(
            upload_file_id, state="done", stage="not_dicom",
        )
        return out

    _set_prerender_progress(
        upload_file_id, state="parsing", stage="parse_archive",
    )

    # ── Parse + render + persist ──────────────────────────────────
    # extract_root lives next to the uploaded archive so studies are
    # discoverable by file_id alone after a restart.
    extract_root = disk_path.parent / f"{disk_path.stem}.dicom-extract"
    try:
        extract_root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        out["status"] = DICOM_STATUS_RENDER_FAILED
        out["error"] = f"mkdir extract_root: {e}"
        return out

    try:
        study = parse_dicom_archive(disk_path, extract_root)
        if not study.series:
            out["status"] = DICOM_STATUS_RENDER_FAILED
            out["error"] = "parse_dicom_archive returned no series"
            return out
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "DICOM prerender: parse failed on %s: %s",
            upload_name, e,
        )
        out["status"] = DICOM_STATUS_RENDER_FAILED
        out["error"] = f"parse: {type(e).__name__}: {e}"
        return out

    out["modality"] = study.modality or ""
    out["series_count"] = len(study.series)
    out["instance_count"] = study.total_instances

    try:
        study_id = persist_study(
            user_id, upload_file_id, study, extract_root,
            patient_hash_override=patient_hash_override,
        )
        out["study_id"] = study_id
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "DICOM prerender: persist_study failed on %s: %s — "
            "rendering will still happen but study isn't viewable",
            upload_name, e,
        )

    # Render previews. For multi-series studies (the canonical case
    # for PET-CT, where you have a CT series + a PET series), render
    # ONE slice per series modality so the agent sees both anatomic
    # (CT) AND functional (PET) views. MIP + grid still come from
    # the largest series (most clinical content for spotting nodules
    # / vessels / calcifications). This is what radiologists actually
    # do when reading PET-CT: look at the PET slice for uptake, the
    # CT slice for anatomy, and the MIP for overview.
    try:
        primary = max(study.series, key=lambda s: s.slice_count)
        modality_u = (study.modality or "").upper()
        body_u = (primary.body_part or "").upper()
        primary_preset = (
            "lung" if (modality_u == "CT" and "CHEST" in body_u)
            else "default"
        )

        mip = render_mip_png(primary, preset=primary_preset)
        primary_mid_idx = max(0, primary.slice_count // 2)
        primary_mid = render_slice_png(
            primary, primary_mid_idx, preset=primary_preset,
        )
        grid = render_grid_png(primary, rows=4, cols=4)

        # For each additional series with a DIFFERENT modality, render
        # one middle slice with default preset (which now percentile-
        # auto-windows for PT/NM via the DEFAULT_WINDOWS table).
        secondary_slices: list[tuple[str, int, bytes]] = []
        primary_mod = (primary.modality or "").upper()
        seen_mods = {primary_mod}
        for s in study.series:
            mod_u = (s.modality or "").upper()
            if mod_u in seen_mods:
                continue
            seen_mods.add(mod_u)
            try:
                mid_idx = max(0, s.slice_count // 2)
                png = render_slice_png(s, mid_idx, preset="default")
                secondary_slices.append((mod_u or "OTHER", mid_idx, png))
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "DICOM prerender: secondary modality %s render "
                    "failed for %s: %s — primary slice still served",
                    mod_u, upload_name, e,
                )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "DICOM prerender: render failed on %s: %s",
            upload_name, e,
        )
        out["status"] = DICOM_STATUS_RENDER_FAILED
        out["error"] = f"render: {type(e).__name__}: {e}"
        return out

    preview_dir = extract_root / "previews"
    try:
        preview_dir.mkdir(parents=True, exist_ok=True)
        (preview_dir / "mip.png").write_bytes(mip)
        (preview_dir / f"slice-{primary_mid_idx}.png").write_bytes(primary_mid)
        (preview_dir / "grid-4x4.png").write_bytes(grid)
        # Persist any additional-modality slices alongside the primary
        # set. Filenames carry the modality so the chat-time loader +
        # the chip thumbnails can label them ("PT mid-slice", etc.).
        extra_slice_files: list[dict] = []
        for mod_u, mid_idx, png in secondary_slices:
            fn = f"slice-{mod_u.lower()}-{mid_idx}.png"
            (preview_dir / fn).write_bytes(png)
            extra_slice_files.append({
                "modality":    mod_u,
                "slice_index": mid_idx,
                "filename":    fn,
            })

        # Save a small manifest so the chat-time loader knows the slice
        # index without having to guess from filenames.
        import json as _json
        (preview_dir / "manifest.json").write_text(_json.dumps({
            "mip":           "mip.png",
            "slice_index":   primary_mid_idx,
            "slice":         f"slice-{primary_mid_idx}.png",
            "grid":          "grid-4x4.png",
            "preset":        primary_preset,
            "modality":      study.modality or "",
            "series_uid":    primary.series_instance_uid,
            # #153 — per-modality companion slices (PET alongside CT, etc.)
            "extra_slices":  extra_slice_files,
        }, indent=2))
    except OSError as e:
        out["status"] = DICOM_STATUS_RENDER_FAILED
        out["error"] = f"write previews: {e}"
        return out

    out["status"] = DICOM_STATUS_RENDERED
    out["preview_dir"] = str(preview_dir)
    out["preview_count"] = 3
    logger.info(
        "DICOM prerender ✓ %s → study_id=%s, %d series, %d instances, "
        "previews → %s",
        upload_name, (out["study_id"] or "(none)")[:8],
        out["series_count"], out["instance_count"], preview_dir,
    )

    # #158 — eager render every slice in the primary series to disk so
    # the viewer's scroll-through is instant (no per-slice
    # parse+render round-trip). This is the biggest UX win for PET-CT:
    # 1134 slices at ~50 ms each was making the viewer feel laggy;
    # serving pre-rendered PNGs from disk is ~5 ms each. Total cost:
    # ~30 s extra prerender time + ~250 MB disk per study. Acceptable
    # tradeoff — the progress endpoint tells the medic what's
    # happening while it churns.
    #
    # Sliced cache layout:
    #   previews/slices/{idx}-{preset}.png  for the primary series
    #
    # Filename carries the preset so changing W/L doesn't trigger
    # cache-miss for the cached preset.
    slice_cache_dir = preview_dir / "slices"
    try:
        slice_cache_dir.mkdir(exist_ok=True)
    except OSError:
        # Best-effort — eager render is an optimization; failures
        # here don't break the upload, just keep slice scroll on the
        # live-render path.
        return out

    _set_prerender_progress(
        upload_file_id,
        state="rendering",
        stage="cache_slices",
        current=0,
        total=primary.slice_count,
        study_id=out["study_id"],
        preview_dir=str(preview_dir),
    )

    try:
        for i in range(primary.slice_count):
            target = slice_cache_dir / f"{i}-{primary_preset}.png"
            if target.exists():
                # Survive partial re-runs (the user re-uploads the
                # same study and we already cached half of it last
                # time around).
                _set_prerender_progress(
                    upload_file_id,
                    state="rendering",
                    stage="cache_slices",
                    current=i + 1,
                    total=primary.slice_count,
                    study_id=out["study_id"],
                    preview_dir=str(preview_dir),
                )
                continue
            try:
                png_i = render_slice_png(primary, i, preset=primary_preset)
                target.write_bytes(png_i)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "DICOM eager render: slice %d failed for %s: %s — "
                    "viewer will fall back to live render",
                    i, upload_name, e,
                )
            # Update tracker every slice — the rendering loop is
            # the user-visible bar.
            _set_prerender_progress(
                upload_file_id,
                state="rendering",
                stage="cache_slices",
                current=i + 1,
                total=primary.slice_count,
                study_id=out["study_id"],
                preview_dir=str(preview_dir),
            )
        out["eager_cached_slices"] = primary.slice_count
        logger.info(
            "DICOM eager slice render ✓ %s → %d slices cached at %s",
            upload_name, primary.slice_count, slice_cache_dir,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "DICOM eager render loop crashed on %s: %s — partial cache "
            "remains usable", upload_name, e,
        )

    _set_prerender_progress(
        upload_file_id,
        state="done",
        stage="ready",
        current=primary.slice_count,
        total=primary.slice_count,
        study_id=out["study_id"],
        preview_dir=str(preview_dir),
    )
    return out


def load_prerendered_previews(preview_dir: str) -> list[tuple[str, bytes]]:
    """Read the 3 prerendered PNGs from disk for chat-time attach.

    Returns ``[(label, bytes), ...]`` where label is one of
    ``"mip"`` / ``"slice-N"`` / ``"grid-4x4"`` — same labels we use
    when generating the PNGs so the rest of the pipeline (memory
    captions, chip names) sees stable values across runs.

    Returns ``[]`` if the directory is missing or the manifest is
    unreadable. Callers fall back to inline rendering in that case.
    """
    if not preview_dir:
        return []
    p = Path(preview_dir)
    if not p.is_dir():
        return []
    manifest_path = p / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        import json as _json
        manifest = _json.loads(manifest_path.read_text())
    except Exception as e:  # noqa: BLE001
        logger.debug("manifest read failed for %s: %s", preview_dir, e)
        return []

    primary_mod = (manifest.get("modality") or "").upper() or "PRIMARY"
    out: list[tuple[str, bytes]] = []
    for label, filename in (
        (f"{primary_mod.lower()}-mip",
                                      manifest.get("mip", "mip.png")),
        (f"{primary_mod.lower()}-slice-{manifest.get('slice_index', 0)}",
                                      manifest.get("slice", "")),
        (f"{primary_mod.lower()}-grid-4x4",
                                      manifest.get("grid", "grid-4x4.png")),
    ):
        if not filename:
            continue
        fp = p / filename
        if not fp.exists():
            continue
        try:
            out.append((label, fp.read_bytes()))
        except OSError as e:
            logger.debug("preview read failed %s: %s", fp, e)

    # #153 — per-modality companion slices (e.g. PET alongside CT).
    # These give the agent the OTHER modality's middle-slice view so
    # functional findings (PET uptake) get analysed alongside the
    # anatomic CT in the same chat turn.
    for extra in manifest.get("extra_slices", []) or []:
        try:
            mod = (extra.get("modality") or "").upper() or "OTHER"
            fname = extra.get("filename", "")
            slice_idx = extra.get("slice_index", 0)
            if not fname:
                continue
            fp = p / fname
            if not fp.exists():
                continue
            out.append((f"{mod.lower()}-slice-{slice_idx}", fp.read_bytes()))
        except Exception as e:  # noqa: BLE001
            logger.debug("preview read failed for extra slice: %s", e)
    return out
