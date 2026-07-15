"""Layer 2 — Practitioner Memory (ADR-002 Rev-5).

Three pieces:

* ``extractor`` — per-encounter LLM call emitting candidate observations
* ``distiller`` — nightly aggregator promoting observations to facts
                  when distinct_patient_count crosses the threshold
* ``composer``  — at every agent turn, renders active facts into a
                  system-prompt enrichment block (≤800 tokens)

Privacy invariants (Rev-5 + R15) enforced by Store at write time:
patient_hash hex strings + ISO dates blocked from ``practitioner_facts``.

M1.6 status: extractor is stub (deterministic; real LLM call later);
distiller + composer are full implementations.
"""

from nexus_server.practitioner.composer import (
    PRACTITIONER_PROMPT_BUDGET_TOKENS,
    build_prompt_enrichment,
)
from nexus_server.practitioner.distiller import (
    N_THRESHOLDS,
    DistillerResult,
    distill,
)
from nexus_server.practitioner.extractor import (
    Candidate,
    extract_from_encounter,
    stub_practitioner_extractor,
)
from nexus_server.practitioner.heuristic_extractor import (
    extract_from_user_text,
    heuristic_practitioner_extractor,
)

__all__ = [
    "Candidate",
    "extract_from_encounter",
    "stub_practitioner_extractor",
    "DistillerResult",
    "distill",
    "N_THRESHOLDS",
    "build_prompt_enrichment",
    "PRACTITIONER_PROMPT_BUDGET_TOKENS",
]
