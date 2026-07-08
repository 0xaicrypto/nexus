"""
Integration tests for the U3.3 endpoints added during the desktop-v2
overhaul. Bypasses JWT (the existing test pattern — see
test_memory_router_v2.py) and pins routers' tmp dirs via env so we can
boot multiple routers in the same TestClient without crossing state.

Covers:

  * settings_router      GET / PUT /api/v1/settings/llm
                         - status starts with no keys + advisory
                         - PUT writes to .env atomically, masks output
                         - in-process config updates so next read sees keys
                         - advisory clears when active provider gets a key

  * export_router        GET /api/v1/export/archive_path
                         POST /api/v1/export/bundle
                         - archive_path resolves under NEXUS_ARCHIVE_DIR
                         - bundle 404s before EventLog exists
                         - bundle succeeds after EventLog is seeded;
                           returns counts, the zip exists, the zip
                           contains manifest.json + twin_event_log.db

  * patients_router      DELETE /api/v1/dicom/patients/{hash}
                         - 404 when nothing matches
                         - 200 when manual row exists, manual row goes
                           away on subsequent list, deleted counts > 0

Each test isolates state via tmp_path + env overrides; nothing persists
across tests.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from nexus_server.auth.routes import get_current_user


# ─────────────────────────────────────────────────────────────────────
# Shared scaffolding
# ─────────────────────────────────────────────────────────────────────

TEST_USER = "dr_jz_test"


@pytest.fixture
def reset_llm_config(monkeypatch):
    """conftest.py sets ``GEMINI_API_KEY=fake-key-for-testing`` at import
    time, and ServerConfig reads env at class definition (so the
    singleton's attribute is already populated). To exercise the
    no-key-configured branch we have to BOTH clear the env (so future
    ``settings_router._apply_to_running_config`` calls don't re-seed it)
    AND null out the live singleton fields. Restored when the test
    exits via monkeypatch's teardown for env + an explicit teardown for
    config attributes."""
    for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "gemini")

    # ``get_config()`` returns a fresh ServerConfig() each call, so the
    # class attributes (populated at import time from env) are the
    # source of truth. Patch the class, not an instance.
    from nexus_server.config import ServerConfig
    snapshot = {
        "GEMINI_API_KEY":        ServerConfig.GEMINI_API_KEY,
        "OPENAI_API_KEY":        ServerConfig.OPENAI_API_KEY,
        "ANTHROPIC_API_KEY":     ServerConfig.ANTHROPIC_API_KEY,
        "DEFAULT_LLM_PROVIDER":  ServerConfig.DEFAULT_LLM_PROVIDER,
        "DEFAULT_LLM_MODEL":     ServerConfig.DEFAULT_LLM_MODEL,
    }
    ServerConfig.GEMINI_API_KEY    = None   # type: ignore[assignment]
    ServerConfig.OPENAI_API_KEY    = None   # type: ignore[assignment]
    ServerConfig.ANTHROPIC_API_KEY = None   # type: ignore[assignment]
    ServerConfig.DEFAULT_LLM_PROVIDER = "gemini"
    yield ServerConfig
    for k, v in snapshot.items():
        setattr(ServerConfig, k, v)


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    """Pin every user-data dir to per-test tmp paths so tests are
    hermetic. Returns the resolved paths so the test can poke at them."""
    rune_home = tmp_path / "rune_home"
    rune_home.mkdir(parents=True, exist_ok=True)
    archive = tmp_path / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    twin_base = tmp_path / "twins"
    twin_base.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("RUNE_HOME", str(rune_home))
    monkeypatch.setenv("NEXUS_ARCHIVE_DIR", str(archive))
    monkeypatch.setenv("NEXUS_TWIN_BASE_DIR", str(twin_base))

    return {"rune_home": rune_home, "archive": archive, "twin_base": twin_base}


@pytest.fixture
def app(isolated_dirs, monkeypatch):
    """Build a slim app with just the U3.3 routers + a fake auth dep.
    We don't use the full create_app() — way too much surface for what
    we're testing, and it touches startup hooks (DICOM index, twin
    reaper) we don't want to spin up.

    Uses monkeypatch.setattr for module-level patches so they auto-
    revert at teardown — otherwise the patched ``get_db_connection``
    leaks into sibling test files (test_files_endpoints etc.) that
    rely on the real schema.
    """
    from nexus_server import settings_router, export_router, patients_router

    # Force the patients_router DB to a tempfile.
    db_path = isolated_dirs["rune_home"] / "patients.db"

    from contextlib import contextmanager
    import nexus_server.database as _db_mod
    import nexus_server.patients_router as _pat_mod

    @contextmanager
    def _fake_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # Stand up the tables the delete touches — using IF NOT EXISTS
        # so duplicate calls are safe.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS dicom_studies (
                user_id TEXT, patient_hash TEXT, study_id TEXT
            );
            CREATE TABLE IF NOT EXISTS uploads (
                user_id TEXT, patient_hash TEXT, file_id TEXT
            );
            CREATE TABLE IF NOT EXISTS patient_memory (
                user_id TEXT, patient_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS clinical_graph_nodes (
                user_id TEXT, patient_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                user_id TEXT, session_id TEXT, patient_hash TEXT
            );
            """
        )
        try:
            yield conn
        finally:
            conn.commit()
            conn.close()

    def _fake_conn():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # Use monkeypatch so these revert at teardown. Without this,
    # subsequent test files (e.g. test_files_endpoints) inherit the
    # patched get_db_connection and hit our 3-column uploads table.
    monkeypatch.setattr(_pat_mod, "_conn", _fake_conn)
    monkeypatch.setattr(_db_mod, "get_db_connection", _fake_db)

    # Set up the patients table the router writes to.
    with _fake_conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS patients (
                patient_hash TEXT, user_id TEXT,
                initials TEXT DEFAULT '', mrn TEXT DEFAULT '',
                age_group TEXT DEFAULT '', age_value INTEGER DEFAULT 0,
                sex TEXT DEFAULT '', chief_complaint TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at INTEGER, updated_at INTEGER,
                PRIMARY KEY (user_id, patient_hash)
            )
            """
        )
        c.commit()

    a = FastAPI()
    a.include_router(settings_router.router)
    a.include_router(export_router.router)
    a.include_router(patients_router.router)

    async def fake_user() -> str:
        return TEST_USER

    a.dependency_overrides[get_current_user] = fake_user
    return a


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


# ─────────────────────────────────────────────────────────────────────
# settings_router
# ─────────────────────────────────────────────────────────────────────


class TestLlmSettings:
    def test_status_starts_empty_with_advisory(self, client, reset_llm_config):
        r = client.get("/api/v1/settings/llm")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["provider"] == "gemini"
        assert body["has_gemini_key"] is False
        assert body["advisory"] is not None
        assert "GEMINI_API_KEY" in body["advisory"]

    def test_put_writes_env_and_updates_config(self, client, isolated_dirs, reset_llm_config):
        r = client.put("/api/v1/settings/llm", json={
            "provider": "gemini",
            "gemini_api_key": "AIza-test-key-1234",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert "GEMINI_API_KEY" in body["written_keys"]
        assert body["status"]["has_gemini_key"] is True
        assert body["status"]["advisory"] is None

        # .env file should now exist + contain the key.
        env_file = isolated_dirs["rune_home"] / ".env"
        assert env_file.exists()
        text = env_file.read_text()
        assert "GEMINI_API_KEY=AIza-test-key-1234" in text

        # Subsequent GET reflects state without restart.
        r2 = client.get("/api/v1/settings/llm")
        assert r2.status_code == 200
        assert r2.json()["has_gemini_key"] is True
        assert r2.json()["advisory"] is None

    def test_put_rejects_unknown_provider(self, client):
        r = client.put("/api/v1/settings/llm", json={"provider": "deepmind"})
        assert r.status_code == 400, r.text

    def test_put_empty_body_rejected(self, client):
        r = client.put("/api/v1/settings/llm", json={})
        assert r.status_code == 400, r.text

    def test_advisory_changes_when_switching_provider(
        self, client, isolated_dirs, reset_llm_config,
    ):
        # Start: Gemini key set, OpenAI key not. Switch active provider
        # to OpenAI; advisory should now flag OPENAI_API_KEY missing.
        client.put("/api/v1/settings/llm",
                   json={"gemini_api_key": "AIza-test-key"})
        switch = client.put("/api/v1/settings/llm",
                            json={"provider": "openai"})
        assert switch.status_code == 200
        adv = switch.json()["status"]["advisory"]
        assert adv is not None
        assert "OPENAI_API_KEY" in adv


# ─────────────────────────────────────────────────────────────────────
# export_router
# ─────────────────────────────────────────────────────────────────────


class TestExport:
    def test_archive_path_returns_isolated_dir(self, client, isolated_dirs):
        r = client.get("/api/v1/export/archive_path")
        assert r.status_code == 200, r.text
        assert r.json()["path"] == str(isolated_dirs["archive"])

    def test_bundle_404_when_no_event_log(self, client):
        r = client.post("/api/v1/export/bundle")
        assert r.status_code == 404, r.text
        assert "twin_event_log not found" in r.json()["detail"]

    def test_bundle_succeeds_when_event_log_exists(self, client, isolated_dirs):
        # Seed a fake twin_event_log at the path the router reads.
        from nexus_server.twin_event_log import _db_path
        evt_db = _db_path(TEST_USER)
        evt_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(evt_db) as c:
            c.executescript(
                """
                CREATE TABLE twin_event_log (
                    id INTEGER PRIMARY KEY, kind TEXT, payload TEXT
                );
                INSERT INTO twin_event_log (kind, payload) VALUES ('x','{}');
                INSERT INTO twin_event_log (kind, payload) VALUES ('y','{}');
                """
            )
            c.commit()

        r = client.post("/api/v1/export/bundle")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["bundle_path"].endswith(".zip")
        assert pathlib.Path(body["bundle_path"]).exists()
        assert body["bytes"] > 0
        assert body["counts"].get("twin_event_log") == 2

        # Verify the zip contents.
        with zipfile.ZipFile(body["bundle_path"]) as z:
            names = set(z.namelist())
            assert "manifest.json" in names
            assert "twin_event_log.db" in names
            assert "README.txt" in names
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
            assert manifest["user_id"] == TEST_USER
            assert manifest["counts"]["twin_event_log"] == 2


# ─────────────────────────────────────────────────────────────────────
# patients_router DELETE
# ─────────────────────────────────────────────────────────────────────


class TestDeletePatient:
    def test_idempotent_when_nothing_matches(self, client):
        """Delete is intentionally idempotent — the projection-clear
        contract is "forget this patient", and forgetting an already-
        absent patient is success, not failure. The previous 404 on
        empty-match returned spurious errors in the common case where
        a sidebar projection had a stale entry but the DELETE found
        nothing in the canonical tables. See patients_router.delete_patient
        (lines 681–688) for the rationale."""
        r = client.delete("/api/v1/dicom/patients/deadbeefdeadbeef")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["patient_hash"] == "deadbeefdeadbeef"
        # Every counter must be zero — there was nothing to delete.
        assert all(v == 0 for v in body["deleted"].values()), body

    def test_200_after_register_then_delete(self, client):
        # Insert a manual row directly (the existing register endpoint
        # uses a hash derived from initials/mrn — for the delete test
        # we don't need that, we just need a row to exist).
        # F-merge-patients-db — `patients` is now in the shared DB.
        from nexus_server.database import get_db_connection
        from nexus_server.patients_router import init_patients_table
        init_patients_table()
        ph = "abc1234567890def"
        with get_db_connection() as c:
            c.execute(
                "INSERT INTO patients(patient_hash, user_id, initials, "
                "mrn, age_group, age_value, sex, chief_complaint, "
                "notes, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ph, TEST_USER, "J.D.", "", "40-49", 45, "M",
                 "test", "", 1, 1),
            )
            c.commit()

        r = client.delete(f"/api/v1/dicom/patients/{ph}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["patient_hash"] == ph
        assert body["deleted"]["patients"] == 1

        # A second delete is idempotent — returns 200 with deleted=0
        # for every counter (see test_idempotent_when_nothing_matches
        # for rationale).
        r2 = client.delete(f"/api/v1/dicom/patients/{ph}")
        assert r2.status_code == 200, r2.text
        assert r2.json()["deleted"]["patients"] == 0

    def test_scopes_to_caller(self, client, monkeypatch):
        """Deleting under user A must not touch user B's row with the
        same patient_hash."""
        # F-merge-patients-db — `patients` now lives in the shared DB.
        from nexus_server.database import get_db_connection
        from nexus_server.patients_router import init_patients_table
        init_patients_table()
        ph = "shared_hash_xyz"
        with get_db_connection() as c:
            c.execute(
                "INSERT INTO patients(patient_hash, user_id, initials, "
                "mrn, age_group, age_value, sex, chief_complaint, "
                "notes, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ph, TEST_USER,  "J.D.", "", "", 0, "", "", "", 1, 1),
            )
            c.execute(
                "INSERT INTO patients(patient_hash, user_id, initials, "
                "mrn, age_group, age_value, sex, chief_complaint, "
                "notes, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ph, "OTHER_USER", "K.B.", "", "", 0, "", "", "", 1, 1),
            )
            c.commit()

        r = client.delete(f"/api/v1/dicom/patients/{ph}")
        assert r.status_code == 200, r.text
        assert r.json()["deleted"]["patients"] == 1

        # OTHER_USER's row must still be there.
        with get_db_connection() as c:
            row = c.execute(
                "SELECT initials FROM patients WHERE user_id=? AND patient_hash=?",
                ("OTHER_USER", ph),
            ).fetchone()
        assert row is not None
        assert row[0] == "K.B."
