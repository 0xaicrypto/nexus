"""Shared fixtures for server tests."""
import os
import shutil
import tempfile
import pytest

# Use a temp file for test DB (in-memory doesn't persist across connections)
_test_db = os.path.join(tempfile.gettempdir(), "rune_test.db")
os.environ["SERVER_SECRET"] = "test-secret-key"
os.environ["GEMINI_API_KEY"] = "fake-key-for-testing"
os.environ["DATABASE_URL"] = f"sqlite:///{_test_db}"
# Phase B: the anchor retry daemon was removed. ``NEXUS_DISABLE_RETRY_DAEMON``
# used to exist here for test determinism — no longer needed; main.py
# doesn't start a daemon at all.

# Phase D: Twin path is opt-in for tests. The default in production is
# NEXUS_USE_TWIN=1, but every existing /llm/chat test mocks
# llm_gateway.call_llm — those mocks would never be hit if the request
# routed through DigitalTwin.chat() instead. Tests that specifically
# want to exercise the twin path set _test_override on twin_manager.
os.environ["NEXUS_USE_TWIN"] = "0"
os.environ["NEXUS_DISABLE_TWIN_REAPER"] = "1"

# Auth redesign: the per-IP+username rate limiter (5/min on
# login/claim/register) would trip across the hundreds of register
# calls the suite makes from the single TestClient IP. Disabled here;
# test_auth_password re-enables it locally to test the 429 path.
os.environ["NEXUS_AUTH_RATELIMIT_DISABLED"] = "1"

# S5 isolation: ``twin_event_log._db_path`` defaults to
# ~/.nexus_server/twins/{user_id}/event_log/{agent_id}.db. The S5 tests
# seed events via ``_test_append_event`` which creates real SQLite
# files; without an override they'd land in the developer's home dir
# (and in CI, in $HOME of the runner) and leak across runs. Pin to a
# tempdir we can wipe between tests.
_test_twin_dir = os.path.join(tempfile.gettempdir(), "rune_test_twins")
os.environ["NEXUS_TWIN_BASE_DIR"] = _test_twin_dir


@pytest.fixture(autouse=True)
def _init_db():
    """Fresh database AND fresh twin event_log dir for each test."""
    if os.path.exists(_test_db):
        os.remove(_test_db)
    if os.path.isdir(_test_twin_dir):
        shutil.rmtree(_test_twin_dir, ignore_errors=True)
    from nexus_server.database import init_db
    init_db()
    yield
    if os.path.exists(_test_db):
        os.remove(_test_db)
    if os.path.isdir(_test_twin_dir):
        shutil.rmtree(_test_twin_dir, ignore_errors=True)


@pytest.fixture
def app():
    """Create a fresh FastAPI app for each test."""
    from nexus_server.main import create_app
    return create_app()


@pytest.fixture
def client(app):
    """Create a test client."""
    from fastapi.testclient import TestClient
    return TestClient(app)
