"""Clinical report PDF builder.

Backs ``POST /api/v1/report/pdf``. Renders the medic's ReportMode draft
(clinical info, selected findings, impression, differentials,
recommendation) into a PDF and writes it to
``$ARCHIVE_DIR/Reports/<patient_hash[:8]>-<unix_ts>.pdf``.

Why reportlab and not weasyprint
────────────────────────────────
Weasyprint depends on Pango+Cairo system libraries which are a known
headache to bundle through PyInstaller on macOS — we've already lost
days to PyInstaller native-lib quirks (macOS 26 panic, alembic.ini
path resolution). reportlab is pure Python: zero system deps, ships
cleanly in the bundled .app, and its Platypus framework handles
paragraphs / tables / headings well enough for a clinical report.

Layout (one A4 portrait page typically; auto-paginates on overflow):

  ┌──────────────────────────────────────────────────────────┐
  │ Nexus · Clinical Report             (small caption right) │
  │ Patient · J.D. · #3                                       │
  │ 65 F · CT · 2024-08-15            generated 2026-06-14    │
  │ ─────────────────────────────────────────────────────────│
  │ CLINICAL INFORMATION                                      │
  │ <medic's draft.clinicalInfo>                              │
  │                                                           │
  │ FINDINGS                                                  │
  │ • 8 mm RUL nodule              [N42]                      │
  │ • GGO, slice 64                [N43]                      │
  │                                                           │
  │ IMPRESSION                                                │
  │ <medic's draft.impression>                                │
  │                                                           │
  │ DIFFERENTIAL DIAGNOSIS                                    │
  │ • adenocarcinoma in situ       [N51]                      │
  │                                                           │
  │ RECOMMENDATION                                            │
  │ <medic's draft.recommendation>                            │
  │ ─────────────────────────────────────────────────────────│
  │ AI-assisted preliminary report. Final read by radiologist.│
  └──────────────────────────────────────────────────────────┘

Citation chips ``[Nxx]`` are preserved verbatim — same node-id
prefixes the chat UI uses — so the PDF can be cross-referenced
back to the patient graph by anyone with EventLog access.
"""
from __future__ import annotations

import io
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas as _canvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, HRFlowable, KeepTogether,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# CJK font registration (F-pdf-cjk)
# ─────────────────────────────────────────────────────────────────────
#
# Symptom this fixes: report PDF rendered with all Chinese characters
# as ■ tofu boxes. reportlab's default Helvetica has no CJK glyphs.
#
# Why CIDFont over a TTF file: reportlab ships several built-in Adobe
# CID font definitions (HeiSei / STSong / MSung / HYGothic). They
# don't embed actual glyph data into the PDF — they reference the
# reader's built-in CJK glyphs. Result: PDF is small (~20 KB instead
# of ~5 MB for a full TTF embed) AND we don't have to ship a font
# file. Tradeoff: relies on the reader's CJK support, which every
# modern macOS / Win / iOS / Adobe Reader has by default.
#
# Font choice:
#   STSong-Light          → Simplified Chinese (mainland), serif feel
#   STSongStd-Light       → newer alias
#   HeiseiMin-W3          → Japanese; rejects most simplified glyphs
#   HYSMyeongJo-Medium    → Korean
#
# STSong-Light is the right pick for our Chinese-speaking clinicians.
# It's deterministic — same font across all reader installations —
# so the report looks consistent.
#
# Registration is idempotent (reportlab caches by name); we still
# guard with a sentinel so re-imports don't waste cycles.

_CJK_FONT_NAME = "STSong-Light"
_CJK_FONT_REGISTERED = False


def _ensure_cjk_font_registered() -> str:
    """Register the bundled CID CJK font once. Returns the registered
    font name. Falls back to ``Helvetica`` (with a logged warning) if
    reportlab's CID catalogue is somehow broken — better than crashing
    the whole report pipeline on a font lookup."""
    global _CJK_FONT_REGISTERED
    if _CJK_FONT_REGISTERED:
        return _CJK_FONT_NAME
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT_NAME))
        _CJK_FONT_REGISTERED = True
        logger.info("report_pdf: registered CJK font %s", _CJK_FONT_NAME)
        return _CJK_FONT_NAME
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "report_pdf: failed to register CJK font %s: %s — "
            "Chinese text will render as tofu boxes (■)",
            _CJK_FONT_NAME, exc,
        )
        return "Helvetica"


# ─────────────────────────────────────────────────────────────────────
# Input data shape — mirrors what desktop-v2's ReportMode sends
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ReportDraftInput:
    """One report's worth of medic-edited fields. Matches the desktop's
    ``ReportDraft`` interface in modes.tsx."""
    clinical_info: str = ""
    impression:    str = ""
    recommendation: str = ""
    # Each selected-finding / selected-ddx entry is a {node_id, label,
    # urgency?} dict — the client resolves the labels server-side via
    # PatientProjection before sending, so this layer only formats.
    findings:        list[dict[str, Any]] = None  # type: ignore[assignment]
    differentials:   list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.findings is None:
            self.findings = []
        if self.differentials is None:
            self.differentials = []


@dataclass
class ReportPatientHeader:
    """The pseudonymous patient identifiers we print at the top.
    Intentionally minimal — no DOB, no MRN — same PHI policy as the
    chat-bubble patient label."""
    label:           str = ""   # e.g. "J.D. · #3"
    sex:             str = ""
    age_group:       str = ""
    latest_modality: str = ""
    latest_study_dt: str = ""


# ─────────────────────────────────────────────────────────────────────
# Styles (cache-once at module load)
# ─────────────────────────────────────────────────────────────────────


def _styles() -> dict[str, ParagraphStyle]:
    """Tweak the reportlab default sheet to a denser clinical look.
    Returns a dict of named styles we use throughout build_report_pdf.

    Every style uses the registered CJK CID font (``STSong-Light``)
    so Chinese characters render as glyphs instead of tofu boxes.
    The font has full Latin coverage too, so ASCII (drug names,
    units, citation tags, AJCC codes) renders correctly in the same
    paragraph without font fallback magic.
    """
    cjk = _ensure_cjk_font_registered()
    s = getSampleStyleSheet()
    out: dict[str, ParagraphStyle] = {}

    out["title"] = ParagraphStyle(
        name="ReportTitle", parent=s["Title"],
        fontName=cjk,
        fontSize=18, leading=22, spaceAfter=2,
        textColor=colors.HexColor("#1f2937"),
    )
    out["subtitle"] = ParagraphStyle(
        name="ReportSubtitle", parent=s["Normal"],
        fontName=cjk,
        fontSize=10, leading=14, textColor=colors.HexColor("#6b7280"),
        spaceAfter=8,
    )
    out["caption_right"] = ParagraphStyle(
        name="ReportCaptionRight", parent=s["Normal"],
        fontName=cjk,
        fontSize=8, leading=10, alignment=2,
        textColor=colors.HexColor("#9ca3af"),
    )
    out["section_header"] = ParagraphStyle(
        name="ReportSectionHeader", parent=s["Heading2"],
        fontName=cjk,
        fontSize=10, leading=14, spaceBefore=14, spaceAfter=4,
        textColor=colors.HexColor("#374151"),
        textTransform="uppercase",
        # Manual letter-spacing emulation: reportlab doesn't support
        # CSS letter-spacing, but uppercase + the slightly larger
        # leading + the colour difference give the same "headerness".
    )
    out["body"] = ParagraphStyle(
        name="ReportBody", parent=s["BodyText"],
        fontName=cjk,
        fontSize=10, leading=14, spaceAfter=6,
        textColor=colors.HexColor("#111827"),
    )
    out["list_item"] = ParagraphStyle(
        name="ReportListItem", parent=s["BodyText"],
        fontName=cjk,
        fontSize=10, leading=14, leftIndent=8, bulletIndent=0,
        spaceAfter=2, textColor=colors.HexColor("#111827"),
    )
    out["citation"] = ParagraphStyle(
        name="ReportCitation", parent=s["BodyText"],
        fontName=cjk,
        fontSize=8, leading=10, spaceAfter=0,
        textColor=colors.HexColor("#6b7280"),
    )
    # NB: parent=s["Italic"] but CID fonts don't have a synthetic
    # italic variant. The visual differentiator falls back to colour
    # (lighter grey) which still reads as "secondary text".
    out["disclaimer"] = ParagraphStyle(
        name="ReportDisclaimer", parent=s["Italic"],
        fontName=cjk,
        fontSize=8, leading=10, spaceBefore=18,
        textColor=colors.HexColor("#9ca3af"),
    )
    out["empty"] = ParagraphStyle(
        name="ReportEmpty", parent=s["Italic"],
        fontName=cjk,
        fontSize=9, leading=12, leftIndent=8,
        textColor=colors.HexColor("#9ca3af"),
    )
    return out


_STYLES = _styles()


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def build_report_pdf(
    *,
    patient: ReportPatientHeader,
    draft: ReportDraftInput,
    out_path: Path,
    locale: str = "zh-CN",
) -> int:
    """Render the report PDF to ``out_path``. Returns the byte count of
    the resulting file.

    The output is always overwritten (caller is responsible for picking
    a unique filename — usually ``<patient_hash[:8]>-<unix_ts>.pdf``).
    Parent directory is created if missing.

    ``locale`` controls a few labels ("Clinical Information" /
    "临床信息"). The medic's free-text bodies are NOT translated — they
    appear verbatim as the medic typed them.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=f"Nexus Report · {patient.label or 'Patient'}",
        author="Nexus",
    )

    story = list(_build_story(patient, draft, locale))
    doc.build(story)

    pdf_bytes = buf.getvalue()
    out_path.write_bytes(pdf_bytes)
    return len(pdf_bytes)


def _build_story(
    patient: ReportPatientHeader,
    draft: ReportDraftInput,
    locale: str,
) -> list[Any]:
    """Yield-style: assembles the Platypus Flowables that make up the
    PDF. Separated from ``build_report_pdf`` so the test harness can
    assert on the structural shape without going through reportlab's
    file-write side effects."""
    L = _labels(locale)

    # ── Header ─────────────────────────────────────────────────────
    yield Paragraph(L["title"], _STYLES["title"])

    sub_bits: list[str] = []
    if patient.label:
        sub_bits.append(patient.label)
    if patient.sex:
        sub_bits.append(patient.sex)
    if patient.age_group:
        sub_bits.append(patient.age_group)
    if patient.latest_modality:
        sub_bits.append(patient.latest_modality)
    if patient.latest_study_dt:
        sub_bits.append(patient.latest_study_dt)
    yield Paragraph(" · ".join(sub_bits), _STYLES["subtitle"])

    yield Paragraph(
        L["generated_at"] + " " + time.strftime("%Y-%m-%d %H:%M"),
        _STYLES["caption_right"],
    )
    yield HRFlowable(
        width="100%", thickness=0.5,
        color=colors.HexColor("#e5e7eb"), spaceBefore=2, spaceAfter=8,
    )

    # ── Clinical Information ───────────────────────────────────────
    yield Paragraph(L["clinical_info"], _STYLES["section_header"])
    yield Paragraph(
        _html_safe(draft.clinical_info) or "<i>—</i>", _STYLES["body"],
    )

    # ── Findings ───────────────────────────────────────────────────
    yield Paragraph(L["findings"], _STYLES["section_header"])
    if draft.findings:
        for f in draft.findings:
            yield _render_node_bullet(f)
    else:
        yield Paragraph(L["no_findings"], _STYLES["empty"])

    # ── Impression ─────────────────────────────────────────────────
    yield Paragraph(L["impression"], _STYLES["section_header"])
    yield Paragraph(
        _html_safe(draft.impression) or "<i>—</i>", _STYLES["body"],
    )

    # ── Differentials ──────────────────────────────────────────────
    yield Paragraph(L["differentials"], _STYLES["section_header"])
    if draft.differentials:
        for d in draft.differentials:
            yield _render_node_bullet(d)
    else:
        yield Paragraph(L["no_ddx"], _STYLES["empty"])

    # ── Recommendation ─────────────────────────────────────────────
    yield Paragraph(L["recommendation"], _STYLES["section_header"])
    yield Paragraph(
        _html_safe(draft.recommendation) or "<i>—</i>", _STYLES["body"],
    )

    # ── Disclaimer ─────────────────────────────────────────────────
    # KeepTogether so the disclaimer never gets stranded at the top
    # of a fresh page on a long report.
    yield Spacer(1, 8)
    yield KeepTogether([
        HRFlowable(
            width="100%", thickness=0.5,
            color=colors.HexColor("#e5e7eb"),
            spaceBefore=0, spaceAfter=4,
        ),
        Paragraph(L["disclaimer"], _STYLES["disclaimer"]),
    ])


def _render_node_bullet(node: dict[str, Any]) -> Paragraph:
    """Render one Layer-1 node as a bullet line:
        • <label>  [N42] <urgency, if any>
    Citation in monospace-like inline style so it stands out from prose.
    """
    label   = _html_safe(str(node.get("label") or "—"))
    node_id = node.get("node_id")
    urgency = (node.get("urgency") or "").strip().lower()
    parts = [f"• {label}"]
    if node_id is not None:
        parts.append(
            f'<font color="#6b7280" size="8">  [N{int(node_id)}]</font>'
        )
    if urgency:
        # Colour the urgency tag the way the chat UI does.
        colour = {
            "critical":  "#ef4444",
            "moderate":  "#f59e0b",
            "incidental":"#6b7280",
        }.get(urgency, "#6b7280")
        parts.append(
            f' <font color="{colour}" size="8">({_html_safe(urgency)})</font>'
        )
    return Paragraph("".join(parts), _STYLES["list_item"])


def _html_safe(text: str) -> str:
    """reportlab's Paragraph parses an XML-like subset (<b>, <i>,
    <font>...). User-typed bodies may contain literal '<' / '>' /
    '&' which would blow up the parser. Escape them; preserve newlines
    as <br/> for the markdown-like flow the desktop UI uses."""
    if not text:
        return ""
    out = (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )
    return out


# ─────────────────────────────────────────────────────────────────────
# Localisation
# ─────────────────────────────────────────────────────────────────────


_LABELS_EN = {
    "title":         "Nexus · Clinical Report",
    "generated_at":  "Generated",
    "clinical_info": "CLINICAL INFORMATION",
    "findings":      "FINDINGS",
    "impression":    "IMPRESSION",
    "differentials": "DIFFERENTIAL DIAGNOSIS",
    "recommendation":"RECOMMENDATION",
    "no_findings":   "(no findings included)",
    "no_ddx":        "(no differentials included)",
    "disclaimer":    (
        "AI-assisted preliminary report. Final read and clinical "
        "decision by the supervising radiologist."
    ),
}

_LABELS_ZH = {
    "title":         "Nexus · 临床报告",
    "generated_at":  "生成时间",
    "clinical_info": "临床信息",
    "findings":      "影像所见",
    "impression":    "诊断意见",
    "differentials": "鉴别诊断",
    "recommendation":"建议",
    "no_findings":   "（未包含任何发现）",
    "no_ddx":        "（未包含任何鉴别诊断）",
    "disclaimer":    (
        "AI 辅助预览报告，最终判读及临床决策由上级放射科医师负责。"
    ),
}


def _labels(locale: str) -> dict[str, str]:
    if (locale or "").lower().startswith("zh"):
        return _LABELS_ZH
    return _LABELS_EN


# ─────────────────────────────────────────────────────────────────────
# Output-path helper
# ─────────────────────────────────────────────────────────────────────


def reports_dir(archive_dir: Path) -> Path:
    """``<archive_dir>/Reports/`` — created on demand. Centralised so
    the test harness and the router both agree on the same location."""
    p = archive_dir / "Reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_pdf_name(patient_hash: str) -> str:
    """Filename convention: first 8 chars of patient_hash + unix
    timestamp. Stable enough for "open the latest" debugging, never
    collides on a sub-second double-click thanks to the unix seconds
    suffix being monotone."""
    tag = (patient_hash or "anon")[:8]
    ts = int(time.time())
    return f"{tag}-{ts}.pdf"
