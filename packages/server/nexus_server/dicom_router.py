"""DICOM HTTP API — #142 + #144 + #146.

Endpoints under /api/v1/dicom/...:

  * GET  /studies                         — list this user's studies
  * GET  /studies/{study_id}              — study + series metadata
  * GET  /studies/{study_id}/series/{series_id}/render
        ?kind=slice|mip|grid&slice=N&window=lung|mediastinum|bone|default
                                          — render PNG (#142 viewer)
  * GET  /studies/{study_id}/rois         — list ROIs + per-slice contours (#144)
  * POST /studies/{study_id}/rois         — create/update an ROI
  * POST /studies/{study_id}/rois/{roi_id}/contours — add/update a contour
  * DELETE /studies/{study_id}/rois/{roi_id} — wipe a ROI + its contours
  * GET  /studies/{study_id}/rtstruct.dcm — export ROIs as RTSTRUCT (#146)
  * POST /studies/{study_id}/rtstruct/import — import medic-uploaded RTSTRUCT
  * POST /studies/{study_id}/series/{series_id}/sam — AI auto-segment (#145 stub)

All endpoints are user-scoped via Depends(get_current_user) — a
medic can only touch their own studies.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from nexus_server.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/dicom", tags=["dicom"])


# ── Study + series catalog ────────────────────────────────────────────


class SeriesInfo(BaseModel):
    series_id: str
    series_instance_uid: str
    series_number: Optional[int]
    modality: str
    body_part: str
    series_description: str
    default_wl: Optional[float]
    default_ww: Optional[float]
    instance_count: int


class StudyInfo(BaseModel):
    study_id: str
    study_instance_uid: str
    study_date: str
    study_description: str
    modality: str
    patient_hash: str
    patient_age_group: str
    patient_sex: str
    series: list[SeriesInfo] = Field(default_factory=list)
    created_at: int


def _index_db():
    from nexus_server.dicom import _index_db_path
    return sqlite3.connect(_index_db_path())


class PatientCard(BaseModel):
    """#174 — aggregate row for the patient navigator. One entry per
    distinct (user_id, patient_hash). The desktop's PatientNavigator
    UI renders these as collapsible cards in the left rail.

    ``initials`` / ``mrn`` come from manual registration. They drive
    the human-readable label shown in the rail / mode header instead
    of the raw patient_hash. ``sequence_number`` is the user-local
    ordinal (oldest = 1) — gives the medic a stable "Patient #3"
    handle that doesn't change when other patients shift around the
    rail."""
    patient_hash:      str
    patient_age_group: str
    patient_sex:       str
    study_count:       int
    latest_study_date: str
    latest_modality:   str
    last_seen_at:      int
    initials:          str = ""
    mrn:               str = ""
    sequence_number:   int = 0
    created_at:        int = 0


@router.get("/patients", response_model=list[PatientCard])
async def list_patients(
    current_user: str = Depends(get_current_user),
    include: str = "active",
) -> list[PatientCard]:
    """#174/#190 — aggregate the user's patients for the left-rail
    navigator. UNIONs two sources:

      * dicom_studies — patients that arrived via DICOM uploads;
        we group by patient_hash and pull modality / study count.
      * patients — manually-registered cases from the New Patient
        dialog. These have study_count=0 until a DICOM lands.

    Same patient appearing in both (medic typed them in + later
    uploaded their study) is merged into one card by patient_hash.
    Newest-touched-first ordering so the medic's current case is at
    the top.
    """
    # #190 debug — log entry so we can confirm the endpoint is even
    # hit from the desktop's poll loop.
    logger.info("[diag] list_patients ENTER user=%s", current_user)

    # Ensure schema is initialised before we query (handles cold start
    # where the user opens the rail before any DICOM has been seen).
    try:
        from nexus_server.patients_router import init_patients_table
        init_patients_table()
    except Exception as _init_e:  # noqa: BLE001
        logger.warning("[diag] init_patients_table failed: %s", _init_e)

    # F-merge-patients-db — `dicom_studies` stays in dicom_index.db,
    # `patients` now lives in the SHARED nexus_server.db. We can't JOIN
    # across SQLite files, so we open both connections and merge in
    # Python (same pattern as patients_router.list_patients_full).
    conn = _index_db()
    try:
        # #190 — simplified query. Pulls all study rows for this user
        # newest-first, then aggregates in Python by patient_hash. The
        # previous version tried a correlated subquery that referenced
        # the outer SELECT alias `phash` — SQLite (incorrectly per ANSI
        # but consistently in practice) doesn't expose outer aliases
        # inside subqueries, so the query failed with
        # "no such column: phash". Python aggregation is simpler +
        # provably correct for our small N (≤ thousands of rows).
        raw_dicom = conn.execute(
            """
            SELECT
                patient_hash,
                patient_age_group,
                patient_sex,
                study_date,
                modality,
                created_at
            FROM dicom_studies
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (current_user,),
        ).fetchall()
    finally:
        conn.close()

    # F-archive-frontend — three modes via ?include= query:
    #   active   (default) → archived_at IS NULL       (the picker case)
    #   archived           → archived_at IS NOT NULL   (Settings "restore" view)
    #   all                → no filter                 (admin/debug)
    if include == "archived":
        archive_clause = "AND archived_at IS NOT NULL"
    elif include == "all":
        archive_clause = ""
    else:
        archive_clause = "AND archived_at IS NULL"
    try:
        from nexus_server.database import get_db_connection
        with get_db_connection() as _shared_conn:
            manual_rows = _shared_conn.execute(
                f"""
                SELECT
                    patient_hash,
                    age_group,
                    sex,
                    created_at,
                    COALESCE(initials, ''),
                    COALESCE(mrn, ''),
                    archived_at
                FROM patients
                WHERE user_id = ? {archive_clause}
                """,
                (current_user,),
            ).fetchall()
    except Exception as _patients_err:  # noqa: BLE001
        # patients table doesn't exist yet (fresh DB, schema
        # init hasn't run) — fall back to dicom-only behaviour.
        # Same path used when the archived_at column is missing
        # on a very old DB (init_patients_table adds it but the
        # ALTER may not have run yet on this connection).
        logger.warning("[diag] patients query failed: %s",
                       _patients_err)
        manual_rows = []

    # Aggregate dicom rows per patient_hash in Python. Since
    # raw_dicom is ordered DESC by created_at, the first row we see
    # for each phash is the latest study — exactly what we want for
    # latest_study_date / latest_modality.
    dicom_agg: dict[str, dict] = {}
    for r in raw_dicom:
        phash = r[0] if r[0] else "_anonymous"
        if phash not in dicom_agg:
            dicom_agg[phash] = {
                "patient_hash":      phash,
                "patient_age_group": r[1] or "",
                "patient_sex":       r[2] or "",
                "study_count":       1,
                "latest_study_date": r[3] or "",
                "latest_modality":   r[4] or "",
                "last_seen_at":      int(r[5] or 0),
            }
        else:
            d = dicom_agg[phash]
            d["study_count"] += 1
            if not d["patient_age_group"] and r[1]:
                d["patient_age_group"] = r[1]
            if not d["patient_sex"] and r[2]:
                d["patient_sex"] = r[2]
            d["last_seen_at"] = max(d["last_seen_at"],
                                    int(r[5] or 0))

    # Convert to the shape the merge step below expects.
    dicom_rows = [
        (
            d["patient_hash"], d["patient_age_group"],
            d["patient_sex"], d["study_count"],
            d["latest_study_date"], d["latest_modality"],
            d["last_seen_at"],
        )
        for d in dicom_agg.values()
    ]

    # F-archive-frontend — build a set of archived hashes so DICOM-
    # only patients (where the only signal that they're "archived"
    # is the patients table row) get filtered consistently.
    # ``patients.archived_at`` is on the SHARED DB (nexus_server.db),
    # not dicom_index.db, so we open get_db_connection separately.
    archived_hashes: set[str] = set()
    if include == "active" or include == "archived":
        try:
            from nexus_server.database import get_db_connection
            with get_db_connection() as _shared_conn:
                if include == "archived":
                    rows = _shared_conn.execute(
                        "SELECT patient_hash FROM patients "
                        "WHERE user_id = ? AND archived_at IS NOT NULL",
                        (current_user,),
                    ).fetchall()
                else:
                    rows = _shared_conn.execute(
                        "SELECT patient_hash FROM patients "
                        "WHERE user_id = ? AND archived_at IS NOT NULL",
                        (current_user,),
                    ).fetchall()
                archived_hashes = {r[0] for r in rows}
        except Exception:  # noqa: BLE001
            archived_hashes = set()

    # Merge by patient_hash. DICOM data wins for study/modality fields;
    # manual data fills in age_group / sex if DICOM didn't have them
    # (e.g. anonymized PACS export).
    by_hash: dict[str, dict] = {}
    for r in dicom_rows:
        ph = r[0]
        # Active mode: skip DICOM patient if hash is archived.
        # Archived mode: only INCLUDE DICOM patient if hash IS archived.
        # All mode: pass through.
        if include == "active" and ph in archived_hashes:
            continue
        if include == "archived" and ph not in archived_hashes:
            continue
        by_hash[ph] = {
            "patient_hash":      ph,
            "patient_age_group": r[1] or "",
            "patient_sex":       r[2] or "",
            "study_count":       int(r[3] or 0),
            "latest_study_date": r[4] or "",
            "latest_modality":   r[5] or "",
            "last_seen_at":      int(r[6] or 0),
        }
    for mr in manual_rows:
        ph = mr[0]
        if ph in by_hash:
            d = by_hash[ph]
            if not d["patient_age_group"]:
                d["patient_age_group"] = mr[1] or ""
            if not d["patient_sex"]:
                d["patient_sex"] = mr[2] or ""
            d["last_seen_at"] = max(
                d["last_seen_at"], int(mr[3] or 0))
            d["initials"]   = mr[4] or d.get("initials", "")
            d["mrn"]        = mr[5] or d.get("mrn", "")
            d["created_at"] = min(
                d.get("created_at", int(mr[3] or 0)) or int(mr[3] or 0),
                int(mr[3] or 0),
            )
        else:
            by_hash[ph] = {
                "patient_hash":      ph,
                "patient_age_group": mr[1] or "",
                "patient_sex":       mr[2] or "",
                "study_count":       0,
                "latest_study_date": "",
                "latest_modality":   "",
                "last_seen_at":      int(mr[3] or 0),
                "initials":          mr[4] or "",
                "mrn":               mr[5] or "",
                "created_at":        int(mr[3] or 0),
                "sequence_number":   0,
            }

    # Default created_at for dicom-only patients (no manual row).
    for d in by_hash.values():
        d.setdefault("initials", "")
        d.setdefault("mrn", "")
        d.setdefault("created_at", d["last_seen_at"])
        d.setdefault("sequence_number", 0)

    # Compute per-user sequence number — oldest patient gets #1, next #2,
    # etc. Stable across UI re-orders because we key off created_at, not
    # last_seen_at. The medic's "patient #3" handle stays meaningful even
    # if they revisit older patients and bump them to the top of the rail.
    in_creation_order = sorted(by_hash.values(),
                               key=lambda x: (x["created_at"], x["patient_hash"]))
    for seq, d in enumerate(in_creation_order, start=1):
        d["sequence_number"] = seq

    result = [
        PatientCard(**d)
        for d in sorted(
            by_hash.values(),
            key=lambda x: x["last_seen_at"],
            reverse=True,
        )
    ]
    # #190 — DETAILED diagnostic. Dump first 3 rows from each table
    # so when the rail is empty we can tell if it's a query/auth/data
    # problem at a glance.
    logger.info(
        "[diag] list_patients user=%s → dicom_rows=%d manual_rows=%d merged=%d",
        current_user, len(dicom_rows), len(manual_rows), len(result),
    )
    for i, r in enumerate(dicom_rows[:3]):
        logger.info(
            "[diag]   dicom[%d] phash=%s age=%s sex=%s studies=%s last_seen=%s",
            i, r[0], r[1], r[2], r[3], r[6],
        )
    for i, r in enumerate(manual_rows[:3]):
        logger.info(
            "[diag]   manual[%d] phash=%s age=%s sex=%s created=%s",
            i, r[0], r[1], r[2], r[3],
        )
    for i, c in enumerate(result[:3]):
        logger.info(
            "[diag]   merged[%d] phash=%s age=%s sex=%s studies=%s",
            i, c.patient_hash, c.patient_age_group, c.patient_sex,
            c.study_count,
        )
    return result


# ────────────────────────────────────────────────────────────────
# #190 debug-only endpoint. No auth — returns raw DB row counts for
# every user_id present so we can confirm data is there even when
# the authenticated /patients query returns 0. Remove or auth-gate
# once the rail-empty bug is rooted out.
@router.get("/__debug/all-patients")
async def debug_all_patients() -> dict:
    """Return raw row counts grouped by user_id from BOTH tables.
    No auth — for debugging only."""
    try:
        from nexus_server.patients_router import init_patients_table
        init_patients_table()
    except Exception as exc:
        logger.debug("init_patients_table failed: %s", exc)
    out: dict = {"dicom_studies": [], "patients": []}
    conn = _index_db()
    try:
        try:
            for r in conn.execute(
                """
                SELECT user_id,
                       COUNT(*) AS n,
                       COALESCE(NULLIF(patient_hash, ''), '_anon') AS phash
                FROM dicom_studies
                GROUP BY user_id, phash
                """
            ).fetchall():
                out["dicom_studies"].append({
                    "user_id": r[0], "count": r[1], "phash": r[2],
                })
        except Exception as e:  # noqa: BLE001
            out["dicom_studies_error"] = f"{type(e).__name__}: {e}"
    finally:
        conn.close()
    # F-merge-patients-db — `patients` moved to the shared DB.
    try:
        from nexus_server.database import get_db_connection
        with get_db_connection() as _shared_conn:
            for r in _shared_conn.execute(
                """
                SELECT user_id, patient_hash, initials, mrn, age_group, sex,
                       created_at
                FROM patients
                """
            ).fetchall():
                out["patients"].append({
                    "user_id":      r[0],
                    "patient_hash": r[1],
                    "initials":     r[2],
                    "mrn":          r[3],
                    "age_group":    r[4],
                    "sex":          r[5],
                    "created_at":   r[6],
                })
    except Exception as e:  # noqa: BLE001
        out["patients_error"] = f"{type(e).__name__}: {e}"
    logger.info("[diag] __debug/all-patients → dicom=%d manual=%d",
                len(out["dicom_studies"]), len(out["patients"]))
    return out


@router.get("/patients/{patient_hash}/studies",
            response_model=list[StudyInfo])
async def list_patient_studies(
    patient_hash: str,
    current_user: str = Depends(get_current_user),
) -> list[StudyInfo]:
    """#174 — drill-down to all studies for one patient, newest-first.
    The desktop renders this as the timeline INSIDE a patient card
    when the medic expands it.

    Supports the synthetic '_anonymous' bucket — when invoked with
    that hash we return all studies with empty patient_hash.
    """
    conn = _index_db()
    try:
        if patient_hash == "_anonymous":
            where = "user_id = ? AND (patient_hash = '' OR patient_hash IS NULL)"
            params = (current_user,)
        else:
            where = "user_id = ? AND patient_hash = ?"
            params = (current_user, patient_hash)
        rows = conn.execute(
            f"SELECT study_id, study_instance_uid, study_date, "
            f"study_description, modality, patient_hash, "
            f"patient_age_group, patient_sex, created_at "
            f"FROM dicom_studies WHERE {where} "
            f"ORDER BY created_at DESC",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [
        StudyInfo(
            study_id=r[0], study_instance_uid=r[1] or "",
            study_date=r[2] or "", study_description=r[3] or "",
            modality=r[4] or "", patient_hash=r[5] or "",
            patient_age_group=r[6] or "", patient_sex=r[7] or "",
            series=[],   # not joined here — call /studies/{id} for series
            created_at=int(r[8] or 0),
        )
        for r in rows
    ]


@router.get("/studies", response_model=list[StudyInfo])
async def list_studies(
    current_user: str = Depends(get_current_user),
) -> list[StudyInfo]:
    """All DICOM studies for the current user, newest-first."""
    conn = _index_db()
    try:
        rows = conn.execute(
            "SELECT study_id, study_instance_uid, study_date, "
            "study_description, modality, patient_hash, "
            "patient_age_group, patient_sex, created_at "
            "FROM dicom_studies WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (current_user,),
        ).fetchall()
        result: list[StudyInfo] = []
        for r in rows:
            sid = r[0]
            series_rows = conn.execute(
                "SELECT series_id, series_instance_uid, series_number, "
                "modality, body_part, series_description, default_wl, "
                "default_ww, instance_count FROM dicom_series "
                "WHERE study_id = ? ORDER BY series_number",
                (sid,),
            ).fetchall()
            result.append(StudyInfo(
                study_id=sid,
                study_instance_uid=r[1],
                study_date=r[2] or "",
                study_description=r[3] or "",
                modality=r[4] or "",
                patient_hash=r[5] or "",
                patient_age_group=r[6] or "",
                patient_sex=r[7] or "",
                created_at=r[8] or 0,
                series=[SeriesInfo(
                    series_id=s[0],
                    series_instance_uid=s[1],
                    series_number=s[2],
                    modality=s[3] or "",
                    body_part=s[4] or "",
                    series_description=s[5] or "",
                    default_wl=s[6],
                    default_ww=s[7],
                    instance_count=s[8] or 0,
                ) for s in series_rows],
            ))
        return result
    finally:
        conn.close()


@router.get("/studies/{study_id}", response_model=StudyInfo)
async def get_study(
    study_id: str,
    current_user: str = Depends(get_current_user),
) -> StudyInfo:
    studies = await list_studies(current_user=current_user)
    for s in studies:
        if s.study_id == study_id:
            return s
    raise HTTPException(404, "Study not found")


# ── Render endpoint (#142) ────────────────────────────────────────────


@router.get("/studies/{study_id}/series/{series_id}/render")
async def render_endpoint(
    study_id: str,
    series_id: str,
    kind: str = "slice",
    slice: int = 0,
    window: str = "default",
    wl: Optional[float] = None,
    ww: Optional[float] = None,
    current_user: str = Depends(get_current_user),
) -> Response:
    """Render an axial slice / MIP / 4×4 grid as PNG.

    Validates the study belongs to the calling user. The viewer
    polls this with slice / window changes — keep it cheap (no LLM
    calls, ~50 ms per render).
    """
    from nexus_server.dicom import (
        load_study,
        render_grid_png,
        render_mip_png,
        render_slice_png,
    )

    study = load_study(current_user, study_id)
    if study is None:
        raise HTTPException(404, "Study not found")

    series = next(
        (s for s in study.series if _series_id_match(current_user, study_id, s.series_instance_uid, series_id)),
        None,
    )
    if series is None:
        raise HTTPException(404, "Series not found")

    # #158 — eager cache lookup. The upload-time prerender writes every
    # slice in the primary series to <preview_dir>/slices/{idx}-{preset}.png
    # at 768 px. If the request matches that cached preset AND no WL/WW
    # override is requested (i.e. the medic is using the standard preset
    # window, not dragging manually), serve directly from disk — ~5 ms
    # vs ~50 ms for live render.
    if kind == "slice" and wl is None and ww is None:
        try:
            import sqlite3 as _sql

            from nexus_server.dicom import _index_db_path
            conn = _sql.connect(_index_db_path())
            try:
                row = conn.execute(
                    "SELECT extract_dir FROM dicom_studies "
                    "WHERE user_id = ? AND study_id = ?",
                    (current_user, study_id),
                ).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                from pathlib import Path as _P
                cache_path = (
                    _P(row[0]) / "previews" / "slices" /
                    f"{int(slice)}-{window}.png"
                )
                if cache_path.exists():
                    try:
                        return Response(
                            content=cache_path.read_bytes(),
                            media_type="image/png",
                            headers={"X-Nexus-Cache": "eager"},
                        )
                    except OSError as exc:
                        logger.debug("reading cached render failed: %s", exc)  # fall through to live render
        except Exception as e:  # noqa: BLE001
            logger.debug("eager cache lookup failed for %s: %s", study_id, e)

    try:
        if kind == "mip":
            png = render_mip_png(series, preset=window)
        elif kind == "grid":
            png = render_grid_png(series, preset=window)
        elif kind == "slice":
            png = render_slice_png(
                series, slice_idx=slice, preset=window,
                wl_override=wl, ww_override=ww,
            )
        else:
            raise HTTPException(400, f"Unknown render kind: {kind}")
    except Exception as e:  # noqa: BLE001
        logger.exception("DICOM render failed: %s", e)
        raise HTTPException(500, f"Render failed: {e}")

    return Response(content=png, media_type="image/png")


def _series_id_match(user_id: str, study_id: str,
                     series_instance_uid: str, target_series_id: str) -> bool:
    """Check series_id ↔ series_instance_uid mapping in the DB."""
    conn = _index_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM dicom_series WHERE series_id = ? "
            "AND series_instance_uid = ? AND study_id = ?",
            (target_series_id, series_instance_uid, study_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ── ROI / contour storage (#144) ──────────────────────────────────────


# Valid kinds for ROI typing. Reject anything else so the
# downstream RTSTRUCT exporter (#146) doesn't have to defend
# against arbitrary strings.
ROI_KINDS = {"GTV", "CTV", "PTV", "OAR", "POI", "OTHER"}


def init_rt_tables() -> None:
    """Create rt_rois + rt_contours tables in the DICOM index DB.

    Same shape used by the importer/exporter (#146); both tables
    sit alongside dicom_studies so a single DB backup carries the
    whole RT package.
    """
    conn = _index_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rt_rois (
                roi_id      TEXT PRIMARY KEY,
                study_id    TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                name        TEXT NOT NULL,         -- 'GTV-T', 'Heart', 'Lung-L', ...
                kind        TEXT NOT NULL,         -- GTV | CTV | PTV | OAR | POI | OTHER
                color_hex   TEXT NOT NULL DEFAULT '#FF6B6B',
                margin_mm   REAL DEFAULT NULL,     -- non-null for derived CTV/PTV
                derived_from TEXT DEFAULT NULL,     -- parent roi_id if margin-expanded
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL,
                FOREIGN KEY (study_id) REFERENCES dicom_studies(study_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rt_rois_study
            ON rt_rois(user_id, study_id)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rt_contours (
                contour_id  TEXT PRIMARY KEY,
                roi_id      TEXT NOT NULL,
                series_id   TEXT NOT NULL,
                slice_idx   INTEGER NOT NULL,       -- 0-based after z-sort
                polygon_points TEXT NOT NULL,       -- JSON: [[x,y], ...] in image pixel coords
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL,
                FOREIGN KEY (roi_id) REFERENCES rt_rois(roi_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rt_contours_roi_slice
            ON rt_contours(roi_id, series_id, slice_idx)
        """)
        conn.commit()
    finally:
        conn.close()


class RoiCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    kind: str = Field(..., description="GTV | CTV | PTV | OAR | POI | OTHER")
    color_hex: str = Field("#FF6B6B", pattern=r"^#[0-9A-Fa-f]{6}$")
    margin_mm: Optional[float] = None
    derived_from: Optional[str] = None


class RoiInfo(BaseModel):
    roi_id: str
    study_id: str
    name: str
    kind: str
    color_hex: str
    margin_mm: Optional[float]
    derived_from: Optional[str]
    contour_count: int
    created_at: int
    updated_at: int


class ContourUpsertRequest(BaseModel):
    series_id: str
    slice_idx: int = Field(..., ge=0)
    polygon_points: list[list[float]] = Field(
        ..., min_length=3, max_length=1000,
        description="[[x,y], ...] image-pixel coords; 3 points minimum for a polygon",
    )


def _study_belongs_to_user(user_id: str, study_id: str) -> bool:
    conn = _index_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM dicom_studies WHERE study_id = ? AND user_id = ?",
            (study_id, user_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


@router.get("/studies/{study_id}/rois", response_model=list[RoiInfo])
async def list_rois(
    study_id: str,
    current_user: str = Depends(get_current_user),
) -> list[RoiInfo]:
    if not _study_belongs_to_user(current_user, study_id):
        raise HTTPException(404, "Study not found")
    conn = _index_db()
    try:
        rows = conn.execute(
            "SELECT r.roi_id, r.study_id, r.name, r.kind, r.color_hex, "
            "r.margin_mm, r.derived_from, r.created_at, r.updated_at, "
            "(SELECT COUNT(*) FROM rt_contours c WHERE c.roi_id = r.roi_id) "
            "FROM rt_rois r WHERE r.user_id = ? AND r.study_id = ? "
            "ORDER BY r.created_at",
            (current_user, study_id),
        ).fetchall()
        return [RoiInfo(
            roi_id=r[0], study_id=r[1], name=r[2], kind=r[3],
            color_hex=r[4], margin_mm=r[5], derived_from=r[6],
            created_at=r[7], updated_at=r[8],
            contour_count=r[9],
        ) for r in rows]
    finally:
        conn.close()


@router.post("/studies/{study_id}/rois", response_model=RoiInfo)
async def create_roi(
    study_id: str,
    req: RoiCreateRequest,
    current_user: str = Depends(get_current_user),
) -> RoiInfo:
    if not _study_belongs_to_user(current_user, study_id):
        raise HTTPException(404, "Study not found")
    if req.kind not in ROI_KINDS:
        raise HTTPException(
            400, f"kind must be one of {sorted(ROI_KINDS)}",
        )
    roi_id = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    conn = _index_db()
    try:
        conn.execute(
            "INSERT INTO rt_rois (roi_id, study_id, user_id, name, kind, "
            "color_hex, margin_mm, derived_from, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (roi_id, study_id, current_user, req.name, req.kind,
             req.color_hex, req.margin_mm, req.derived_from,
             now_ms, now_ms),
        )
        conn.commit()
    finally:
        conn.close()
    return RoiInfo(
        roi_id=roi_id, study_id=study_id, name=req.name, kind=req.kind,
        color_hex=req.color_hex, margin_mm=req.margin_mm,
        derived_from=req.derived_from, contour_count=0,
        created_at=now_ms, updated_at=now_ms,
    )


@router.post("/studies/{study_id}/rois/{roi_id}/contours")
async def upsert_contour(
    study_id: str,
    roi_id: str,
    req: ContourUpsertRequest,
    current_user: str = Depends(get_current_user),
) -> dict:
    """Upsert a contour: one polygon per (roi_id, series_id, slice_idx).

    Re-drawing the polygon on the same slice replaces the previous
    one. Use POST to a delete endpoint (or empty polygon) to remove.
    """
    if not _study_belongs_to_user(current_user, study_id):
        raise HTTPException(404, "Study not found")
    conn = _index_db()
    try:
        # Confirm the ROI belongs to this user + study
        row = conn.execute(
            "SELECT 1 FROM rt_rois WHERE roi_id = ? AND user_id = ? AND study_id = ?",
            (roi_id, current_user, study_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "ROI not found")
        now_ms = int(time.time() * 1000)
        # Upsert: same (roi_id, series_id, slice_idx) key replaces.
        conn.execute(
            "DELETE FROM rt_contours WHERE roi_id = ? AND series_id = ? "
            "AND slice_idx = ?",
            (roi_id, req.series_id, req.slice_idx),
        )
        conn.execute(
            "INSERT INTO rt_contours (contour_id, roi_id, series_id, "
            "slice_idx, polygon_points, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), roi_id, req.series_id, req.slice_idx,
             json.dumps(req.polygon_points), now_ms, now_ms),
        )
        # Bump ROI's updated_at
        conn.execute(
            "UPDATE rt_rois SET updated_at = ? WHERE roi_id = ?",
            (now_ms, roi_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@router.delete("/studies/{study_id}/rois/{roi_id}")
async def delete_roi(
    study_id: str,
    roi_id: str,
    current_user: str = Depends(get_current_user),
) -> dict:
    if not _study_belongs_to_user(current_user, study_id):
        raise HTTPException(404, "Study not found")
    conn = _index_db()
    try:
        conn.execute(
            "DELETE FROM rt_contours WHERE roi_id = ?", (roi_id,),
        )
        cur = conn.execute(
            "DELETE FROM rt_rois WHERE roi_id = ? AND user_id = ? "
            "AND study_id = ?",
            (roi_id, current_user, study_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "ROI not found")
    finally:
        conn.close()
    return {"ok": True}


@router.get("/studies/{study_id}/rois/{roi_id}/contours")
async def list_contours(
    study_id: str,
    roi_id: str,
    series_id: Optional[str] = None,
    current_user: str = Depends(get_current_user),
) -> list[dict]:
    """All contours of one ROI. Optional ``series_id`` filter so the
    viewer only fetches contours for the series currently displayed.
    """
    if not _study_belongs_to_user(current_user, study_id):
        raise HTTPException(404, "Study not found")
    conn = _index_db()
    try:
        if series_id:
            rows = conn.execute(
                "SELECT c.contour_id, c.series_id, c.slice_idx, "
                "c.polygon_points, c.created_at, c.updated_at "
                "FROM rt_contours c WHERE c.roi_id = ? AND c.series_id = ? "
                "ORDER BY c.slice_idx",
                (roi_id, series_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT c.contour_id, c.series_id, c.slice_idx, "
                "c.polygon_points, c.created_at, c.updated_at "
                "FROM rt_contours c WHERE c.roi_id = ? "
                "ORDER BY c.series_id, c.slice_idx",
                (roi_id,),
            ).fetchall()
        return [{
            "contour_id":     r[0],
            "series_id":      r[1],
            "slice_idx":      r[2],
            "polygon_points": json.loads(r[3]),
            "created_at":     r[4],
            "updated_at":     r[5],
        } for r in rows]
    finally:
        conn.close()


# ── RTSTRUCT import/export (#146) ─────────────────────────────────────


@router.get("/studies/{study_id}/rtstruct.dcm")
async def export_rtstruct(
    study_id: str,
    current_user: str = Depends(get_current_user),
) -> Response:
    """Export all ROIs as a DICOM RTSTRUCT file.

    Output is a single .dcm following DICOM PS3.3 RT Structure Set
    IOD. Medic downloads, hand-carries (USB / institution PACS
    web upload) into their treatment planning system (Eclipse,
    Monaco, Pinnacle, ...).
    """
    if not _study_belongs_to_user(current_user, study_id):
        raise HTTPException(404, "Study not found")
    try:
        from nexus_server.dicom_rtstruct import export_rtstruct_for_study
        dcm_bytes = export_rtstruct_for_study(current_user, study_id)
    except ImportError as e:
        raise HTTPException(
            500, f"RTSTRUCT exporter unavailable: {e}",
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("RTSTRUCT export failed: %s", e)
        raise HTTPException(500, f"Export failed: {e}")

    fn = f"nexus-rtstruct-{study_id[:8]}.dcm"
    return Response(
        content=dcm_bytes,
        media_type="application/dicom",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


class RtStructImportRequest(BaseModel):
    """Imports a medic-uploaded RTSTRUCT.dcm. The .dcm bytes come
    in as a base64 string in the request body (uploaded via the
    main files.upload route first; then this endpoint takes the
    file_id and reads the bytes from disk)."""
    file_id: str = Field(..., description="file_id from /files/upload")


@router.post("/studies/{study_id}/rtstruct/import")
async def import_rtstruct(
    study_id: str,
    req: RtStructImportRequest,
    current_user: str = Depends(get_current_user),
) -> dict:
    if not _study_belongs_to_user(current_user, study_id):
        raise HTTPException(404, "Study not found")

    # Resolve file_id → bytes via the files module.
    try:
        from nexus_server import files as _files
        rows = _files.resolve_files(current_user, [req.file_id])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"File lookup failed: {e}")
    if not rows:
        raise HTTPException(404, "Upload not found")
    disk_path = rows[0].get("disk_path")
    if not disk_path:
        raise HTTPException(404, "Upload has no disk_path")
    try:
        with open(disk_path, "rb") as fh:
            dcm_bytes = fh.read()
    except OSError as e:
        raise HTTPException(500, f"Could not read upload bytes: {e}")

    try:
        from nexus_server.dicom_rtstruct import import_rtstruct_bytes
        n_rois, n_contours = import_rtstruct_bytes(
            current_user, study_id, dcm_bytes,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("RTSTRUCT import failed: %s", e)
        raise HTTPException(500, f"Import failed: {e}")
    return {"ok": True, "rois_imported": n_rois, "contours_imported": n_contours}


# ── SAM auto-segment stub (#145) ──────────────────────────────────────


class SamSegmentRequest(BaseModel):
    series_id: str
    slice_idx: int
    # SAM is a "click + box" segmenter; the medic clicks a point
    # to indicate the structure and optionally draws a coarse
    # bounding box. The model expands these into a precise polygon.
    point_x: float
    point_y: float
    box: Optional[list[float]] = Field(
        None,
        description="[x1, y1, x2, y2] coarse bounding box, optional",
    )


@router.post("/studies/{study_id}/series/{series_id}/sam")
async def sam_segment(
    study_id: str,
    series_id: str,
    req: SamSegmentRequest,
    current_user: str = Depends(get_current_user),
) -> dict:
    """AI auto-segmentation via local SAM (Segment Anything).

    The medic clicks a point (or drags a box) on a slice in the
    viewer; we render that slice to PNG (lung window by default),
    feed it + the prompt into the SAM ONNX model, and return a
    polygon the viewer overlays as a candidate ROI. The medic then
    accepts / edits / rejects — the gesture flows through #130
    feedback so the eventual model adaptation has real training
    data per skill folder.

    Returns 503 with a friendly hint when the model files haven't
    been downloaded yet (first-call gate). The desktop calls
    /sam/download to fetch them.
    """
    if not _study_belongs_to_user(current_user, study_id):
        raise HTTPException(404, "Study not found")

    from nexus_server import dicom_sam
    available, reason = dicom_sam.is_available()
    if not available:
        # 503 = service unavailable. Tells the desktop to surface
        # a "Download SAM model (~85 MB)" button rather than fail
        # silently.
        raise HTTPException(503, reason)

    # Render the underlying slice so SAM has pixels to work with.
    # Use lung window for chest CT (the common case); medic can
    # override via wl/ww query if they need a different view.
    try:
        from nexus_server.dicom import load_study, render_slice_png
    except ImportError as e:
        raise HTTPException(500, f"DICOM module unavailable: {e}")
    study = load_study(current_user, study_id)
    if study is None:
        raise HTTPException(404, "Study not found")
    series = next(
        (s for s in study.series
         if _series_id_match(current_user, study_id,
                             s.series_instance_uid, series_id)),
        None,
    )
    if series is None:
        raise HTTPException(404, "Series not found")
    try:
        png = render_slice_png(series, req.slice_idx, preset="lung")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Slice render for SAM failed: {e}")

    try:
        if req.box and len(req.box) == 4:
            polygon = dicom_sam.segment_from_box(
                png, (req.box[0], req.box[1], req.box[2], req.box[3]),
            )
        else:
            polygon = dicom_sam.segment_from_point(
                png, req.point_x, req.point_y,
            )
    except Exception as e:  # noqa: BLE001
        logger.exception("SAM inference failed: %s", e)
        raise HTTPException(500, f"SAM inference failed: {e}")

    return {
        "ok": True,
        "polygon_points": polygon,
        "model": "sam-vit-b-onnx",
        "point_count": len(polygon),
    }


@router.post("/sam/download")
async def download_sam_models(
    current_user: str = Depends(get_current_user),
) -> dict:
    """Trigger SAM ONNX model download. ~85 MB, one-time, lands in
    $RUNE_HOME/.nexus/models/sam/.

    Returns immediately if the files already exist. Otherwise
    blocks until the download completes (synchronous because the
    desktop button shows a spinner — we want the response to
    arrive after the model is actually usable, not before).
    """
    from nexus_server import dicom_sam
    available, _reason = dicom_sam.is_available()
    if available:
        return {"ok": True, "status": "already_present"}
    try:
        dicom_sam.ensure_models_downloaded()
    except Exception as e:  # noqa: BLE001
        logger.exception("SAM download failed: %s", e)
        raise HTTPException(500, f"Download failed: {e}")
    return {
        "ok": True,
        "status": "downloaded",
        "encoder_path": str(dicom_sam.encoder_path()),
        "decoder_path": str(dicom_sam.decoder_path()),
    }


@router.get("/sam/status")
async def sam_status(
    current_user: str = Depends(get_current_user),
) -> dict:
    """Quick check whether SAM is ready. Lets the desktop conditionally
    show "Download SAM" vs "AI segment" buttons."""
    from nexus_server import dicom_sam
    available, reason = dicom_sam.is_available()
    return {
        "available": available,
        "reason": reason,
        "encoder_present": dicom_sam.encoder_path().exists(),
        "decoder_present": dicom_sam.decoder_path().exists(),
    }


# ── #161: Send-to-agent intent queue ──────────────────────────────────
#
# The DICOM viewer (#157 rewrite) now runs in a standalone Chrome / Brave
# / Vivaldi `--app` window, NOT a WebView. Means the previous in-process
# bridge (window.chrome.webview.postMessage / WKWebView messageHandlers)
# is gone — the viewer can't talk to the desktop directly.
#
# Solution: per-user FIFO queue, in-memory on the server. The viewer
# POSTs each "Send to agent" click here; the desktop polls
# /dicom/pending-sends every 1-2 seconds and drains it. Both endpoints
# auth-gate by user so different medics on the same server can't see
# each other's intents.
#
# In-memory only: a server restart drops anything queued but unsent.
# That's acceptable — the medic can just re-click in the viewer. If we
# need durability later (e.g. shared tenant deploys), this graduates to
# a SQL-backed queue.

import threading as _threading

_send_to_agent_queue: dict[str, list[dict]] = {}
_send_to_agent_lock = _threading.Lock()


class SendToAgentRequest(BaseModel):
    """Body shape for POST /dicom/send-to-agent — mirrors what the
    viewer's old postToHost(msg) payload was emitting so the desktop
    handler logic doesn't have to change."""
    study_id:   str
    series_id:  str
    slice_idx:  int
    window:     str = "default"
    wl:         Optional[float] = None
    ww:         Optional[float] = None
    is_last:    bool = True
    batch_size: int = 1
    note:       str = ""        # optional text the medic typed alongside


@router.post("/send-to-agent")
async def send_to_agent_enqueue(
    req: SendToAgentRequest,
    current_user: str = Depends(get_current_user),
) -> dict:
    """Viewer-side endpoint: enqueue a 'show this slice to the agent'
    intent. Returns immediately; the desktop poll loop picks it up.
    """
    item = req.model_dump()
    item["enqueued_at"] = time.time()
    with _send_to_agent_lock:
        q = _send_to_agent_queue.setdefault(current_user, [])
        # Cap per-user queue at 50 — protects against a hostile or
        # spinning viewer page that keeps POSTing.
        if len(q) >= 50:
            q.pop(0)
        q.append(item)
    logger.info(
        "send-to-agent enqueued for user %s: study=%s slice=%d note=%s",
        current_user, req.study_id[:8], req.slice_idx,
        (req.note or "")[:40],
    )
    return {"ok": True, "queued": len(q)}


@router.get("/studies/{study_id}/patient-context")
async def get_patient_context(
    study_id: str,
    current_user: str = Depends(get_current_user),
) -> dict:
    """#162 — return the formatted patient context block for a study.

    Desktop uses this to prepend patient identity + study timeline
    to the default prompt when sending a slice to the agent (so the
    agent never treats a viewer slice as belonging to "some random
    patient"). Empty string when the study isn't found OR the patient
    has no demographic info — caller falls back to a generic prompt.
    """
    from nexus_server.dicom import get_patient_context_block
    text = get_patient_context_block(current_user, study_id)
    return {"text": text, "study_id": study_id}


@router.get("/pending-sends")
async def pending_sends_drain(
    current_user: str = Depends(get_current_user),
) -> dict:
    """Desktop-side endpoint: return everything queued for this user
    and clear the queue in one atomic step. Desktop polls ~1Hz.
    """
    with _send_to_agent_lock:
        q = _send_to_agent_queue.pop(current_user, [])
    return {"items": q, "count": len(q)}
