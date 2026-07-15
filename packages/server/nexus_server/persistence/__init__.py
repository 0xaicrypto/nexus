"""Five-tier persistence (ADR-002 Rev-7).

* ``export_bundle`` — Tier 4 sovereign export. Self-contained directory
  of open formats (JSON, JSONL, MD, FHIR-shaped JSON, SQL dump) the
  medic can read with any tool.
* ``snapshots``     — Tier 2 daily local archive (deferred to D1).
* ``cloud_sync``    — Tier 3 optional encrypted remote (deferred to D3).
"""

from nexus_server.persistence.export_bundle import (
    ExportBundleResult,
    create_export_bundle,
)
from nexus_server.persistence.snapshots import (
    SnapshotResult,
    apply_retention,
    start_snapshot_scheduler,
    take_snapshot,
)

__all__ = [
    "create_export_bundle",
    "ExportBundleResult",
    "SnapshotResult",
    "take_snapshot",
    "apply_retention",
    "start_snapshot_scheduler",
]
