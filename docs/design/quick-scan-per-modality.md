# Quick Scan — Per-Modality Logic

Status: **Reference** (2026-06-14)
Code:   `packages/server/nexus_server/quick_scan.py` + `dicom.py`

Quick scan today supports CT, MRI, and PET-CT with three different
strategies driven by `_presets_for_body_part(body_part, modality)` and
the per-modality `DEFAULT_WINDOWS` table. This doc captures what each
modality does and why.

## Shared pipeline (all modalities)

```
upload .zip
  → dicom_ingester unpacks + indexes (study + series + per-slice z_position)
  → Tier-A auto-fire after ingest:
     1. Pick the primary series = max(slice_count) across series
     2. Decide presets via _presets_for_body_part(body_part, modality)
     3. For each preset: render dense 4×4 grids (16 slices per grid,
        cell_size=384, hard-cap 400 slices = 25 grids per preset)
     4. Phase 1 — Gemini 2.5 Flash triage per grid, parallel=4, with
        a window-aware system prompt (PRESET_PROMPT_HINTS) so the
        model is anchored to the right tissue class
     5. Aggregate verdicts → emit ASSISTANT_RESPONSE + NODE_ADDED
        events (one finding per non-clean grid)
```

Constants (`quick_scan.py:48-99`):

| Constant              | Value                | Why                                                          |
| --------------------- | -------------------- | ------------------------------------------------------------ |
| SLICES_PER_GRID       | 16 (4×4)             | 5 mm nodules render at ~4–5 px on a 1536² grid               |
| QUICK_SCAN_CELL_SIZE  | 384                  | Below Gemini's 3072 px cap; clear small-finding visibility   |
| PHASE1_CONCURRENCY    | 4                    | Parallel Gemini calls; avoids hitting per-minute rate limit  |
| SLICES_HARD_CAP       | 400                  | Cost ceiling: ~25 grids × 3 presets × $0.0001 ≈ $0.0075/scan |
| PHASE1_MODEL          | `gemini-2.5-flash`   | Vision-capable; v1 used 2.0-flash-exp which Google retired   |

## CT — three windows for chest, one for everything else

**Decision** (`quick_scan.py:249-266`):

```python
if modality.upper() != "CT":
    return DEFAULT_PRESETS
if body_part in CHEST_BODY_PARTS:
    return CHEST_PRESETS  # ('lung', 'mediastinum', 'bone')
return DEFAULT_PRESETS    # ('default',)
```

`CHEST_BODY_PARTS = {"CHEST", "THORAX", "LUNG", "LUNGS",
"CHEST_AND_ABDOMEN", "CARDIAC", "HEART", "CHESTABDOMEN",
"WHOLEBODY"}` — PET-CT whole-body studies often DICOM-tag as
WHOLEBODY, which is why that's in the chest set (the CT volume in
PET-CT covers the same anatomy as a chest+abdo CT).

**Chest CT → 3 passes** (`quick_scan.py:441-458`):
The same 25 grids are rendered three times with three different
HU windows. Each pass uses a different Gemini prompt hint:

| Preset       | WL/WW (HU)       | Catches                                                                          |
| ------------ | ---------------- | -------------------------------------------------------------------------------- |
| `lung`       | -600 / 1500      | Parenchymal: nodules, masses, GGO, consolidation, pneumothorax, PE               |
| `mediastinum`| 40 / 400         | Soft-tissue: masses, lymphadenopathy, vascular abnormalities, effusions, aortic dissection |
| `bone`       | 400 / 1800       | Cortical: fractures, lytic/blastic lesions, vertebral compression, rib pathology |

Why three windows: at any single HU window the OTHER tissue classes
saturate to pure black/white and are unreadable. A 5 mm lung nodule
on the bone window is invisible (lung parenchyma → black); a rib
fracture on the lung window is invisible (cortical bone → white).
Without per-tissue windowing we'd miss roughly half of clinically
relevant findings.

**De-duplication** (prompt-side, `quick_scan.py:693-697`):
The Phase 1 prompt explicitly tells Gemini: "if the abnormality is
one this window is NOT meant for, call it clean here and let the
matching-window pass catch it". This prevents a hot finding (e.g. a
big mass visible on all three windows) from being triple-counted.

**Non-chest CT → 1 pass** (head, abdomen, pelvis, extremities):
Single `default` preset → WL/WW = 40/400. Adequate for soft-tissue
focused reads; for non-chest cases we don't pay the 3x Gemini cost.

**Cost** (chest CT vs non-chest CT):
- Chest CT, 500 slices: scan_count=400, n_grids=25 × 3 presets = 75
  grids = 75 Gemini Flash calls × $0.0001 ≈ **$0.0075 per scan**
- Non-chest CT, same size: 25 grids × 1 preset = 25 calls ≈ **$0.0025**
- Latency: chest ~45 s wall (4-way parallel); non-chest ~15 s

## MRI — single percentile-autowindow pass

**Decision**: `_presets_for_body_part("CT" check fails) → DEFAULT_PRESETS`
= `('default',)`. Same as non-chest CT structurally.

**Window** (`dicom.py:76`): `("MR", "default") = (200, 400)` is a
WEAK fallback that almost never matches the actual pixel range a
specific scanner produces.

The fallback path: when `_resolve_window` returns `(None, None)`
OR the explicit window saturates the image, `_percentile_window`
runs over the raw pixel array (`dicom.py:535-551`): take the 2nd
and 98th percentile of all pixel values as WL/WW. This adapts to
whatever the scanner / sequence / TE/TR produced.

Why no per-sequence multi-pass:
- MRI sequences (T1, T2, FLAIR, DWI, ADC, post-contrast) ARE
  effectively different "windows" — but they live in separate
  DICOM SERIES, not as different windowings of the same volume.
- Today Quick Scan picks ONE series (the largest) and triages it.
  So a brain MRI study with T1 + T2 + FLAIR + DWI all uploaded
  gets Quick scan on whichever series has the most slices.

**Implication**: Quick scan on MRI today is intentionally shallow.
The medic gets a triage on "the biggest sequence" with a single
adaptive window. Catches gross findings (masses, hemorrhage, large
infarcts) reliably. Misses sequence-specific findings (DWI hot
spot for early infarct, FLAIR hyperintensity for MS plaques) when
those live in non-largest series.

**Future**: Phase B work item is to make MRI scan all series with
≥N slices, with sequence-aware prompts (T1 prompt vs T2 prompt vs
FLAIR prompt vs DWI prompt) — same multi-pass architecture as
chest CT but driven by series-type instead of window-window.

**Cost / latency**: 1 series × 25 grids × $0.0001 ≈ **$0.0025**, ~15 s.

## PET-CT — CT path + special PT-series percentile windowing

**Decision** (`quick_scan.py:400-404`):
```python
primary = max(study.series, key=lambda s: s.slice_count)
modality = (study.modality or "").upper() or "CT"
```

For a PET-CT, the CT volume dominates slice count (~500 vs PT's
~200 slices), so `primary` becomes the CT series and `modality`
becomes "CT". From here it's the **same path as a chest CT** — the
PET-CT CT volume is treated identically to a standalone chest CT.

`WHOLEBODY` body_part is in `CHEST_BODY_PARTS`, so PET-CT
whole-body gets the **3-preset (lung / mediastinum / bone) treatment**.

### What about the PT (PET) series itself?

Currently NOT scanned by Phase 1 — `primary` is the larger CT
series so the PT series never reaches Gemini. The reasoning
(`quick_scan.py:258-259` comment):

> PT (PET) studies use the default percentile-windowed path regardless —
> the CT-specific lung / bone presets don't make sense for SUV maps.

If a future task forces Quick Scan onto the PT series directly
(e.g. `primary = pt_series` override), the per-modality path is
already in place:

- `("PT", "default") = (None, None)` in DEFAULT_WINDOWS
  (`dicom.py:84`) triggers `_percentile_window` (2/98 percentile)
- SUV-like pixel ranges (typical 0–10 after RescaleSlope) get
  auto-stretched to 0–255 grayscale
- A single-preset (`default`) pass is run
- Prompt would need to be re-anchored ("scan for FDG-avid foci,
  not anatomical abnormalities") — that's the open work item

### Where PET-CT scanning is strong today

The CT volume of a PET-CT is **anatomical truth** — locating
masses, lymphadenopathy, structural pathology. That's what the
3-window chest CT pipeline already does well. The medic gets:

- Lung window pass: pulmonary nodules + GGO
- Mediastinum pass: enlarged nodes (PET-CT staging's primary use)
- Bone pass: lytic / blastic mets

### Where PET-CT scanning is weak today

Metabolic-only findings invisible on CT (e.g. a small FDG-hot lymph
node without anatomical enlargement) won't trigger. That's the
gap a PT-series triage pass would close.

**Cost / latency** (whole-body PET-CT): 3-pass on the CT, 400-slice
cap = 75 grids ≈ **$0.0075**, ~45 s. PT-series pass would add
~$0.0025 / +15 s.

## Summary table

| Modality                     | Body part         | Presets / passes              | Series scanned                  | Total grids | Cost / Latency       |
| ---------------------------- | ----------------- | ----------------------------- | ------------------------------- | ----------- | -------------------- |
| **CT chest / thorax / heart / whole-body** | CHEST etc.        | lung + mediastinum + bone (3) | primary (largest) only          | ~75         | ~$0.0075 / ~45 s     |
| **CT non-chest** (head, abd, pelvis, etc.) | other             | default (1)                   | primary only                    | ~25         | ~$0.0025 / ~15 s     |
| **MRI**                      | any               | default (1, percentile auto)  | primary only                    | ~25         | ~$0.0025 / ~15 s     |
| **PET-CT**                   | WHOLEBODY etc.    | lung + mediastinum + bone (3) | CT series only (PT skipped today) | ~75       | ~$0.0075 / ~45 s     |

## Known gaps / Phase B candidates

1. **MRI sequence-aware multi-pass** — one Gemini pass per
   T1/T2/FLAIR/DWI series with sequence-specific prompts. Quadruples
   cost for brain MR studies but catches early infarcts and MS
   plaques.

2. **PET PT-series triage** — single-preset percentile-windowed pass
   on the PT series with an FDG-avidity-aware prompt. Catches
   metabolically active but anatomically silent findings.

3. **Phase 2 (zoom-in)** — for any `unsure` grid in Phase 1, emit a
   higher-resolution zoom on the suspect slice range to a stronger
   model (e.g. Gemini 2.5 Pro). Not yet implemented.

4. **Sagittal / coronal reformat** — Quick scan only sees axial
   slices today. Spinal pathology is much more obvious on sagittal;
   aortic dissection more obvious on coronal. Adding reformat
   render paths would catch axially-subtle findings.

5. **Cross-window finding merge** — when the same finding shows
   weakly on two windows, today they're emitted as two finding
   nodes. A post-Phase-1 dedupe pass (cluster findings whose slice
   ranges overlap + verdict is the same) would consolidate.
