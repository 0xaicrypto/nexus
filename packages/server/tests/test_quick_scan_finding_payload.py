"""
Regression test for #203 — Quick scan finding nodes silently lost.

Bug
===
``_run_quick_scan_after_ingest`` emitted ``EventKind.NODE_ADDED``
events with payload key ``"content"``. The EventSpec for NODE_ADDED
(see ``event_kinds.py:185``) declares ``required_fields=
("node_type", "content_json")`` — schema validation raised KeyError.
The outer try/except caught it, returned a "graph emit failed"
suffix in the upload summary, but the medic still saw the "10
flagged finding(s)" count on the Imaging card (computed from the
report metadata, not from the node-write success).

Downstream visible breakage:
  - Memory · L1 · Patient graph showed Studies (1) only — no
    "Active findings" group.
  - Patient mode's "Active findings" section read
    "No active findings yet" even though Quick scan reported 10.
  - Chat PATIENT CONTEXT had no findings → LLM said "specific
    findings not in the current context".

Fix
===
Payload key renamed to ``content_json`` to match the EventSpec.
Optional fields ``evidence_quote / confidence / extraction_model
/ extraction_prompt_id / source_kind / source_ref`` are now dropped
from the NODE_ADDED payload because they belong to a separate
PROVENANCE_RECORDED event — keeping them on NODE_ADDED only
inflates the event_log row's payload_json.

Test
====
Drives the actual emit code path against a real in-process Store
and asserts the row lands in clinical_graph_nodes with the right
node_type + label.
"""
from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def test_finding_payload_uses_content_json_key():
    """Source-level guard: the Quick scan emit must pass
    ``"content_json"`` (not ``"content"``) to match the EventSpec."""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "nexus_server" / "files.py"
    ).read_text()

    # Slice out _run_quick_scan_after_ingest specifically. The next
    # top-level definition is ``def retry_quick_scan_for_study`` — NOT
    # an async def — so anchor on either form.
    m = re.search(
        r"def _run_quick_scan_after_ingest\([\s\S]+?\n(?:async )?def \w",
        src,
    )
    assert m, "_run_quick_scan_after_ingest not found"
    body = m.group(0)

    # Find the NODE_ADDED emit block — anchored by EventKind.NODE_ADDED.
    emit_m = re.search(
        r"EventKind\.NODE_ADDED[\s\S]+?apply_fn=_h_node_added",
        body,
    )
    assert emit_m, "NODE_ADDED emit not found in helper"
    emit = emit_m.group(0)

    assert '"content_json"' in emit, (
        "Finding payload still uses key 'content' — schema "
        "validation will reject and findings never reach "
        "clinical_graph_nodes. Use 'content_json' to match the "
        "EventSpec required_fields."
    )
    # Catch the regression form explicitly too — if someone reverts.
    assert re.search(
        r'"content"\s*:\s*\{', emit,
    ) is None or '"content_json"' in emit, (
        "Both 'content' AND 'content_json' present? Pick one. "
        "EventSpec wants content_json."
    )


def test_finding_node_actually_lands_in_graph(tmp_path, monkeypatch):
    """End-to-end: emit a NODE_ADDED event with the same payload
    shape Quick scan uses, against a real Store backed by a tmp DB,
    and confirm the row appears in clinical_graph_nodes.

    Catches the kind of regression where someone keeps the right
    payload key but accidentally moves the emit outside the
    ``patient_hash`` truthiness gate."""
    from nexus_server.config import ServerConfig
    from nexus_server.migrations.runner import run_migrations
    from nexus_server.event_sourcing import (
        EventKind, Store, init_event_sourcing_schema,
    )
    from nexus_server.event_sourcing.handlers import _h_node_added

    db = tmp_path / "qs_finding.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setattr(ServerConfig, "DATABASE_URL", f"sqlite:///{db}")
    run_migrations()

    with sqlite3.connect(db) as conn:
        init_event_sourcing_schema(conn)
        store = Store(conn)
        # This payload shape MUST match what _run_quick_scan_after_ingest
        # emits today. If you change one, change both.
        store.emit_and_apply(
            kind=EventKind.NODE_ADDED,
            payload={
                "node_type": "finding",
                "content_json": {
                    "label":    "8mm RUL nodule",
                    "source":   "quick_scan",
                    "study_id": "study-xyz",
                    "urgency":  "moderate",
                    "status":   "unconfirmed",
                },
                "encounter_id": "quick_scan:study-xy",
            },
            apply_fn=_h_node_added,
            user_id="u1",
            patient_hash="p1",
        )
        conn.commit()

    # Read back via the projection-table query the memory router uses.
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT node_id, node_type, content_json "
            "FROM clinical_graph_nodes "
            "WHERE user_id = ? AND patient_hash = ? AND node_type = ?",
            ("u1", "p1", "finding"),
        ).fetchone()

    assert row is not None, (
        "Finding row didn't land in clinical_graph_nodes. Either the "
        "schema rejected the payload (key mismatch) OR _h_node_added "
        "no longer projects NODE_ADDED into this table."
    )
    node_id, ntype, cjson = row
    assert ntype == "finding"
    content = json.loads(cjson)
    assert content.get("label") == "8mm RUL nodule"
    assert content.get("source") == "quick_scan"
    assert content.get("urgency") == "moderate"


def test_finding_shows_up_in_patient_projection_query(tmp_path, monkeypatch):
    """The Memory mode + Patient mode UI hit
    ``GET /api/v1/memory/patient/{hash}/projection`` which goes
    through ``active_clinical("finding")``. That SQL has a LEFT JOIN
    against node_provenance with a filter on retracted_at. Quick scan
    findings don't emit a corresponding PROVENANCE_RECORDED event —
    the LEFT JOIN must still surface them (NULL retracted_at on the
    missing right-hand side is what makes the filter pass)."""
    from nexus_server.config import ServerConfig
    from nexus_server.migrations.runner import run_migrations
    from nexus_server.event_sourcing import (
        EventKind, Store, init_event_sourcing_schema,
    )
    from nexus_server.event_sourcing.handlers import _h_node_added

    db = tmp_path / "qs_proj.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setattr(ServerConfig, "DATABASE_URL", f"sqlite:///{db}")
    run_migrations()

    with sqlite3.connect(db) as conn:
        init_event_sourcing_schema(conn)
        store = Store(conn)
        for i, label in enumerate(("nodule", "GGO", "lymphadenopathy")):
            store.emit_and_apply(
                kind=EventKind.NODE_ADDED,
                payload={
                    "node_type":    "finding",
                    "content_json": {"label": label, "source": "quick_scan"},
                    "encounter_id": "quick_scan:s",
                },
                apply_fn=_h_node_added,
                user_id="u1",
                patient_hash="p1",
            )
        conn.commit()

    # Replicate the projection-endpoint query verbatim.
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT n.node_id, n.node_type, n.content_json "
            "FROM clinical_graph_nodes n "
            "LEFT JOIN node_provenance p "
            "  ON p.user_id = n.user_id "
            " AND p.patient_hash = n.patient_hash "
            " AND p.node_id = n.node_id "
            "WHERE n.user_id = ? AND n.patient_hash = ? "
            "  AND n.node_type = ? "
            "  AND (p.retracted_at IS NULL) "
            "ORDER BY n.updated_at DESC",
            ("u1", "p1", "finding"),
        ).fetchall()

    assert len(rows) == 3, (
        f"Patient projection's active_clinical('finding') query "
        f"returned {len(rows)} rows (expected 3). Either the "
        f"LEFT JOIN doesn't pass null-right-side rows OR the "
        f"NODE_ADDED payload didn't land. Memory tab + Patient tab "
        f"would show 'No active findings yet' even though Quick "
        f"scan reported some."
    )
    labels = {json.loads(r[2])["label"] for r in rows}
    assert labels == {"nodule", "GGO", "lymphadenopathy"}
