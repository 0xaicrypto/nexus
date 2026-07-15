"""#191 — Quick scan pipeline for DICOM studies.

User-facing flow (locked in #182 design):

  1. Medic uploads a PET-CT / CT zip → DICOM prerender finishes.
  2. Medic clicks 🔍 Quick scan button in chat or study card.
  3. Server enqueues this module's worker.
  4. Worker iterates the primary series in batches of 16 slices,
     renders 4×4 grids (already cached on disk from #140's prerender),
     sends each grid to Gemini Flash with a triage prompt.
  5. Phase 1 collects { slice_range, verdict, finding, urgency } tuples.
  6. Phase 2 (deferred — currently re-uses Phase 1 hints; future
     iteration will add Gemini Pro focused review on suspicious ranges).
  7. Phase 3 synthesises a structured report and emits it as an
     ``assistant_response`` event with ``metadata.kind="quick_scan_report"``
     so the desktop renders it as a special card in chat.

CRITICAL safety rules baked in:
  * Every Phase 1 prompt ends with "Be honest about uncertainty. This
    is a preliminary screen; final read happens by the radiologist."
  * Every report carries an immutable disclaimer string in metadata
    so the desktop's render never drops it.
  * If Gemini errors / no findings, we STILL emit a report (saying
    "no flagged findings") so the medic never sees an empty silence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from nexus_server import config
from nexus_server.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dicom", tags=["quick-scan"])


# ── Constants ───────────────────────────────────────────────────────

SLICES_PER_GRID    = 16          # 4×4 grid
PHASE1_CONCURRENCY = 4           # parallel Gemini Flash calls
# Google retired ``gemini-2.0-flash-exp`` on the v1beta API; calls now
# 404 NOT_FOUND with "models/gemini-2.0-flash-exp is not found". We
# pin to the current ga ``gemini-2.5-flash`` — same vision capabilities,
# same price tier, longer context, supported by ``google-genai`` SDK
# both old and new releases.
PHASE1_MODEL       = "gemini-2.5-flash"
SLICES_HARD_CAP    = 400         # don't scan more than this many slices
                                 # (cost guard for huge PET-CT, ~25 grids)

# Per-cell thumbnail size in the 4×4 grid. The previous default of 256
# (1024² total) had 5mm nodules rendering at ~3 px diameter — at the
# vision model's noise floor for medical anatomy. 384 (1536² total)
# is still well under Gemini's 3072 px per-image cap, and pushes a
# 5 mm finding to ~4–5 px — visible without yet bloating PNG size.
QUICK_SCAN_CELL_SIZE = 384

# Window presets we render for chest / lung studies. Each preset gets
# its own Phase 1 pass with a window-aware prompt — lung window catches
# parenchymal disease, soft-tissue (mediastinum) catches masses /
# lymphadenopathy / vascular abnormalities, bone catches fractures and
# lytic lesions. For non-chest body parts we fall back to a single
# "default" window so cost stays bounded.
CHEST_BODY_PARTS = frozenset({
    "CHEST", "THORAX", "LUNG", "LUNGS",
    "CHEST_AND_ABDOMEN", "CARDIAC", "HEART",
    "CHESTABDOMEN", "WHOLEBODY",  # PET-CT whole-body often labelled this way
})

# Each preset name MUST exist in dicom.DEFAULT_WINDOWS for the given
# modality, otherwise _resolve_window falls back to the modality default
# (single-window scan → defeats the point).
CHEST_PRESETS = ("lung", "mediastinum", "bone")
DEFAULT_PRESETS = ("default",)

# Human-readable description per preset for the Phase 1 prompt — pins
# Gemini's interpretive frame to the right tissue class for each grid.
PRESET_PROMPT_HINTS = {
    "lung":        "LUNG window (W:1500 / L:-600) — focus on parenchymal "
                   "abnormalities: nodules, masses, consolidation, ground-glass, "
                   "pneumothorax, pulmonary embolism.",
    "mediastinum": "MEDIASTINAL / SOFT-TISSUE window (W:400 / L:40) — focus "
                   "on masses, lymphadenopathy, vascular abnormalities, pleural "
                   "/ pericardial effusions, aortic dissection.",
    "bone":        "BONE window (W:1800 / L:400) — focus on cortical integrity, "
                   "fractures, lytic / blastic lesions, vertebral compression, "
                   "rib pathology.",
    "default":     "Default modality window — scan for any clear abnormality.",
}

DISCLAIMER = (
    "Preliminary AI screen — not a diagnosis. "
    "Radiologist review required for any clinical decision."
)


# ── Live progress (for the desktop's Imaging card streaming view) ──
#
# Module-level dict, keyed by study_id. The HTTP handler in
# ``files.get_prerender_progress`` reads from this on every 2-second
# poll the desktop fires, merges into the prerender response under
# ``quick_scan_progress``. UploadJobRow renders it under the "🔍 Quick
# scan: running…" line so the medic sees scan progress live instead
# of a static "running" placeholder.
#
# Thread safety: writes happen in the asyncio worker thread, reads on
# the FastAPI request handler's thread. Python dict updates of
# already-allocated keys are atomic w.r.t. the GIL — we don't bother
# with a lock. The worst case under contention is a momentarily-stale
# ``current`` field, which the next poll smooths over.
#
# Memory: a TTL-based prune fires from ``_set_quick_scan_progress`` so
# completed scans don't leak indefinitely. Bounded buffer for
# ``recent`` lines keeps each row small.
_quick_scan_progress: dict[str, dict] = {}
_QSP_TTL_SECONDS = 60 * 60   # drop completed scans after 1h
_QSP_RECENT_CAP  = 8         # keep the 8 most recent findings inline


def _set_quick_scan_progress(study_id: str, **fields) -> None:
    """Update (or create) the progress record for one in-flight scan.

    Callers pass a partial dict of fields to merge. Special key
    ``__push_recent__`` (single dict) appends to the bounded
    ``recent`` list — used after each Gemini Flash return so the UI
    can show "slices 32–47 [lung]: 8mm RUL nodule" as it streams.
    """
    cur = _quick_scan_progress.get(study_id)
    if cur is None:
        cur = {
            "stage":          "rendering",
            "started_at":     time.time(),
            "total_grids":    0,
            "rendered_grids": 0,
            "triaged_grids":  0,
            "errors":         0,
            "presets":        [],
            "current_preset": "",
            "recent":         [],
        }
        _quick_scan_progress[study_id] = cur

    push_recent = fields.pop("__push_recent__", None)
    if push_recent is not None:
        recent: list = cur.setdefault("recent", [])
        recent.append(push_recent)
        if len(recent) > _QSP_RECENT_CAP:
            del recent[: len(recent) - _QSP_RECENT_CAP]

    cur.update(fields)
    cur["elapsed_s"] = round(time.time() - cur.get("started_at", time.time()), 1)

    # Cheap GC: when a study reaches a terminal stage, schedule it for
    # later pruning. We don't need a real scheduler — just prune any
    # stale entries we encounter here.
    if cur.get("stage") in ("complete", "error"):
        cutoff = time.time() - _QSP_TTL_SECONDS
        stale = [
            sid for sid, e in _quick_scan_progress.items()
            if e.get("stage") in ("complete", "error")
            and e.get("started_at", 0) < cutoff
        ]
        for sid in stale:
            _quick_scan_progress.pop(sid, None)


def _clear_quick_scan_progress(study_id: str) -> None:
    """Drop any in-flight progress for ``study_id``. Called at the top
    of ``_run_quick_scan_async`` so a manual Retry starts with a clean
    slate (instead of inheriting the failed previous run's counters)."""
    _quick_scan_progress.pop(study_id, None)


def get_quick_scan_progress(study_id: str) -> Optional[dict]:
    """Read-only view of in-flight progress for the HTTP handler.

    Returns ``None`` when no scan is (or has been) running for this
    study — frontends should treat that as "no live data, fall back
    to the persisted uploads.quick_scan_status badge".
    """
    return _quick_scan_progress.get(study_id)


def _live_gemini_api_key() -> str:
    """Resolve GEMINI_API_KEY freshly at each Quick scan call.

    Why we don't use ``config.GEMINI_API_KEY``: ServerConfig captures
    env vars at module import time, so any key the medic types into
    Settings · LLM → Save (which writes ``$RUNE_HOME/.env``) is
    invisible to the running process until the sidecar restarts.
    For Quick scan in particular — which is the medic's first taste
    of AI in the app — having to "restart sidecar" after fixing a
    dead key is a heavy UX failure.

    Resolution order (first non-empty wins):
      1. ``$RUNE_HOME/.env`` GEMINI_API_KEY line   ← Settings · LLM writes here
      2. process ``os.environ["GEMINI_API_KEY"]``  ← Tauri sidecar bootstrap
      3. ``config.GEMINI_API_KEY``                 ← cached fallback

    All three are best-effort; missing values silently fall through
    so unit tests with no env still hit (3). The single I/O is a
    cheap ~5 KB read from a known-warm file each call — negligible
    next to the multi-second Gemini round-trip.
    """
    rh = os.environ.get("RUNE_HOME", "")
    if rh:
        try:
            from pathlib import Path as _Path
            env_path = _Path(rh) / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    if key.strip() == "GEMINI_API_KEY":
                        val = val.strip()
                        # Strip a matching pair of surrounding quotes.
                        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                            val = val[1:-1]
                        if val:
                            return val
        except (OSError, UnicodeDecodeError) as e:
            # Fall through — the cached config is still valid.
            logger.debug("reading env file for GEMINI_API_KEY failed: %s", e)

    env_val = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if env_val:
        return env_val

    # Last resort: the cached value on ServerConfig (captured at module
    # import time from os.environ). NOTE: we explicitly go through the
    # class — ``config.GEMINI_API_KEY`` (module attribute) does NOT
    # exist; the constant lives on the ``ServerConfig`` class object.
    cached = getattr(config.ServerConfig, "GEMINI_API_KEY", None)
    return (cached or "").strip()


def _presets_for_body_part(body_part: str, modality: str) -> tuple[str, ...]:
    """Pick which window presets to scan based on body part.

    CT chest → 3 presets (lung / mediastinum / bone), tripling Gemini
    Flash calls per study but catching tissue-class-specific findings
    that any single window would mask.

    Everything else → single default window.

    PT (PET) studies use the default percentile-windowed path regardless —
    the CT-specific lung / bone presets don't make sense for SUV maps.
    """
    if (modality or "").upper() != "CT":
        return DEFAULT_PRESETS
    bp = (body_part or "").upper().replace(" ", "").replace("-", "_")
    if bp in CHEST_BODY_PARTS:
        return CHEST_PRESETS
    return DEFAULT_PRESETS


# ── Data shapes ─────────────────────────────────────────────────────


@dataclass
class GridFinding:
    """One Gemini Flash verdict on one 4×4 grid."""
    slice_start: int
    slice_end:   int
    verdict:     str           # clean / suspicious / unsure / error
    finding:     str = ""      # one-sentence hint, '' when clean
    urgency:     str = ""      # critical / moderate / incidental / ''
    error:       str = ""      # populated only on API error
    # Window preset that produced this grid (lung / mediastinum / bone
    # / default). Lets the report group findings by tissue class so the
    # medic can tell "rib fracture at slice 220" (bone window) from
    # "pneumothorax at slice 80" (lung window).
    window:      str = "default"


@dataclass
class QuickScanReport:
    """The synthesised report Phase 3 emits as an
    assistant_response event."""
    study_id:       str
    patient_hash:   str
    modality:       str
    body_part:      str
    total_slices:   int
    scanned_slices: int
    grids_scanned:  int
    elapsed_s:      float
    findings:       list[dict] = field(default_factory=list)
    summary_counts: dict       = field(default_factory=dict)
    model_chain:    list[str]  = field(default_factory=list)


# ── Public entry points ─────────────────────────────────────────────


def trigger_quick_scan(
    *, user_id: str, study_id: str, background_tasks: BackgroundTasks,
) -> dict:
    """Kick off a Quick scan for the given study. Returns immediately
    after enqueueing; the worker runs out-of-band. Result lands in
    twin.event_log as an ``assistant_response`` with
    metadata.kind="quick_scan_report"."""
    background_tasks.add_task(_run_quick_scan_sync, user_id, study_id)
    return {
        "status":      "enqueued",
        "study_id":    study_id,
        "disclaimer":  DISCLAIMER,
    }


@router.post("/studies/{study_id}/quick-scan")
async def post_quick_scan(
    study_id: str,
    background_tasks: BackgroundTasks,
    current_user: str = Depends(get_current_user),
) -> dict:
    """Doctor clicked 🔍 Quick scan. Kick the background worker."""
    # Sanity-check the study exists + belongs to this user before
    # enqueueing; cheaper than letting the worker discover at run time.
    try:
        from nexus_server.dicom import load_study
        study = load_study(current_user, study_id)
        if study is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"study {study_id} not found",
            )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("quick_scan study lookup failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"study lookup failed: {e}",
        )
    # Delegate to the full post-ingest pipeline (worker + uploads-row
    # status writeback + finding-node emit) so a manual Retry behaves
    # identically to the Tier-A auto-fire. The bare ``trigger_quick_scan``
    # worker only runs Gemini + writes to twin_event_log; without the
    # uploads-row update the Imaging card's "🔍 Quick scan failed:…"
    # red line would never disappear and the medic would think the
    # retry didn't fire.
    from nexus_server.files import retry_quick_scan_for_study
    background_tasks.add_task(
        retry_quick_scan_for_study, current_user, study_id,
    )
    return {
        "status":     "enqueued",
        "study_id":   study_id,
        "disclaimer": DISCLAIMER,
    }


# ── Worker body ─────────────────────────────────────────────────────


def _run_quick_scan_sync(user_id: str, study_id: str) -> None:
    """BackgroundTasks calls this synchronously on its own thread.
    We drive an asyncio loop ourselves for the parallel Gemini calls.
    """
    try:
        asyncio.run(_run_quick_scan_async(user_id, study_id))
    except Exception as e:  # noqa: BLE001
        logger.exception("quick_scan worker crashed: %s", e)
        _emit_failure_report(user_id, study_id, f"{type(e).__name__}: {e}")


async def _run_quick_scan_async(user_id: str, study_id: str) -> None:
    """The actual three-phase scan."""
    t0 = time.monotonic()
    logger.info("quick_scan starting — user=%s study=%s",
                user_id, study_id[:8])

    # Reset any prior progress (e.g. left over from a failed previous
    # run) so the desktop's polling UI starts from zeroed counters.
    _clear_quick_scan_progress(study_id)
    _set_quick_scan_progress(study_id, stage="rendering")

    # ── Load study + grids ──────────────────────────────────────────
    from nexus_server.dicom import load_study
    study = load_study(user_id, study_id)
    if study is None:
        _set_quick_scan_progress(study_id, stage="error",
                                 last_error="study not found")
        _emit_failure_report(user_id, study_id, "study not found")
        return

    # Pick the primary (largest) series. For PET-CT this gives us the
    # CT volume which is what we want to triage anatomically.
    primary = max(study.series, key=lambda s: s.slice_count)
    modality  = (study.modality or "").upper() or "CT"
    body_part = (primary.body_part or "").upper() or "UNKNOWN"
    total     = primary.slice_count

    if total <= 0:
        _set_quick_scan_progress(study_id, stage="error",
                                 last_error="primary series has no slices")
        _emit_failure_report(user_id, study_id,
                             "primary series has no slices")
        return

    # Cap scan size to keep cost predictable.
    scan_count = min(total, SLICES_HARD_CAP)

    # Pick which CT window presets to render. Chest / lung / whole-body
    # studies get triple-pass (lung / mediastinum / bone) so findings
    # specific to one tissue class don't get masked by the wrong window.
    # Non-chest CT (head, abdomen, pelvis…) stays single-pass.
    presets = _presets_for_body_part(body_part, modality)
    n_grids_per_preset = max(1, (scan_count + SLICES_PER_GRID - 1) // SLICES_PER_GRID)
    expected_total = n_grids_per_preset * len(presets)
    logger.info(
        "quick_scan windowing — modality=%s body_part=%s presets=%s "
        "expected_grids=%d",
        modality, body_part, presets, expected_total,
    )

    _set_quick_scan_progress(
        study_id,
        modality=modality,
        body_part=body_part,
        presets=list(presets),
        total_grids=expected_total,
        scan_count=scan_count,
        total_slices=total,
    )

    # ── Render dense per-range × per-window grids ──────────────────
    # Each preset gets its own batched scan. For a 500-slice chest CT
    # under default config: scan_count=400, SLICES_PER_GRID=16 →
    # 25 grids × 3 presets = 75 PNGs → 75 Gemini Flash calls.
    # At ~$0.0001/call that's ~$0.0075 per scan — well under the
    # design cost budget.
    grids: list[tuple[int, int, str, bytes]] = []
    for preset in presets:
        _set_quick_scan_progress(
            study_id, stage="rendering", current_preset=preset,
        )
        preset_grids = await _render_batched_grids(
            user_id, study_id, primary,
            scan_count, SLICES_PER_GRID,
            preset=preset,
            progress_study_id=study_id,
        )
        for (s, e, png) in preset_grids:
            grids.append((s, e, preset, png))

    if not grids:
        _set_quick_scan_progress(
            study_id, stage="error",
            last_error="could not render grids for scan",
        )
        _emit_failure_report(user_id, study_id,
                             "could not render grids for scan")
        return

    # ── Phase 1: Gemini Flash triage ────────────────────────────────
    _set_quick_scan_progress(study_id, stage="triaging",
                             current_preset="",
                             triaged_grids=0)
    findings = await _phase1_triage(
        grids, modality=modality, body_part=body_part,
        progress_study_id=study_id,
    )

    # ── Phase 3: Synthesise + emit ──────────────────────────────────
    report = QuickScanReport(
        study_id       = study_id,
        patient_hash   = study.patient_hash or "",
        modality       = modality,
        body_part      = body_part,
        total_slices   = total,
        scanned_slices = scan_count,
        grids_scanned  = len(grids),
        elapsed_s      = round(time.monotonic() - t0, 1),
        findings       = [_finding_to_dict(f) for f in findings
                          if f.verdict in ("suspicious", "unsure")],
        summary_counts = _summarise_counts(findings),
        model_chain    = [PHASE1_MODEL],
    )
    await _emit_report(user_id, report)

    # Final stage marker — UI's polling sees this and stops the
    # "running…" spinner. The uploads.quick_scan_status fields fire
    # right after (set by the caller's writeback in files.py).
    _set_quick_scan_progress(
        study_id, stage="complete",
        triaged_grids=len(findings),
        summary_counts=report.summary_counts,
    )


# ── Grid rendering ──────────────────────────────────────────────────


async def _render_batched_grids(
    user_id: str, study_id: str, series, scan_count: int,
    per_grid: int,
    *,
    preset: str = "default",
    progress_study_id: Optional[str] = None,
) -> list[tuple[int, int, bytes]]:
    """Render dense per-range 4×4 grids of one window preset.

    For a 500-slice series with scan_count=400 and per_grid=16 this
    produces 25 grids:

        grid 0  = slices   0–15  (~1.6 cm)
        grid 1  = slices  16–31
        ...
        grid 24 = slices 384–399

    Each grid is uniformly sampled WITHIN its range (so neighbouring
    slices land in adjacent cells, useful for tracking findings across
    cells). The ``preset`` controls windowing: e.g. "lung", "mediastinum",
    "bone" for chest CT.

    Bug history (2026-06-14): an earlier version tried to pass
    ``slice_start=s, slice_end=e`` to a ``render_grid_png`` signature
    that didn't accept them. Every call raised TypeError → the
    ``except TypeError`` fallback rendered ONE whole-series grid and
    broke out of the loop. Net effect: a 500-slice CT was triaged
    from a single 16-thumbnail PNG (one thumbnail = ~31 slices of
    anatomy lumped together). ``dicom.render_grid_png`` now accepts
    the range params, so this loop produces the dense per-range grids
    the design always intended.
    """
    try:
        from nexus_server.dicom import render_grid_png
    except ImportError:
        logger.warning("quick_scan: render_grid_png unavailable")
        return []

    out: list[tuple[int, int, bytes]] = []
    n_grids = max(1, (scan_count + per_grid - 1) // per_grid)
    loop = asyncio.get_event_loop()

    for g in range(n_grids):
        start = g * per_grid
        end   = min(scan_count, start + per_grid) - 1
        if start > end:
            break
        try:
            # render_grid_png is sync + CPU heavy → run in thread pool.
            # Capture s/e/preset in default args so the lambda closure
            # doesn't smear over loop iterations.
            png = await loop.run_in_executor(
                None,
                lambda s=start, e=end, p=preset: render_grid_png(
                    series, rows=4, cols=4,
                    cell_size=QUICK_SCAN_CELL_SIZE,
                    slice_start=s, slice_end=e,
                    preset=p,
                ),
            )
            if png:
                out.append((start, end, png))
                if progress_study_id:
                    # Bump rendered_grids so the UI can show
                    # "Rendering 23/75 grids · lung window".
                    cur = _quick_scan_progress.get(progress_study_id) or {}
                    _set_quick_scan_progress(
                        progress_study_id,
                        rendered_grids=int(cur.get("rendered_grids", 0)) + 1,
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "quick_scan grid %d render failed (preset=%s): %s",
                g, preset, e,
            )

    return out


# ── Phase 1 triage ──────────────────────────────────────────────────


async def _phase1_triage(
    grids: list[tuple[int, int, str, bytes]],
    *, modality: str, body_part: str,
    progress_study_id: Optional[str] = None,
) -> list[GridFinding]:
    """Send each (slice-range × window) grid to Gemini Flash in
    parallel (capped) and parse the JSON verdict. Errors become
    ``GridFinding(verdict='error')`` so the report can still render.

    Grids carry their window preset as the third element so the prompt
    can tell Gemini what tissue class it's looking at. A "rib fracture
    in the bone window grid" and "ground-glass opacity in the lung
    window grid" are very different findings; without the window
    label Gemini conflates them.

    When ``progress_study_id`` is set, each Gemini return bumps the
    ``triaged_grids`` counter and (for non-clean verdicts) appends a
    summary to the recent-findings buffer so the desktop's Imaging
    card can render it live.
    """
    sem = asyncio.Semaphore(PHASE1_CONCURRENCY)

    async def scan_one(
        start: int, end: int, window: str, png: bytes,
    ) -> GridFinding:
        async with sem:
            result = await _gemini_triage_grid(
                png, slice_start=start, slice_end=end,
                modality=modality, body_part=body_part,
                window=window,
            )
        # Outside the semaphore — bookkeeping doesn't need to be
        # serialised with the Gemini RPC.
        if progress_study_id:
            cur = _quick_scan_progress.get(progress_study_id) or {}
            fields: dict = {
                "triaged_grids": int(cur.get("triaged_grids", 0)) + 1,
                "current_preset": window,
            }
            if result.verdict == "error":
                fields["errors"] = int(cur.get("errors", 0)) + 1
            # Only stream non-clean entries — clean is the majority and
            # would drown the recent list. Errors land too so the medic
            # sees "API key invalid · slices 0-15 [lung]" as soon as the
            # first call returns the bad-key response.
            if result.verdict != "clean":
                fields["__push_recent__"] = {
                    "slice_start": result.slice_start,
                    "slice_end":   result.slice_end,
                    "window":      result.window,
                    "verdict":     result.verdict,
                    "finding":     result.finding,
                    "urgency":     result.urgency,
                    "error":       result.error,
                }
            _set_quick_scan_progress(progress_study_id, **fields)
        return result

    tasks = [scan_one(s, e, w, p) for (s, e, w, p) in grids]
    return await asyncio.gather(*tasks)


async def _gemini_triage_grid(
    png: bytes, *, slice_start: int, slice_end: int,
    modality: str, body_part: str, window: str,
) -> GridFinding:
    """One Gemini Flash call against one (slice-range × window) grid.
    Parses STRICT JSON or returns a GridFinding(verdict='error').

    The prompt is window-aware: each preset (lung / mediastinum / bone)
    pins Gemini's interpretive frame to the right tissue class for
    that rendering. Without this the model would try to grade a lung
    nodule on a bone-window grid (where the lung parenchyma is
    saturated black) and get nothing.
    """
    # Re-read the API key from disk each call so the medic doesn't
    # have to Restart sidecar after updating it in Settings · LLM.
    # See ``_live_gemini_api_key`` for the resolution order.
    api_key = _live_gemini_api_key()
    if not api_key:
        return GridFinding(
            slice_start=slice_start, slice_end=slice_end,
            verdict="error",
            error="GEMINI_API_KEY not configured — set it in Settings · LLM",
            window=window,
        )

    window_hint = PRESET_PROMPT_HINTS.get(window) or \
        PRESET_PROMPT_HINTS["default"]

    prompt = f"""You are a board-certified radiologist screening a {modality} of {body_part}. The attached image is a 4×4 grid of axial slices labeled top-left → bottom-right as slices {slice_start} through {slice_end}.

Rendering window: {window_hint}

Triage rules:
  - "clean": nothing notable for this window's tissue class.
  - "suspicious": clear abnormality visible in this window's tissue class
                  (mass, hemorrhage, dissection, fracture, consolidation, etc.).
  - "unsure": something might be off but not confident.
  - Pick the urgency: "critical" (needs urgent follow-up TODAY),
                      "moderate" (routine follow-up),
                      "incidental" (note but not urgent).

If the abnormality is one this window is NOT meant for (e.g. a lung
nodule visible only on the LUNG window — don't flag it on the BONE
window grid), call it "clean" here and let the matching-window pass
catch it. This prevents the same finding from being triple-counted
across window passes.

Return STRICT JSON ONLY — no markdown fence, no prose:
{{
  "verdict": "clean" | "suspicious" | "unsure",
  "finding": "<one sentence, anatomy + approximate slice number, OR empty string if clean>",
  "urgency": "critical" | "moderate" | "incidental" | ""
}}

Be honest about uncertainty. This is a preliminary screen; final read happens by the radiologist."""

    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        # google-genai supports inline image bytes via types.Part.
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model=PHASE1_MODEL,
                contents=[
                    types.Part.from_bytes(data=png, mime_type="image/png"),
                    prompt,
                ],
            ),
        )
        text = (getattr(resp, "text", "") or "").strip()
    except Exception as e:  # noqa: BLE001
        return GridFinding(
            slice_start=slice_start, slice_end=slice_end,
            verdict="error", error=f"{type(e).__name__}: {e}",
            window=window,
        )

    # Parse the JSON. LLMs often wrap with ```json fences; strip them.
    parsed = _parse_loose_json(text)
    if not parsed:
        return GridFinding(
            slice_start=slice_start, slice_end=slice_end,
            verdict="error", window=window,
            error=f"non-JSON response: {text[:120]}",
        )
    verdict = (parsed.get("verdict") or "").lower().strip()
    if verdict not in ("clean", "suspicious", "unsure"):
        verdict = "unsure"
    return GridFinding(
        slice_start = slice_start,
        slice_end   = slice_end,
        verdict     = verdict,
        finding     = str(parsed.get("finding") or "").strip(),
        urgency     = str(parsed.get("urgency") or "").lower().strip(),
        window      = window,
    )


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_loose_json(text: str) -> Optional[dict]:
    """Best-effort JSON extraction from LLM text. Handles raw JSON,
    JSON wrapped in ```json fences, JSON embedded in prose."""
    text = text.strip()
    # Try direct parse first.
    try:
        return json.loads(text)
    except Exception as e:
        logger.debug("direct JSON parse failed: %s", e)
    # Fall back to first {...} match.
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# ── Emit report ─────────────────────────────────────────────────────


def _finding_to_dict(f: GridFinding) -> dict:
    return {
        "slice_start": f.slice_start,
        "slice_end":   f.slice_end,
        "verdict":     f.verdict,
        "finding":     f.finding,
        "urgency":     f.urgency,
        # New in #196 — propagates the rendering window so the Imaging
        # card / chat report can group findings by tissue class (lung
        # vs mediastinum vs bone) and the medic can spot "this lesion
        # only shows up on the bone window" patterns.
        "window":      f.window,
    }


def _summarise_counts(findings: list[GridFinding]) -> dict:
    counts = {
        "critical":   0,
        "moderate":   0,
        "incidental": 0,
        "clean":      0,
        "unsure":     0,
        "error":      0,
    }
    for f in findings:
        if f.verdict == "clean":
            counts["clean"] += 1
        elif f.verdict == "error":
            counts["error"] += 1
        elif f.urgency in ("critical", "moderate", "incidental"):
            counts[f.urgency] += 1
        else:
            counts["unsure"] += 1
    return counts


async def _emit_report(user_id: str, report: QuickScanReport) -> None:
    """Write the synthesised report as an assistant_response event
    in the user's twin event log. The desktop's chat refresh picks
    this up on its next poll and renders it as a special card."""
    body = _format_report_markdown(report)
    try:
        from nexus_server.twin_manager import get_twin
        twin = await get_twin(user_id)
        twin.event_log.append(
            "assistant_response", body,
            metadata={
                "kind":           "quick_scan_report",
                "study_id":       report.study_id,
                "patient_hash":   report.patient_hash,
                "modality":       report.modality,
                "body_part":      report.body_part,
                "total_slices":   report.total_slices,
                "scanned_slices": report.scanned_slices,
                "elapsed_s":      report.elapsed_s,
                "findings":       report.findings,
                "summary_counts": report.summary_counts,
                "model_chain":    report.model_chain,
                "disclaimer":     DISCLAIMER,
            },
        )
        logger.info(
            "quick_scan ✓ user=%s study=%s findings=%d elapsed=%ss",
            user_id, report.study_id[:8],
            len(report.findings), report.elapsed_s,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("quick_scan emit failed: %s", e)


def _emit_failure_report(user_id: str, study_id: str, err: str) -> None:
    """Synchronously emit a failure event so the medic isn't left
    waiting for a result that will never arrive."""
    import asyncio as _asyncio

    async def _emit():
        try:
            from nexus_server.twin_manager import get_twin
            twin = await get_twin(user_id)
            twin.event_log.append(
                "assistant_response",
                f"🔍 Quick scan could not run on this study.\n\n"
                f"Reason: {err}\n\n"
                f"{DISCLAIMER}",
                metadata={
                    "kind":       "quick_scan_report",
                    "study_id":   study_id,
                    "error":      err,
                    "disclaimer": DISCLAIMER,
                },
            )
        except Exception as e:
            logger.warning("emitting quick-scan failure report failed: %s", e)

    try:
        _asyncio.run(_emit())
    except Exception as e:
        logger.warning("emitting quick-scan failure report failed: %s", e)


def _format_report_markdown(report: QuickScanReport) -> str:
    """Render the report as markdown for the chat bubble fallback.
    The desktop's quick_scan_report card will render this in a richer
    layout, but plain-text fallback keeps it useful even in raw
    text mode."""
    counts = report.summary_counts
    lines = [
        f"🔍 **Quick scan complete** · {report.elapsed_s}s · "
        f"{report.modality} {report.body_part}",
        "",
    ]
    if report.findings:
        crit = counts.get("critical", 0)
        mod  = counts.get("moderate", 0)
        inc  = counts.get("incidental", 0)
        bar = []
        if crit: bar.append(f"🔴 {crit} critical")
        if mod:  bar.append(f"🟡 {mod} moderate")
        if inc:  bar.append(f"🟢 {inc} incidental")
        if bar:
            lines.append("  ·  ".join(bar))
            lines.append("")
        # Sort findings by urgency (critical → moderate → incidental)
        urgency_rank = {"critical": 0, "moderate": 1, "incidental": 2,
                        "": 3}
        sorted_findings = sorted(
            report.findings,
            key=lambda f: (urgency_rank.get(f.get("urgency", ""), 3),
                           f.get("slice_start", 0)),
        )
        for f in sorted_findings:
            icon = {"critical": "🔴", "moderate": "🟡",
                    "incidental": "🟢"}.get(f.get("urgency", ""), "•")
            txt = f.get("finding", "").strip() or "(no detail)"
            lines.append(
                f"{icon} **slices {f['slice_start']}–{f['slice_end']}** — "
                f"{txt}"
            )
    else:
        lines.append(
            "✓ No flagged findings across the scanned slices."
        )
    lines.append("")
    lines.append(
        f"_Scanned {report.scanned_slices} of {report.total_slices} slices · "
        f"{report.grids_scanned} grids · "
        f"model: {', '.join(report.model_chain)}_"
    )
    lines.append("")
    lines.append(f"⚠ {DISCLAIMER}")
    return "\n".join(lines)
