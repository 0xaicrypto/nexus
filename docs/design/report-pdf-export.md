# Report PDF Export — Fix Plan

Status: **Proposed** (2026-06-14)
Owner: TBD
Reported: medic feedback — "Export PDF doesn't work, I don't know
where the file went either."

## Diagnosis

`ReportMode.exportPdf` (`packages/desktop-v2/src/modes.tsx:1624-1632`)
currently:

```ts
document.body.classList.add('report-print-mode');
setTimeout(() => {
  window.print();
  document.body.classList.remove('report-print-mode');
}, 50);
```

### Why it fails

1. **WKWebView (macOS Tauri's renderer) does not expose a usable print
   dialog** to embedding apps. No "Save as PDF" destination like
   Chrome's print pipeline — that is a Chromium feature, not WebKit's.
   At best a tiny system print sheet appears; at worst nothing.
2. **No path feedback** — `exportPdf` produces no file path, no toast,
   no `lastReport` UI card. Contrast Settings · Data → Export now,
   which DOES surface `{path, bytes, createdAt}` and an "Open Archive
   folder" button.
3. **`<a download>` blob URLs are broken in WKWebView too** — so the
   sibling exports (FHIR DiagnosticReport JSON, DICOM SR stub) are
   probably also silent failures, despite never being reported as
   such.

## Recommended fix: server-side WeasyPrint endpoint

Mirrors the existing `export_router.py` pattern (bundle archive
writes to `~/Documents/Nexus Archive/`, returns `{bundle_path, bytes,
counts, created_at}`). Single render path, no client divergence.

### Why not alternatives

- **(a) Keep `window.print()`** — broken in WKWebView, can't fix
- **(b) jsPDF on the client** — produces fixed-coordinate ugly PDFs,
  needs Tauri `plugin-dialog` (not currently installed; would change
  `Cargo.toml` + `entitlements.plist`)
- **(c) WeasyPrint on the server** ✓ — real CSS engine for layout
  fidelity; reuses Archive directory; symmetric with existing exports

### Tradeoffs of (c)

- Adds `weasyprint` Python dep (~30 MB wheels)
- macOS system libraries: `pango`, `cairo` — need PyInstaller to
  bundle the dylibs into the .app
- ~300 ms render time per page (acceptable for clinical reports)
- Tailwind utility classes don't apply server-side → need a dedicated
  Jinja template `templates/report.html` with inline styles

## Action items (ordered)

1. **server / dependencies**:
   - `packages/server/pyproject.toml` add `weasyprint` (and verify
     `pango`/`cairo` system-lib path resolution on macOS).

2. **server / router**:
   - New `packages/server/nexus_server/report_pdf_router.py`:
     - `POST /api/v1/report/pdf`
     - body: `{patient_hash, draft, proj}`
     - render Jinja template `templates/report.html`
     - WeasyPrint → write to
       `$ARCHIVE_DIR/Reports/<patient_hash[:8]>-<ts>.pdf`
     - return `{path, bytes, created_at}`
   - Register in `main.py` next to `export_router`.

3. **server / tests**:
   - `tests/test_report_pdf.py`:
     - smoke: route accepts well-formed payload
     - rejects missing `patient_hash`
     - output is a valid PDF (magic bytes `%PDF-`)
     - path is under Archive dir
     - `bytes > 0`

4. **PyInstaller**:
   - Add `weasyprint` to `hiddenimports` in
     `packages/server/nexus-server.spec`
   - Verify Pango/Cairo dylibs end up in `.app/Contents/Frameworks/`
     — add to `binaries` list if needed
   - Test by `pnpm tauri:build` and importing weasyprint inside the
     bundled sidecar

5. **desktop-v2 / api-client**:
   - Add `api.exportReportPdf({patientHash, draft, proj})`
     returning `{path, bytes, createdAt}`

6. **desktop-v2 / ReportMode UI**:
   - Replace `exportPdf` body with `await api.exportReportPdf(...)`
   - `useState<LastReport | null>` holds the result
   - Show "Last report · {size} · {when}" card below the Export
     buttons, with a path line + "Open folder" button (sourcing the
     pattern from
     `components/full-screen-overlays.tsx:422-435`, reusing
     `openInOsShell(path)`)
   - Add success toast: `已导出 PDF · {size}`
   - i18n keys: `report.lastExport`, `report.openFolder`,
     `report.exportFailed`

7. **Cleanup**:
   - Delete `index.css:98-121` `@media print` block + every
     `.report-print-mode` toggle (dead code after switch)

8. **Sibling fixes (FHIR + DICOM SR)**:
   - Both currently use `downloadBlob` which is also broken in
     WKWebView. Either:
     - (a) Route through the same server endpoint with a `format`
       parameter (`pdf` | `fhir` | `dicom_sr`)
     - (b) Use Tauri `plugin-fs` to write the blob to a chosen path
       (still need `plugin-dialog` for the chooser)
   - Recommendation: (a) — keeps single export pipeline; route accepts
     `format`, the response is always `{path, bytes, created_at,
     format}` and the UI's lastReport card is format-agnostic.

## Estimated effort

~1.5 days, single PR. Includes:
- backend route + template + WeasyPrint plumbing
- 5 tests
- frontend client + lastReport card + i18n
- PyInstaller spec adjustment + build verification

## Risk

- WeasyPrint native deps under PyInstaller on macOS 14/15/26 can be
  fragile — first build will likely need 2–3 iterations to get the
  dylib bundling right. Mitigation: ship behind a fall-back so if the
  router 500s the UI shows "PDF export unavailable on this build, try
  rebuilding from latest .dmg" rather than silently failing again.
