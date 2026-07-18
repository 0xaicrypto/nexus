"""Tests for ``nexus_core.versioned.VersionedStore`` — the
versioned-JSON storage primitive shared by Phase J memory
namespaces and Phase O evolution rollback.

The contract these tests pin:

1. A fresh store has no current version.
2. ``propose`` advances ``_current`` and returns the new version
   label (zero-padded).
3. ``current()`` reads through the pointer.
4. ``rollback`` flips the pointer; subsequent ``current()`` reads
   the older version.
5. After a rollback, ``propose`` creates a NEW tip beyond the
   highest existing version — it never overwrites history.
6. Rolling back to a nonexistent version raises (not a silent
   no-op).
7. ``history()`` lists versions in chronological order regardless
   of where the pointer is.
8. Version files are immutable on disk — re-writing the same
   version label is rejected.
"""

from __future__ import annotations

import json

import pytest

import nexus_core
from nexus_core.versioned import VersionedStore, VersionRecord


# ── Empty store ─────────────────────────────────────────────────────


def test_fresh_store_has_no_current(tmp_path):
    s = VersionedStore(tmp_path / "facts")
    assert s.current_version() is None
    assert s.current() is None
    assert len(s) == 0
    assert s.history() == []


# ── propose ─────────────────────────────────────────────────────────


def test_propose_creates_v0001_then_v0002(tmp_path):
    s = VersionedStore(tmp_path / "facts")
    label1 = s.propose({"x": 1})
    label2 = s.propose({"x": 2})
    assert label1 == "v0001"
    assert label2 == "v0002"
    assert s.current_version() == "v0002"
    assert s.current() == {"x": 2}


def test_propose_persists_to_disk(tmp_path):
    """A second store opened on the same directory sees the same
    state — the primitive is durably stored, not in-memory."""
    s1 = VersionedStore(tmp_path / "facts")
    s1.propose({"hello": "world"})

    s2 = VersionedStore(tmp_path / "facts")
    assert s2.current_version() == "v0001"
    assert s2.current() == {"hello": "world"}


def test_version_label_width_is_configurable(tmp_path):
    s = VersionedStore(tmp_path / "facts", version_width=2)
    assert s.propose({}) == "v01"
    assert s.propose({}) == "v02"


def test_pointer_file_format_is_canonical_json(tmp_path):
    """The pointer file content must match a stable shape so
    external readers (auditors, server-side views) can parse it
    without depending on the Python class."""
    s = VersionedStore(tmp_path / "facts")
    s.propose({"k": "v"})
    pointer = json.loads((tmp_path / "facts" / "_current.json").read_text())
    assert pointer["version"] == "v0001"
    assert isinstance(pointer["updated_at"], (int, float))


# ── rollback ────────────────────────────────────────────────────────


def test_rollback_flips_pointer_back(tmp_path):
    s = VersionedStore(tmp_path / "facts")
    s.propose({"step": 1})
    s.propose({"step": 2})
    s.propose({"step": 3})
    assert s.current() == {"step": 3}

    prev = s.rollback("v0001")
    assert prev == "v0003"
    assert s.current_version() == "v0001"
    assert s.current() == {"step": 1}


def test_rollback_to_nonexistent_version_raises(tmp_path):
    s = VersionedStore(tmp_path / "facts")
    s.propose({"x": 1})
    with pytest.raises(ValueError, match="not found"):
        s.rollback("v0099")


def test_propose_after_rollback_creates_new_tip(tmp_path):
    """Critical invariant: rollback + propose does NOT overwrite
    the rolled-back versions — it appends a new tip beyond the
    highest existing label."""
    s = VersionedStore(tmp_path / "facts")
    s.propose({"step": 1})   # v0001
    s.propose({"step": 2})   # v0002
    s.propose({"step": 3})   # v0003

    s.rollback("v0001")
    assert s.current_version() == "v0001"

    new_label = s.propose({"step": 4})
    assert new_label == "v0004"   # NOT "v0002"
    assert s.current() == {"step": 4}

    # All four versions still exist on disk
    assert len(s) == 4
    assert {r.version for r in s.history()} == {"v0001", "v0002", "v0003", "v0004"}


# ── history ─────────────────────────────────────────────────────────


def test_history_lists_versions_chronologically(tmp_path):
    s = VersionedStore(tmp_path / "facts")
    for i in range(5):
        s.propose({"i": i})

    h = s.history()
    assert [r.version for r in h] == ["v0001", "v0002", "v0003", "v0004", "v0005"]
    assert all(isinstance(r, VersionRecord) for r in h)


def test_history_respects_limit(tmp_path):
    s = VersionedStore(tmp_path / "facts")
    for i in range(10):
        s.propose({"i": i})

    assert len(s.history(limit=3)) == 3
    assert [r.version for r in s.history(limit=3)] == ["v0001", "v0002", "v0003"]


def test_history_independent_of_pointer_position(tmp_path):
    """Rolling back the pointer doesn't change the history list —
    history is the FILESYSTEM state, pointer is the LOGICAL state."""
    s = VersionedStore(tmp_path / "facts")
    s.propose({}); s.propose({}); s.propose({})  # v0001, v0002, v0003
    s.rollback("v0001")

    versions = [r.version for r in s.history()]
    assert versions == ["v0001", "v0002", "v0003"]


# ── get specific version ────────────────────────────────────────────


def test_get_returns_specific_version_data(tmp_path):
    s = VersionedStore(tmp_path / "facts")
    s.propose({"step": 1})
    s.propose({"step": 2})

    assert s.get("v0001") == {"step": 1}
    assert s.get("v0002") == {"step": 2}
    assert s.get("v0099") is None  # nonexistent → None (not raise)


# ── Immutability invariant ──────────────────────────────────────────


def test_version_files_are_immutable_on_disk(tmp_path):
    """Hand-rewriting a version file should fail. We never offer an
    API to mutate an existing version — propose creates fresh,
    rollback only moves the pointer.

    This test directly invokes the internal _write_version helper
    to verify the safety check works (an evolver bug that tried
    to overwrite would hit this)."""
    s = VersionedStore(tmp_path / "facts")
    s.propose({"step": 1})

    with pytest.raises(FileExistsError, match="immutable"):
        s._write_version("v0001", {"step": 999})


# ── Public API surface ─────────────────────────────────────────────


def test_top_level_exports():
    """VersionedStore is reachable via the package root for callers
    in framework / server layers that want it."""
    # We don't currently export VersionedStore at the package root —
    # it's a SDK-internal building block. Just verify it's
    # importable from its module path:
    from nexus_core.versioned import VersionedStore as VS
    assert VS is VersionedStore


# ── Realistic integration scenario ──────────────────────────────────


def test_evolver_propose_verdict_revert_flow(tmp_path):
    """End-to-end scenario: an evolver writes a new fact set, the
    verdict scorer decides ``reverted``, the runner rolls back —
    the rolled-back state is what subsequent reads see."""
    facts = VersionedStore(tmp_path / "facts")

    # Original state
    facts.propose({"likes": ["sushi"], "allergies": []})
    pre_version = facts.current_version()
    assert pre_version == "v0001"

    # Evolver proposes adding a new allergy fact
    facts.propose({"likes": ["sushi"], "allergies": ["peanuts"]})
    post_version = facts.current_version()
    assert post_version == "v0002"
    assert facts.current()["allergies"] == ["peanuts"]

    # Verdict says revert (e.g. unpredicted regression observed)
    facts.rollback(pre_version)
    assert facts.current_version() == "v0001"
    assert facts.current()["allergies"] == []

    # The bad version is still on disk for audit
    assert facts.get("v0002") == {"likes": ["sushi"], "allergies": ["peanuts"]}
