"""DICOM RTSTRUCT import / export — #146.

A RTSTRUCT (RT Structure Set) DICOM IOD packages the medic's ROI
polygons in a format the radiotherapy planning systems (Eclipse,
Monaco, Pinnacle, ...) understand. Without this, Nexus is an
island — the medic draws contours and they stay trapped.

Two flows:

  * EXPORT — read this user's rt_rois + rt_contours rows for a
    study, build a fresh RTSTRUCT.dcm whose ReferencedFrameOfReference
    points back to the original CT study, write to bytes the HTTP
    route streams to the medic.

  * IMPORT — take an external RTSTRUCT.dcm (often a peer's reading
    or a commercial auto-segmentation output), parse out the
    ROIContourSequence, and write rt_rois + rt_contours rows into
    the same shape Nexus uses natively. The medic can then edit /
    refine on top.

pydicom's RT support is comprehensive but the boilerplate is
huge — we keep this module focused on the small subset of fields
required to round-trip a structure set Eclipse / Monaco will load
without complaining. Anything beyond that (DVH metadata, dose
references, beam config) is out of scope; Nexus is a contouring
tool, not a TPS.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from io import BytesIO
from typing import Optional

logger = logging.getLogger(__name__)


def _index_db():
    from nexus_server.dicom import _index_db_path
    return sqlite3.connect(_index_db_path())


# ── EXPORT ────────────────────────────────────────────────────────────


def export_rtstruct_for_study(user_id: str, study_id: str) -> bytes:
    """Build an RTSTRUCT.dcm in memory for a study + its ROIs.

    The exporter:
      1. Loads the source study via :func:`nexus_server.dicom.load_study`
         so we have the StudyInstanceUID, SeriesInstanceUIDs, and
         per-instance SOPInstanceUIDs to reference.
      2. Reads rt_rois + rt_contours rows from the SQLite index.
      3. Walks each contour, converting polygon image-pixel coords
         to RCS (Reference Coordinate System) using the source
         slice's ImagePositionPatient + ImageOrientationPatient
         + PixelSpacing tags — RTSTRUCT expects RCS mm, NOT pixels.
      4. Emits a pydicom Dataset following the RT Structure Set IOD.
      5. Returns ``ds.save_as`` bytes ready for HTTP transfer.

    Raises ValueError when the study has no ROIs (nothing to
    export — caller should 400 rather than ship an empty file).
    """
    import pydicom
    from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
    from pydicom.sequence import Sequence
    from pydicom.uid import (
        ExplicitVRLittleEndian,
        RTStructureSetStorage,
        generate_uid,
    )

    from nexus_server.dicom import load_study

    study = load_study(user_id, study_id)
    if study is None:
        raise ValueError(f"Study {study_id} not found")

    # Pull this user's ROIs + contours
    rois = _load_rois(user_id, study_id)
    if not rois:
        raise ValueError(
            "Study has no ROIs to export. Draw at least one contour "
            "before requesting RTSTRUCT.",
        )

    # Build the file metadata block
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = RTStructureSetStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()
    file_meta.ImplementationVersionName = "Nexus-RT-1.0"

    # Top-level dataset
    ds = FileDataset(
        "rtstruct.dcm",
        {},
        file_meta=file_meta,
        preamble=b"\x00" * 128,
    )
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    ds.SpecificCharacterSet = "ISO_IR 192"  # UTF-8

    # SOP common
    ds.SOPClassUID = RTStructureSetStorage
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID

    # Patient module — anonymized (we hash patient IDs locally;
    # the RTSTRUCT carries the hash, not the real ID)
    ds.PatientName = f"Nexus^Anonymous^{study.patient_hash[:8]}"
    ds.PatientID = study.patient_hash
    ds.PatientBirthDate = ""
    ds.PatientSex = study.patient_sex or "O"

    # General study
    ds.StudyInstanceUID = study.study_instance_uid
    ds.StudyDate = study.study_date or ""
    ds.StudyTime = ""
    ds.AccessionNumber = ""
    ds.ReferringPhysicianName = ""
    ds.StudyID = ""

    # RT Series
    ds.SeriesInstanceUID = generate_uid()
    ds.Modality = "RTSTRUCT"
    ds.SeriesNumber = 1
    ds.SeriesDescription = "Nexus RT Structure Set"

    # Equipment
    ds.Manufacturer = "Nexus"
    ds.ManufacturerModelName = "Nexus Medical Imaging"
    ds.SoftwareVersions = "1.0"

    # RT Structure Set module
    ds.StructureSetLabel = "NexusRTSS"
    ds.StructureSetName = "Nexus RT Structure Set"
    ds.StructureSetDate = time.strftime("%Y%m%d")
    ds.StructureSetTime = time.strftime("%H%M%S")

    # Referenced Frame of Reference — points back at the source CT
    # series. Required so Eclipse / Monaco know which study these
    # contours overlay.
    frame_uid = generate_uid()
    rfor_seq = Sequence()
    for series in study.series:
        rfor_item = Dataset()
        rfor_item.FrameOfReferenceUID = frame_uid
        # Each series gets its own ReferencedStudySequence entry
        rss_item = Dataset()
        rss_item.ReferencedSOPClassUID = "1.2.840.10008.3.1.2.3.1"  # Detached Study
        rss_item.ReferencedSOPInstanceUID = study.study_instance_uid
        # Inside: RTReferencedSeriesSequence
        rrs_item = Dataset()
        rrs_item.SeriesInstanceUID = series.series_instance_uid
        # Contour Image Sequence — every instance we reference
        cis = Sequence()
        for inst in series.instances:
            ci = Dataset()
            ci.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"  # CT Image
            ci.ReferencedSOPInstanceUID = inst.sop_instance_uid
            cis.append(ci)
        rrs_item.ContourImageSequence = cis
        rss_item.RTReferencedSeriesSequence = Sequence([rrs_item])
        rfor_item.RTReferencedStudySequence = Sequence([rss_item])
        rfor_seq.append(rfor_item)
    ds.ReferencedFrameOfReferenceSequence = rfor_seq

    # StructureSetROISequence — one entry per ROI
    ssros = Sequence()
    rcs_seq = Sequence()
    obs_seq = Sequence()
    for roi_num, roi in enumerate(rois, start=1):
        ssro = Dataset()
        ssro.ROINumber = roi_num
        ssro.ReferencedFrameOfReferenceUID = frame_uid
        ssro.ROIName = roi["name"]
        ssro.ROIGenerationAlgorithm = "MANUAL"
        ssros.append(ssro)

        # ROIContourSequence entry — colour + per-slice contours
        rcs = Dataset()
        rcs.ROIDisplayColor = list(_hex_to_rgb(roi["color_hex"]))
        rcs.ReferencedROINumber = roi_num
        contour_seq = Sequence()
        for c in roi["contours"]:
            # Find the source DICOM instance for this contour
            inst = _find_instance(study, c["series_id"], c["slice_idx"])
            if inst is None:
                logger.warning(
                    "RTSTRUCT export skipped contour roi=%s slice=%d "
                    "(no matching source instance)",
                    roi["name"], c["slice_idx"],
                )
                continue
            contour_ds = Dataset()
            contour_ds.ContourGeometricType = "CLOSED_PLANAR"
            # ContourImageSequence — back-reference to the slice
            cis = Dataset()
            cis.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
            cis.ReferencedSOPInstanceUID = inst["sop_instance_uid"]
            contour_ds.ContourImageSequence = Sequence([cis])
            # Polygon points → RCS coordinates
            xy_pixel = c["polygon_points"]
            xyz_rcs = _pixel_polygon_to_rcs(xy_pixel, inst)
            # Flatten to flat list of mm coordinates [x1,y1,z1,x2,y2,z2,...]
            flat: list[float] = []
            for x, y, z in xyz_rcs:
                flat.extend([x, y, z])
            contour_ds.NumberOfContourPoints = len(xy_pixel)
            contour_ds.ContourData = flat
            contour_seq.append(contour_ds)
        rcs.ContourSequence = contour_seq
        rcs_seq.append(rcs)

        # RTROIObservationsSequence — interpretation type
        obs = Dataset()
        obs.ObservationNumber = roi_num
        obs.ReferencedROINumber = roi_num
        obs.RTROIInterpretedType = _kind_to_dicom_type(roi["kind"])
        obs.ROIInterpreter = ""
        obs_seq.append(obs)

    ds.StructureSetROISequence = ssros
    ds.ROIContourSequence = rcs_seq
    ds.RTROIObservationsSequence = obs_seq

    # Write to bytes
    buf = BytesIO()
    pydicom.dcmwrite(buf, ds, enforce_file_format=True)
    return buf.getvalue()


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """``#FF6B6B`` → (255, 107, 107). Defaults to red on malformed."""
    s = hex_str.lstrip("#")
    if len(s) != 6:
        return (255, 0, 0)
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore
    except ValueError:
        return (255, 0, 0)


def _rgb_to_hex(rgb: list) -> str:
    """RTSTRUCT ROIDisplayColor list → '#RRGGBB' hex string."""
    if not rgb or len(rgb) < 3:
        return "#FF6B6B"
    try:
        r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])
        return f"#{r:02X}{g:02X}{b:02X}"
    except (TypeError, ValueError):
        return "#FF6B6B"


# Map Nexus ROI kinds to DICOM RT-ROI-Interpreted-Type strings.
# DICOM allows almost anything but commercial TPS systems only
# render a finite set reliably. Stick to the well-known ones.
_KIND_TO_DICOM = {
    "GTV": "GTV",
    "CTV": "CTV",
    "PTV": "PTV",
    "OAR": "ORGAN",
    "POI": "MARKER",
    "OTHER": "CONTROL",
}
_DICOM_TO_KIND = {v: k for k, v in _KIND_TO_DICOM.items()}


def _kind_to_dicom_type(kind: str) -> str:
    return _KIND_TO_DICOM.get(kind, "CONTROL")


def _dicom_type_to_kind(dt: str) -> str:
    return _DICOM_TO_KIND.get((dt or "").upper(), "OTHER")


def _find_instance(study, series_id: str, slice_idx: int) -> Optional[dict]:
    """Look up the source slice's ImagePositionPatient + ImageOrientationPatient
    + PixelSpacing so we can transform polygon pixel coords → RCS mm.

    Returns ``{sop_instance_uid, ipp, iop, ps}`` or None when the
    slice can't be located.
    """
    import pydicom
    # Walk study.series to find the right one + the ordinal'd instance
    for s in study.series:
        # series_id from the DB doesn't appear on DicomStudy directly;
        # match by series_instance_uid instead. The caller passes the
        # internal series_id; we look it up in the index.
        conn = _index_db()
        try:
            row = conn.execute(
                "SELECT series_instance_uid FROM dicom_series "
                "WHERE series_id = ?",
                (series_id,),
            ).fetchone()
            if not row:
                continue
            wanted_uid = row[0]
        finally:
            conn.close()
        if s.series_instance_uid != wanted_uid:
            continue
        if slice_idx < 0 or slice_idx >= len(s.instances):
            return None
        inst = s.instances[slice_idx]
        try:
            ds = pydicom.dcmread(str(inst.file_path), stop_before_pixels=True)
            ipp = list(getattr(ds, "ImagePositionPatient", [0.0, 0.0, 0.0]))
            iop = list(getattr(ds, "ImageOrientationPatient",
                                [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]))
            ps = list(getattr(ds, "PixelSpacing", [1.0, 1.0]))
            return {
                "sop_instance_uid": inst.sop_instance_uid,
                "ipp": [float(v) for v in ipp],
                "iop": [float(v) for v in iop],
                "ps": [float(v) for v in ps],
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("instance read failed for slice %d: %s",
                           slice_idx, e)
            return None
    return None


def _pixel_polygon_to_rcs(
    xy_pixel: list[list[float]], inst: dict,
) -> list[tuple[float, float, float]]:
    """Convert polygon image-pixel coords → patient RCS millimetres.

    DICOM mapping (PS3.3 §C.7.6.2.1.1):

      P = O + r * (deltaR * eX) + c * (deltaC * eY)

    where:
      O   = ImagePositionPatient (origin of pixel [0,0,0] in mm)
      eX  = row direction cosine (ImageOrientationPatient[0:3])
      eY  = column direction cosine (ImageOrientationPatient[3:6])
      deltaR = PixelSpacing[0] (row spacing, mm/px)
      deltaC = PixelSpacing[1] (column spacing, mm/px)
      r,c   = pixel row, column

    Image-pixel coords come in as [x, y] where x = column, y = row.
    """
    O = inst["ipp"]
    eX = inst["iop"][0:3]
    eY = inst["iop"][3:6]
    deltaR = inst["ps"][0]
    deltaC = inst["ps"][1]
    out: list[tuple[float, float, float]] = []
    for px in xy_pixel:
        x_pix, y_pix = float(px[0]), float(px[1])
        # x_pix = column, y_pix = row
        out.append((
            O[0] + y_pix * deltaR * eX[0] + x_pix * deltaC * eY[0],
            O[1] + y_pix * deltaR * eX[1] + x_pix * deltaC * eY[1],
            O[2] + y_pix * deltaR * eX[2] + x_pix * deltaC * eY[2],
        ))
    return out


def _rcs_polygon_to_pixel(
    xyz_flat: list[float], inst: dict,
) -> list[list[float]]:
    """Inverse of :func:`_pixel_polygon_to_rcs` — used by importer.

    Given the flat ContourData list [x1,y1,z1,x2,y2,z2,...] in
    mm RCS coords, recover [[x_pix, y_pix], ...] for each point.

    Solves the 2-D linear system per point — direction cosines
    are unit vectors so this is just two dot products.
    """
    O = inst["ipp"]
    eX = inst["iop"][0:3]
    eY = inst["iop"][3:6]
    deltaR = inst["ps"][0]
    deltaC = inst["ps"][1]
    pts: list[list[float]] = []
    for i in range(0, len(xyz_flat), 3):
        x_rcs = xyz_flat[i] - O[0]
        y_rcs = xyz_flat[i + 1] - O[1]
        z_rcs = xyz_flat[i + 2] - O[2]
        # row = (xyz_rcs · eX) / deltaR
        # col = (xyz_rcs · eY) / deltaC
        row = (x_rcs * eX[0] + y_rcs * eX[1] + z_rcs * eX[2]) / deltaR
        col = (x_rcs * eY[0] + y_rcs * eY[1] + z_rcs * eY[2]) / deltaC
        pts.append([col, row])  # back to [x_pix, y_pix] = [col, row]
    return pts


def _load_rois(user_id: str, study_id: str) -> list[dict]:
    """Read all rt_rois + rt_contours rows for this study into a
    list of dicts the exporter can walk."""
    conn = _index_db()
    try:
        roi_rows = conn.execute(
            "SELECT roi_id, name, kind, color_hex FROM rt_rois "
            "WHERE user_id = ? AND study_id = ? ORDER BY created_at",
            (user_id, study_id),
        ).fetchall()
        out = []
        for rr in roi_rows:
            roi_id, name, kind, color = rr
            contour_rows = conn.execute(
                "SELECT series_id, slice_idx, polygon_points FROM rt_contours "
                "WHERE roi_id = ? ORDER BY series_id, slice_idx",
                (roi_id,),
            ).fetchall()
            contours = [{
                "series_id": cr[0],
                "slice_idx": cr[1],
                "polygon_points": json.loads(cr[2]),
            } for cr in contour_rows]
            out.append({
                "roi_id": roi_id,
                "name": name,
                "kind": kind,
                "color_hex": color,
                "contours": contours,
            })
        return out
    finally:
        conn.close()


# ── IMPORT ────────────────────────────────────────────────────────────


def import_rtstruct_bytes(
    user_id: str, study_id: str, dcm_bytes: bytes,
) -> tuple[int, int]:
    """Parse external RTSTRUCT.dcm and write ROIs + contours into the
    Nexus index DB.

    Returns ``(n_rois_imported, n_contours_imported)``.
    Raises ValueError on malformed input.
    """
    import pydicom

    from nexus_server.dicom import load_study

    try:
        ds = pydicom.dcmread(BytesIO(dcm_bytes))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Not a parseable DICOM file: {e}") from e

    if getattr(ds, "Modality", "") != "RTSTRUCT":
        raise ValueError(
            f"Uploaded DICOM has Modality={getattr(ds, 'Modality', '?')}, "
            "expected RTSTRUCT."
        )

    study = load_study(user_id, study_id)
    if study is None:
        raise ValueError(f"Study {study_id} not found")

    # Build a {SOPInstanceUID → (series_id, slice_idx, inst dict)} index
    # so we can map ContourImageSequence references back to our slices.
    sop_to_slice: dict[str, tuple[str, int, dict]] = {}
    conn = _index_db()
    try:
        for s in study.series:
            row = conn.execute(
                "SELECT series_id FROM dicom_series "
                "WHERE series_instance_uid = ? AND study_id = ?",
                (s.series_instance_uid, study_id),
            ).fetchone()
            if not row:
                continue
            series_id = row[0]
            for idx, inst in enumerate(s.instances):
                inst_meta = _find_instance(study, series_id, idx)
                if inst_meta:
                    sop_to_slice[inst.sop_instance_uid] = (
                        series_id, idx, inst_meta,
                    )
    finally:
        conn.close()

    # Walk RTSTRUCT
    roi_meta_by_num: dict[int, dict] = {}
    for ssro in getattr(ds, "StructureSetROISequence", []):
        roi_num = int(getattr(ssro, "ROINumber", 0))
        roi_meta_by_num[roi_num] = {
            "name": str(getattr(ssro, "ROIName", f"ROI-{roi_num}")),
            "kind": "OTHER",  # will refine from RTROIObservationsSequence
            "color_hex": "#FF6B6B",
        }
    for obs in getattr(ds, "RTROIObservationsSequence", []):
        roi_num = int(getattr(obs, "ReferencedROINumber", 0))
        if roi_num in roi_meta_by_num:
            roi_meta_by_num[roi_num]["kind"] = _dicom_type_to_kind(
                str(getattr(obs, "RTROIInterpretedType", "")),
            )

    n_rois = 0
    n_contours = 0
    conn = _index_db()
    try:
        now_ms = int(time.time() * 1000)
        for rcs in getattr(ds, "ROIContourSequence", []):
            roi_num = int(getattr(rcs, "ReferencedROINumber", 0))
            meta = roi_meta_by_num.get(roi_num)
            if not meta:
                continue
            # Capture colour
            color = _rgb_to_hex(list(getattr(rcs, "ROIDisplayColor", [])))

            # Write the ROI row
            roi_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO rt_rois (roi_id, study_id, user_id, name, "
                "kind, color_hex, margin_mm, derived_from, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                (roi_id, study_id, user_id, meta["name"], meta["kind"],
                 color, now_ms, now_ms),
            )
            n_rois += 1

            # Each contour
            for contour_ds in getattr(rcs, "ContourSequence", []):
                # Which slice does this contour belong to?
                cis = getattr(contour_ds, "ContourImageSequence", [])
                if not cis:
                    continue
                sop_uid = str(getattr(cis[0], "ReferencedSOPInstanceUID", ""))
                slice_info = sop_to_slice.get(sop_uid)
                if not slice_info:
                    logger.debug(
                        "imported RTSTRUCT references unknown SOP "
                        "%s (skipping)", sop_uid[:16],
                    )
                    continue
                series_id, slice_idx, inst_meta = slice_info
                xyz_flat = list(getattr(contour_ds, "ContourData", []))
                if not xyz_flat or len(xyz_flat) % 3 != 0:
                    continue
                pts = _rcs_polygon_to_pixel(xyz_flat, inst_meta)
                conn.execute(
                    "INSERT INTO rt_contours (contour_id, roi_id, "
                    "series_id, slice_idx, polygon_points, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), roi_id, series_id, slice_idx,
                     json.dumps(pts), now_ms, now_ms),
                )
                n_contours += 1
        conn.commit()
    finally:
        conn.close()
    return n_rois, n_contours
