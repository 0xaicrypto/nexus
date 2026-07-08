"""ChatIngester end-to-end test.

Verifies the first real event-sourcing client:
- Emit-event-then-apply chain produces correct projection state.
- Verbatim-quote verification rejects hallucinated quotes.
- A drop_projections + replay roundtrip rebuilds the same state.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from nexus_server.event_sourcing import EventKind, Store, init_event_sourcing_schema
from nexus_server.event_sourcing.handlers import (
    _h_assistant_response, _h_user_message,
)
from nexus_server.event_sourcing.replay import full_rebuild
from nexus_server.event_sourcing.schema import PROJECTION_TABLES
from nexus_server.memorization import (
    ChatIngester,
    StructuredEntity,
)
from nexus_server.memorization.chat_ingester import make_stub_extractor


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    init_event_sourcing_schema(c)
    return c


@pytest.fixture
def store(conn: sqlite3.Connection) -> Store:
    return Store(conn)


def _seed_chat_encounter(
    store: Store,
    user_id: str,
    patient_hash: str,
    session_id: str,
    user_text: str,
    assistant_text: str,
) -> int:
    """Helper: drop a chat turn into event_log so the ingester has source.
    Returns the assistant_response event_idx."""
    store.emit_and_apply(
        kind=EventKind.USER_MESSAGE,
        payload={"text": user_text, "session_id": session_id},
        apply_fn=_h_user_message,
        user_id=user_id, patient_hash=patient_hash,
    )
    return store.emit_and_apply(
        kind=EventKind.ASSISTANT_RESPONSE,
        payload={
            "text": assistant_text, "session_id": session_id,
            "model": "claude-haiku-4-5",
            "prompt_id": "main_chat", "prompt_version": "1.0",
        },
        apply_fn=_h_assistant_response,
        user_id=user_id, patient_hash=patient_hash,
    )


# ─────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────

class TestIngestEncounter:
    def test_emits_full_chain_and_writes_projections(self, store, conn):
        user_id = "dr_chen"
        patient_hash = "7a3f_test"
        session_id = "sess-001"

        # Seed chat — quote must exist VERBATIM in this text:
        assistant_text = (
            "The left renal mass measures 2.4 cm on today's CT, "
            "stable from the prior study 4 months ago."
        )
        resp_idx = _seed_chat_encounter(
            store, user_id, patient_hash, session_id,
            user_text="how's the renal mass?",
            assistant_text=assistant_text,
        )

        # Extractor returns one finding citing verbatim text.
        finding = StructuredEntity(
            node_type="finding",
            content={"label": "left renal mass", "size_cm": 2.4},
            evidence_quote="The left renal mass measures 2.4 cm on today's CT",
            confidence=0.92,
        )
        ingester = ChatIngester(
            store=store, conn=conn,
            extractor=make_stub_extractor([finding]),
        )

        node_ids = ingester.ingest_encounter(
            user_id=user_id,
            patient_hash=patient_hash,
            encounter_id=session_id,
            source_event_idx=resp_idx,
        )

        assert len(node_ids) == 1

        # Verify projection state
        rows = conn.execute(
            "SELECT node_type, content_json FROM clinical_graph_nodes "
            "WHERE user_id = ? AND patient_hash = ? ORDER BY node_id",
            (user_id, patient_hash),
        ).fetchall()
        # Expect: patient node + finding node
        types = [r[0] for r in rows]
        assert "patient" in types
        assert "finding" in types

        # Verify provenance was written
        prov_row = conn.execute(
            "SELECT evidence_quote, extraction_model FROM node_provenance "
            "WHERE user_id = ? AND patient_hash = ? LIMIT 1",
            (user_id, patient_hash),
        ).fetchone()
        assert prov_row is not None
        assert "left renal mass measures 2.4 cm" in prov_row[0]

        # Verify edge was created: patient → finding
        edge_count = conn.execute(
            "SELECT COUNT(*) FROM clinical_graph_edges "
            "WHERE user_id = ? AND patient_hash = ? AND kind = 'mentions'",
            (user_id, patient_hash),
        ).fetchone()[0]
        assert edge_count >= 1

    def test_emits_archival_chain_in_event_log(self, store, conn):
        user_id = "dr_test"
        patient_hash = "p_chain"
        resp_idx = _seed_chat_encounter(
            store, user_id, patient_hash, "sess-x",
            user_text="check this",
            assistant_text="No abnormal findings on the latest film.",
        )

        finding = StructuredEntity(
            node_type="finding",
            content={"label": "no abnormal findings", "presence": False},
            evidence_quote="No abnormal findings on the latest film",
        )
        ingester = ChatIngester(
            store=store, conn=conn,
            extractor=make_stub_extractor([finding]),
        )
        ingester.ingest_encounter(
            user_id=user_id, patient_hash=patient_hash,
            encounter_id="sess-x", source_event_idx=resp_idx,
        )

        kinds = [
            r[0] for r in conn.execute(
                "SELECT event_kind FROM twin_event_log "
                "WHERE user_id = ? ORDER BY event_idx", (user_id,)
            ).fetchall()
        ]
        # Expect, in order: user_message, assistant_response, patient_registered,
        # ingestion_started, ingestion_llm_response, node_added, provenance_recorded,
        # edge_added, ingestion_completed.
        assert kinds[0] == "user_message"
        assert kinds[1] == "assistant_response"
        assert "ingestion_started" in kinds
        assert "ingestion_llm_response" in kinds
        assert "node_added" in kinds
        assert "provenance_recorded" in kinds
        assert "ingestion_completed" in kinds
        # ingestion_completed must come AFTER all node_added events
        last_node_added = max(i for i, k in enumerate(kinds) if k == "node_added")
        ingestion_completed_pos = kinds.index("ingestion_completed")
        assert ingestion_completed_pos > last_node_added


# ─────────────────────────────────────────────────────────────────────
# Quote verification — Rev-2 hallucination defense
# ─────────────────────────────────────────────────────────────────────

class TestQuoteVerification:
    """Hallucination defense now lives in the extractor (F20 + F-extractor
    -drops). The chat_ingester is a pass-through that trusts whatever the
    extractor returns — it no longer raises ``QuoteVerificationError`` on
    paraphrased entities. These tests verify the new boundary:

      * extractor drops paraphrased entities and bumps
        ``drops['not_verbatim']``
      * the resulting empty entity list means no clinical-fact node is
        emitted by chat_ingester (replay-equivalent to "nothing happened")

    The verbatim contract itself (``evidence_quote`` must be a substring
    of source_text, modulo NFC + fuzzy_rescue) is enforced in
    ``llm_extractor.llm_chat_extractor`` — see test below.
    """

    def test_extractor_drops_paraphrased_quotes(self):
        """Paraphrased quote → extractor drops it with reason
        ``not_verbatim`` (instead of the historical raise from
        chat_ingester). The drops counter is what surfaces to the UI."""
        from unittest.mock import patch
        from nexus_server.memorization.llm_extractor import llm_chat_extractor

        async def fake_call_llm(*_a, **_kw):
            return (
                '{"entities": ['
                ' {"node_type":"finding","content":{"label":"renal mass"},'
                '  "evidence_quote":"The left kidney mass is unchanged."}'
                ']}',
                "gemini-2.5-flash", "stop", [],
            )
        with patch("nexus_server.llm_gateway.call_llm", new=fake_call_llm):
            result = llm_chat_extractor("The left renal mass is stable.")
        assert result.raw_count == 1
        assert result.entities == []
        assert result.drops.get("not_verbatim") == 1

    def test_ingester_with_empty_extractor_emits_no_findings(self, store, conn):
        """When the extractor (correctly) drops every entity, chat_ingester
        commits the audit-trail events (ingestion_started + completed) but
        zero NODE_ADDED. Replay-equivalent to "no clinical facts" — the
        Memory tab stays empty, and the diagnostic banner can read the
        drop counters from INGESTION_COMPLETED.payload."""
        user_id = "dr_test"
        patient_hash = "p_partial"
        resp_idx = _seed_chat_encounter(
            store, user_id, patient_hash, "sess-p",
            user_text="?",
            assistant_text="Mass is 2.4 cm.",
        )

        from nexus_server.memorization.chat_ingester import ExtractionResult

        def empty_extractor(_source: str) -> ExtractionResult:
            """Stub that returns the same shape llm_chat_extractor would
            on a 'all paraphrased, nothing kept' batch."""
            return ExtractionResult(
                raw_llm_output="",
                entities=[],
                drops={"not_verbatim": 2},
                raw_count=2,
            )

        ingester = ChatIngester(store=store, conn=conn,
                                extractor=empty_extractor)
        ingester.ingest_encounter(
            user_id=user_id, patient_hash=patient_hash,
            encounter_id="sess-p", source_event_idx=resp_idx,
        )

        # No finding nodes — extractor dropped them all.
        finding_count = conn.execute(
            "SELECT COUNT(*) FROM clinical_graph_nodes "
            "WHERE user_id = ? AND patient_hash = ? AND node_type = 'finding'",
            (user_id, patient_hash),
        ).fetchone()[0]
        assert finding_count == 0

        # …but INGESTION_COMPLETED still got emitted with the drop counts,
        # so the UI banner can show "LLM gave 2, dropped 2 at not_verbatim".
        rows = conn.execute(
            "SELECT payload_json FROM twin_event_log "
            "WHERE user_id = ? AND patient_hash = ? "
            "  AND event_kind = 'ingestion_completed'",
            (user_id, patient_hash),
        ).fetchall()
        assert len(rows) == 1
        payload = json.loads(rows[0][0])
        assert payload["emitted_node_count"] == 0
        assert payload["raw_count"] == 2
        assert payload["drops"]["not_verbatim"] == 2


# ─────────────────────────────────────────────────────────────────────
# F-truncated-extract — partial-array recovery when the LLM stream
# gets cut off mid-entity (token-limit / network glitch).
# ─────────────────────────────────────────────────────────────────────

class TestTruncatedExtractRecovery:
    """The LLM emits ``{"entities": [..., {...}, {...}, {...incomplete``
    when it hits max_tokens (or for any other reason runs out of
    stream). The original parser saw "unparseable JSON" and dropped
    EVERYTHING, including the 2 well-formed entities at the front of
    the array.

    Layer-4 recovery in ``_parse_json_safe`` walks the array brace-
    aware and yields each complete object until it hits the truncation,
    so 2-of-N entities survive instead of 0-of-N. This is the bug the
    medic hit with patient 老王 (long PET-CT history → JSON cut off
    at the third finding, all entities silently dropped).
    """

    # The exact pattern reported from the desktop diagnostic banner —
    # three findings, the last one truncated mid-object.
    _OLAWANG_RAW = (
        '```json\n{"entities": [\n'
        '  {\n'
        '    "node_type": "finding",\n'
        '    "content": {\n'
        '      "label": "咳嗽",\n'
        '      "canonical_en": "cough"\n'
        '    },\n'
        '    "evidence_quote": "咳嗽 2 月",\n'
        '    "confidence": 1.0\n'
        '  },\n'
        '  {\n'
        '    "node_type": "finding",\n'
        '    "content": {\n'
        '      "label": "气紧",\n'
        '      "canonical_en": "shortness of breath"\n'
        '    },\n'
        '    "evidence_quote": "气紧 2 周",\n'
        '    "confidence": 1.0\n'
        '  },\n'
        '  {\n'
        '    "node_type": "finding"'   # <- truncated
    )

    def test_truncated_array_rescues_complete_objects(self):
        from nexus_server.memorization.llm_extractor import (
            _parse_json_safe, _recover_partial_entities,
        )
        # Direct recovery function — pulls 2 well-formed objects out.
        rescued = _recover_partial_entities(self._OLAWANG_RAW)
        assert len(rescued) == 2
        assert rescued[0]["content"]["label"] == "咳嗽"
        assert rescued[0]["evidence_quote"] == "咳嗽 2 月"
        assert rescued[1]["content"]["label"] == "气紧"
        assert rescued[1]["evidence_quote"] == "气紧 2 周"
        # Full parser short-circuits to recovery when the outer JSON
        # is busted, surfaces the rescued entities under "entities".
        parsed = _parse_json_safe(self._OLAWANG_RAW)
        assert len(parsed["entities"]) == 2

    def test_well_formed_json_unchanged_by_recovery(self):
        from nexus_server.memorization.llm_extractor import _parse_json_safe
        ok = (
            '{"entities": [{"node_type":"finding",'
            '"content":{"label":"x"},"evidence_quote":"q","confidence":0.9}]}'
        )
        parsed = _parse_json_safe(ok)
        assert len(parsed["entities"]) == 1
        assert parsed["entities"][0]["content"]["label"] == "x"

    def test_garbage_returns_empty(self):
        from nexus_server.memorization.llm_extractor import _parse_json_safe
        assert _parse_json_safe("hello world no json here") == {}
        assert _parse_json_safe("") == {}

    def test_braces_inside_string_dont_confuse_depth_tracker(self):
        from nexus_server.memorization.llm_extractor import _recover_partial_entities
        # The evidence_quote string literal contains a `}` — the
        # brace-aware walker MUST skip it. Without string-awareness
        # we'd mis-count depth and slice the entity in half.
        s = (
            '{"entities": ['
            '{"node_type":"finding","content":{"label":"x"},'
            '"evidence_quote":"the closing brace } is part of text",'
            '"confidence":0.9},'
            '{"node_type":"finding","content":{"label":"y"},'   # truncated
        )
        rescued = _recover_partial_entities(s)
        assert len(rescued) == 1
        assert rescued[0]["content"]["label"] == "x"
        assert "}" in rescued[0]["evidence_quote"]


# ─────────────────────────────────────────────────────────────────────
# Replay roundtrip — Contract B for chat_ingester
# ─────────────────────────────────────────────────────────────────────

class TestReplayRoundtrip:
    def test_drop_projections_replay_rebuilds_chat_state(self, store, conn):
        user_id = "dr_replay"
        patient_hash = "p_rb"
        resp_idx = _seed_chat_encounter(
            store, user_id, patient_hash, "sess-rb",
            user_text="any changes?",
            assistant_text=(
                "Mild progression: lesion grew from 2.1 to 2.4 cm. "
                "Recommend MR follow-up."
            ),
        )

        entities = [
            StructuredEntity(
                node_type="finding",
                content={"label": "lesion", "size_cm": 2.4, "delta_cm": 0.3},
                evidence_quote="lesion grew from 2.1 to 2.4 cm",
            ),
            StructuredEntity(
                node_type="ddx",
                content={"diagnosis": "RCC", "leading": True},
                evidence_quote="Recommend MR follow-up",
            ),
        ]
        ingester = ChatIngester(
            store=store, conn=conn,
            extractor=make_stub_extractor(entities),
        )
        # ddx isn't in the "provenance required" set so no provenance attached.
        # finding IS in the set so the helper builds provenance for it.
        # But add_node only attaches provenance for finding/measurement/semantic_fact;
        # ddx → no provenance, ingester logic handles correctly.
        emitted = ingester.ingest_encounter(
            user_id=user_id, patient_hash=patient_hash,
            encounter_id="sess-rb", source_event_idx=resp_idx,
        )
        assert len(emitted) == 2

        # Snapshot every projection table
        before = {
            t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3").fetchall()
            for t in PROJECTION_TABLES
        }

        # Full rebuild — drop projections, replay event_log
        full_rebuild(conn)

        after = {
            t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3").fetchall()
            for t in PROJECTION_TABLES
        }

        for table in PROJECTION_TABLES:
            assert before[table] == after[table], (
                f"projection {table} diverged after replay; "
                f"ChatIngester broke Contract B"
            )
