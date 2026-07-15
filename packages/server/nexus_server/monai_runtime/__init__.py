"""MONAI lightweight runtime (M0.5 spike per ADR-002 Rev-6).

Mac-friendly subset of the MONAI ecosystem. See module docstring of
each submodule for details.

M0.5 in-progress — currently shipped:
  * ``bundle_loader`` — Bundle parser + Provenance adapter (DONE)

In-progress (next when M0.5 resumes):
  * ``inference_backend`` — abstract inference + Gemini Flash impls
  * ``coreml_inference`` — Mac CoreML wrapper
  * ``ohif_label_bridge`` — REST endpoints for OHIF medic-in-the-loop
  * ``bundles/quick_scan_4x4_grid/`` — first shipped Bundle
"""

from nexus_server.monai_runtime.bundle_loader import (
    ACCEPTABLE_LICENSES,
    BUNDLE_ROOT,
    BundleInferenceConfig,
    BundleLicenseError,
    BundleLoadError,
    BundleMeta,
    ProvenanceRefs,
    bundle_to_provenance_refs,
    list_bundles,
    load_bundle,
)
from nexus_server.monai_runtime.inference_backend import (
    BackendUnavailable,
    CoreMLBackend,
    GeminiFlash2DBackend,
    GeminiFlashQuickScanBackend,
    InferenceBackend,
    InferenceFailed,
    InferenceInput,
    InferenceResult,
    StubBackend,
    resolve_backend,
)

__all__ = [
    "BundleMeta",
    "BundleInferenceConfig",
    "BundleLoadError",
    "BundleLicenseError",
    "ProvenanceRefs",
    "load_bundle",
    "list_bundles",
    "bundle_to_provenance_refs",
    "BUNDLE_ROOT",
    "ACCEPTABLE_LICENSES",
    "InferenceBackend",
    "InferenceResult",
    "InferenceInput",
    "BackendUnavailable",
    "InferenceFailed",
    "GeminiFlash2DBackend",
    "GeminiFlashQuickScanBackend",
    "CoreMLBackend",
    "StubBackend",
    "resolve_backend",
]
