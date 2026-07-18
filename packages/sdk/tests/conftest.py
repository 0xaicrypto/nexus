"""
Shared fixtures for Nexus SDK test suite.

Provides a clean StateManager + FlushPolicy per test, guaranteeing
test isolation (each test gets a fresh temp directory that is
automatically cleaned up).

Optional-extra gating: tests that import google-adk or the a2a-sdk
should skip cleanly when those extras aren't installed instead of
erroring at collection time. We achieve that with an
``collect_ignore_glob`` hook below — pytest skips the listed files
entirely when the relevant extra is missing, so a developer who
hasn't pulled in [adk] / [a2a] still gets a clean run on the rest
of the suite.
"""

import importlib.util
import os
import shutil
import sys
import tempfile

import pytest

# Ensure the SDK package is importable regardless of install state
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nexus_core.flush import FlushPolicy


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        # find_spec("google.adk") raises (not returns None) when the
        # parent package "google" itself is absent.
        return False


_ADK_AVAILABLE = _has_module("google.adk")
_A2A_AVAILABLE = _has_module("a2a")

# Files in this directory that import ``google.adk`` or ``a2a.types``
# at module top level. Listed here (not via per-file import-time skip)
# because pytest's collection runs the imports before any skip
# directive in the module body would fire.
collect_ignore_glob: list[str] = []
if not _ADK_AVAILABLE:
    collect_ignore_glob += [
        "test_artifact.py",
        "test_session_advanced.py",
        "test_state.py",
    ]
if not _A2A_AVAILABLE:
    collect_ignore_glob += ["test_a2a_task_store.py"]


@pytest.fixture
def tmp_state_dir(tmp_path):
    """Return a clean temp directory path string.  Cleaned automatically by pytest."""
    d = str(tmp_path / "rune_test")
    os.makedirs(d, exist_ok=True)
    return d


@pytest.fixture
def state_manager(tmp_state_dir):
    """Fresh StateManager in local mode."""
    return StateManager(base_dir=tmp_state_dir, mode="local")


@pytest.fixture
def flush_policy(tmp_state_dir):
    """FlushPolicy with WAL dir inside the test temp dir."""
    return FlushPolicy(wal_dir=os.path.join(tmp_state_dir, "wal"))
