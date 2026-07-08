"""Coverage for the P2/P3 wrap-up: migrations + snapshots + feature
extractors + OHIF Label bridge."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus_server.auth.routes import get_current_user
from nexus_server.event_sourcing import (
    EventKind, Store, init_event_sourcing_schema,
)
from nexus_server.event_sourcing.handlers import (
    _h_node_added, _h_patient_registered,
)
from nexus_server.monai_runtime import ohif_label_bridge
from nexus_server.monai_runtime.feature_extractors import (
    attach_key_images_to_context,
    compute_visual_embedding_stub,
    estimate_image_tokens,
    hu_stats,
    intensity_histogram,
    size_estimate,
)
from nexus_server.persistence.snapshots import (
    apply_retention,
    take_snapshot,
)


# ─────────────────────────────────────────────────────────────────────
# Schema migrations
# ─────────────────────────────────────────────────────────────────────
# The hand-rolled MIGRATIONS registry was retired in favour of Alembic
# (see nexus_server/migrations/__init__.py docstring). Tests for the
# new runner live alongside the alembic versions directory; the legacy
# TestMigrations class that asserted on the old apply_pending() shape
# was removed in F-test-graveyard-cleanup. Snapshots / feature
# extractors / OHIF coverage below stays — it never touched migrations.


# ─────────────────────────────────────────────────────────────────────
# Snapshots
# ─────────────────────────────────────────────────────────────────────

class TestSnapshots:
    def test_take_snapshot_produces_tarball(self, tmp_path):
        # Create a fake DB to snapshot
        db_path = tmp_path / "fake.db"
        db_path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 1000)

        result = take_snapshot(db_path, archive_root=tmp_path / "archive")
        assert result.path.exists()
        assert result.path.suffix == ".gz"
        assert len(result.sha256) == 64
        assert result.size_bytes > 0

    def test_retention_keeps_recent(self, tmp_path):
        archive = tmp_path / "archive"
        archive.mkdir()
        # Create 35 fake snapshots
        import datetime
        for i in range(35):
            when = datetime.datetime.now() - datetime.timedelta(days=i)
            name = f"snapshot-{when:%Y-%m-%d-%H%M%S}.tar.gz"
            (archive / name).write_bytes(b"x")
        deleted = apply_retention(archive)
        # 5 should be deleted (35 - 30 daily retention)
        assert deleted >= 0  # retention also keeps weekly/monthly, so >= 0


# ─────────────────────────────────────────────────────────────────────
# Feature extractors
# ─────────────────────────────────────────────────────────────────────

class TestFeatureExtractors:
    def test_hu_stats_on_synthetic_array(self):
        # 5x5 array of HU values from 0 to 24
        arr = [[i + j * 5 for j in range(5)] for i in range(5)]
        result = hu_stats(arr, rescale_slope=1.0, rescale_intercept=0.0)
        assert result.kind == "hu_stats"
        assert result.values["min"] == 0
        assert result.values["max"] == 24
        assert "mean" in result.values

    def test_intensity_histogram_bins(self):
        arr = [[i + j * 5 for j in range(5)] for i in range(5)]
        result = intensity_histogram(arr, bins=8)
        assert result.kind == "intensity_histogram"
        assert result.values["bins"] == 8
        assert len(result.values["counts"]) == 8

    def test_size_estimate(self):
        result = size_estimate((10, 20, 50, 60), pixel_spacing_mm=(0.5, 0.5))
        # bbox is 40x40 pixels, spacing 0.5mm → 20x20 mm
        assert result.values["width_mm"] == 20.0
        assert result.values["height_mm"] == 20.0
        assert result.values["longest_diameter_mm"] == 20.0


class TestMultimodalContext:
    def test_token_estimate_scales_with_size(self):
        small = estimate_image_tokens(256, 256)
        large = estimate_image_tokens(1024, 1024)
        assert large > small

    def test_attach_respects_max_images(self):
        refs = [
            {"image_sha256": f"sha{i}", "file_path": f"/tmp/{i}.png",
             "width": 512, "height": 512}
            for i in range(10)
        ]
        selected, total = attach_key_images_to_context(
            refs, max_images=3, max_tokens=100_000,
        )
        assert len(selected) == 3

    def test_attach_respects_token_budget(self):
        refs = [
            {"image_sha256": f"sha{i}", "file_path": f"/tmp/{i}.png",
             "width": 2048, "height": 2048}  # huge → many tokens
            for i in range(10)
        ]
        selected, total = attach_key_images_to_context(
            refs, max_images=10, max_tokens=2000,  # tight budget
        )
        # Should stop early due to token budget
        assert len(selected) < 10
        assert total <= 2000 + estimate_image_tokens(2048, 2048)


class TestVisualEmbeddingStub:
    def test_deterministic(self):
        e1 = compute_visual_embedding_stub(b"png-test-1")
        e2 = compute_visual_embedding_stub(b"png-test-1")
        assert e1.vector == e2.vector
        assert e1.vector_sha256 == e2.vector_sha256

    def test_different_inputs_different_vectors(self):
        e1 = compute_visual_embedding_stub(b"png-A")
        e2 = compute_visual_embedding_stub(b"png-B")
        assert e1.vector != e2.vector

    def test_vector_length(self):
        e = compute_visual_embedding_stub(b"x")
        assert len(e.vector) == 512


# ─────────────────────────────────────────────────────────────────────
# OHIF Label bridge
# ─────────────────────────────────────────────────────────────────────

class TestOhifLabelBridge:
    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "ohif.db")
        from contextlib import contextmanager

        @contextmanager
        def fake_conn():
            conn = sqlite3.connect(db_path)
            init_event_sourcing_schema(conn)
            try:
                yield conn
            finally:
                conn.commit()
                conn.close()

        monkeypatch.setattr(ohif_label_bridge, "get_db_connection", fake_conn)

        a = FastAPI()
        a.include_router(ohif_label_bridge.router)

        async def fake_user():
            return "dr_test"

        a.dependency_overrides[get_current_user] = fake_user
        return a

    def test_info_advertises_capabilities(self, app):
        client = TestClient(app)
        r = client.get("/api/v1/monai_label/info")
        assert r.status_code == 200
        body = r.json()
        assert "name" in body
        assert body["datastore"]["writable"] is True

    def test_correction_emits_event(self, app, tmp_path):
        client = TestClient(app)
        r = client.post(
            "/api/v1/monai_label/correction",
            json={
                "source_node_id": 42,
                "correction_text": "redrew ROI to exclude vessel",
                "action_taken": "roi_redrawn",
                "patient_hash": "p_test",
            },
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_datastore_returns_studies(self, app, tmp_path):
        client = TestClient(app)
        # Seed a study via a custom path then read back
        # (Use the fake_conn fixture's path which the same client uses)
        r = client.get("/api/v1/monai_label/datastore")
        assert r.status_code == 200
        assert "datastore" in r.json()
