"""POST /api/v1/report/pdf — clinical report PDF export.

Mirrors the existing ``export_router.py`` pattern: builds a file
under the user's Archive directory and returns the resolved path +
size + creation timestamp. The desktop's ReportMode renders a
"Last report" card from this response (path + Open Folder button)
so the medic always knows where the file went — the very thing the
previous ``window.print()`` flow failed to do.

The PDF builder itself lives in ``report_pdf.py``. This module just
handles HTTP-side concerns: auth, validation, filename, response
shape.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus_server.auth import get_current_user
from nexus_server.export_router import _archive_dir
from nexus_server.report_pdf import (
    ReportDraftInput,
    ReportPatientHeader,
    build_report_pdf,
    default_pdf_name,
    reports_dir,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/report", tags=["report"])


# ─────────────────────────────────────────────────────────────────────
# Wire shapes
# ─────────────────────────────────────────────────────────────────────


class NodeRef(BaseModel):
    """One selected Layer-1 node (finding or differential) the medic
    chose to include in the report. Resolved client-side from the
    projection — backend doesn't re-fetch (keeps the PDF route from
    being a partial replay of the chat-ingester pipeline)."""

    node_id: Optional[int] = None
    label:   str           = ""
    urgency: Optional[str] = None


class ReportPdfRequest(BaseModel):
    """Body shape for POST /api/v1/report/pdf.

    Matches desktop-v2 ``ReportMode``'s editing state — the client
    sends ``draft`` (the medic's free-text fields) + selected node
    refs + the patient header. We don't reach back into the
    projection from this endpoint so a stale draft on the client
    never silently picks up newer findings the medic didn't intend
    to include.
    """

    patient_hash:  str = Field(..., min_length=1)

    # Patient header for the PDF top — pseudonymous only (no MRN / DOB).
    patient_label:        str = Field(default="")
    patient_sex:          str = Field(default="")
    patient_age_group:    str = Field(default="")
    latest_modality:      str = Field(default="")
    latest_study_dt:      str = Field(default="")

    # Medic-edited fields.
    clinical_info:        str = Field(default="", max_length=20_000)
    impression:           str = Field(default="", max_length=20_000)
    recommendation:       str = Field(default="", max_length=20_000)

    # Selected nodes (already filtered client-side).
    findings:        list[NodeRef] = Field(default_factory=list)
    differentials:   list[NodeRef] = Field(default_factory=list)

    # 'zh-CN' | 'en-US' — picks PDF section labels.
    locale:          str = Field(default="zh-CN")


class ReportPdfResponse(BaseModel):
    """Symmetric with export_router's BundleResponse. UI hangs the
    "Last report" card off this — path is shown verbatim + Open
    Folder button uses it."""

    path:       str   # absolute filesystem path
    bytes:      int
    created_at: int   # unix seconds
    patient_hash: str
    locale:     str


# ─────────────────────────────────────────────────────────────────────
# Route
# ─────────────────────────────────────────────────────────────────────


@router.post(
    "/pdf",
    response_model=ReportPdfResponse,
    status_code=status.HTTP_200_OK,
)
async def export_report_pdf(
    req: ReportPdfRequest,
    user_id: str = Depends(get_current_user),
) -> ReportPdfResponse:
    """Build one report PDF and return where it landed.

    Path resolution:
      ``<archive_dir>/Reports/<patient_hash[:8]>-<unix_ts>.pdf``

    Auth: any signed-in medic can export. There's no per-row ownership
    check on ``patient_hash`` here because the entire request body is
    medic-supplied — the PDF is purely a layout of the request
    payload, not a database read. The medic can't "steal" another
    user's data via this endpoint because the data they're asking us
    to PDF-format is data they typed.
    """
    try:
        archive = _archive_dir()
    except Exception as exc:  # noqa: BLE001
        logger.exception("archive dir resolve failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Cannot resolve archive directory. Tried "
                "$NEXUS_ARCHIVE_DIR, ~/Documents/Nexus Archive, "
                "~/.nexus_server/archive."
            ),
        ) from exc

    rep_dir = reports_dir(archive)
    name    = default_pdf_name(req.patient_hash)
    out     = Path(rep_dir) / name

    # Build.
    try:
        size_bytes = build_report_pdf(
            patient=ReportPatientHeader(
                label           = req.patient_label,
                sex             = req.patient_sex,
                age_group       = req.patient_age_group,
                latest_modality = req.latest_modality,
                latest_study_dt = req.latest_study_dt,
            ),
            draft=ReportDraftInput(
                clinical_info  = req.clinical_info,
                impression     = req.impression,
                recommendation = req.recommendation,
                findings       = [n.model_dump() for n in req.findings],
                differentials  = [n.model_dump() for n in req.differentials],
            ),
            out_path = out,
            locale   = req.locale,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "report PDF build failed: user=%s patient=%s err=%s",
            user_id, req.patient_hash[:12], exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"PDF build failed: {type(exc).__name__}: {exc}",
        ) from exc

    logger.info(
        "report PDF exported: user=%s patient=%s bytes=%d path=%s",
        user_id, req.patient_hash[:12], size_bytes, str(out),
    )

    import os
    return ReportPdfResponse(
        path         = str(out),
        bytes        = size_bytes,
        created_at   = int(os.path.getmtime(out)),
        patient_hash = req.patient_hash,
        locale       = req.locale,
    )
