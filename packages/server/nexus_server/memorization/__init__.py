"""Memorization — ingesters that derive Layer 1 graph state from raw events.

Per ADR-002 Rev-8, every ingester uses the emit-event-then-apply pattern
through ``Store.emit_and_apply()``. No ingester writes to projection
tables directly. Per Rev-2, every clinical-fact node emitted carries
verbatim-quote-verified provenance.

Modules:
    chat_ingester  — chat → Layer 1 graph nodes (first event-sourcing
                     client; validates the pattern end-to-end in M0)
    dicom_ingester — DICOM → Layer 1 graph nodes (M1 + modality routing)
    lab_ingester   — labs → Layer 1 graph nodes (M5)
"""

from nexus_server.memorization.chat_ingester import (
    ChatIngester,
    ExtractionResult,
    QuoteVerificationError,
    StructuredEntity,
)
from nexus_server.memorization.dicom_ingester import (
    DicomIngester,
    DicomIngestError,
    KeySliceInput,
    RedactionRequired,
    RedactionResult,
    StudyInput,
    UnsupportedModality,
    make_test_study,
    route_modality,
    stub_redactor,
)

__all__ = [
    "ChatIngester",
    "QuoteVerificationError",
    "ExtractionResult",
    "StructuredEntity",
    "DicomIngester",
    "DicomIngestError",
    "RedactionRequired",
    "UnsupportedModality",
    "StudyInput",
    "KeySliceInput",
    "RedactionResult",
    "route_modality",
    "stub_redactor",
    "make_test_study",
]
