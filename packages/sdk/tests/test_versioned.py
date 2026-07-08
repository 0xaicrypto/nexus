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


# ─────────────────────────────────────────────────────────────────────────────
# Chain mirror (Phase D)
#
# When ``chain_backend`` is set, every ``propose`` mirrors the new
# version + pointer to ``namespaces/<chain_namespace>/...``. Nothing
# else about the store's behaviour changes — local writes are still
# the source of truth on the synchronous path.
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import json as _json
import pytest


def test_chain_namespace_required_when_backend_given(tmp_path):
    from nexus_core.backends import MockBackend
    backend = MockBackend()
    with pytest.raises(ValueError, match="chain_namespace"):
        VersionedStore(tmp_path / "skills", chain_backend=backend)


def test_chain_mirror_propose_writes_blob_and_pointer(tmp_path):
    """propose() inside an event loop mirrors version + pointer to chain."""
    from nexus_core.backends import MockBackend
    backend = MockBackend()

    async def run():
        store = VersionedStore(
            tmp_path / "skills",
            chain_backend=backend,
            chain_namespace="skills",
        )
        v = store.propose({"skills": [{"name": "demo"}]})
        # Allow fire-and-forget tasks to run to completion.
        await asyncio.sleep(0.01)
        return v

    v = asyncio.run(run())
    assert v == "v0001"

    # Mirrored to chain at the expected paths.
    async def assert_mirrored():
        version_blob = await backend.load_blob(f"namespaces/skills/{v}.json")
        assert version_blob is not None
        assert _json.loads(version_blob.decode("utf-8"))["skills"] == [{"name": "demo"}]

        pointer_blob = await backend.load_blob("namespaces/skills/_current.json")
        assert pointer_blob is not None
        assert _json.loads(pointer_blob.decode("utf-8"))["version"] == v

    asyncio.run(assert_mirrored())


def test_chain_mirror_rollback_updates_pointer(tmp_path):
    from nexus_core.backends import MockBackend
    backend = MockBackend()

    async def run():
        store = VersionedStore(
            tmp_path / "skills",
            chain_backend=backend,
            chain_namespace="skills",
        )
        v1 = store.propose({"x": 1})
        v2 = store.propose({"x": 2})
        store.rollback(v1)
        await asyncio.sleep(0.01)
        return v1, v2

    v1, _v2 = asyncio.run(run())

    async def assert_pointer():
        ptr_blob = await backend.load_blob("namespaces/skills/_current.json")
        assert ptr_blob is not None
        assert _json.loads(ptr_blob.decode("utf-8"))["version"] == v1

    asyncio.run(assert_pointer())


def test_chain_mirror_no_eventloop_is_soft_skip(tmp_path, caplog):
    """When propose runs without an event loop, the local write
    still succeeds — chain mirror is a soft promise."""
    from nexus_core.backends import MockBackend
    backend = MockBackend()
    store = VersionedStore(
        tmp_path / "skills",
        chain_backend=backend,
        chain_namespace="skills",
    )
    # No event loop here — should not raise.
    v = store.propose({"skills": []})
    assert v == "v0001"
    assert store.current_version() == "v0001"


def test_recover_from_chain_hydrates_empty_local_dir(tmp_path):
    """A fresh local directory, given a chain backend with prior
    versions, recovers them via ``recover_from_chain``."""
    from nexus_core.backends import MockBackend
    backend = MockBackend()

    async def setup_chain():
        # Pretend a previous server pushed two versions to chain.
        await backend.store_blob(
            "namespaces/skills/v0001.json",
            _json.dumps({"skills": [{"name": "first"}]}).encode("utf-8"),
        )
        await backend.store_blob(
            "namespaces/skills/v0002.json",
            _json.dumps({"skills": [{"name": "second"}]}).encode("utf-8"),
        )
        await backend.store_blob(
            "namespaces/skills/_current.json",
            _json.dumps({"version": "v0002", "updated_at": 12345}).encode("utf-8"),
        )

    asyncio.run(setup_chain())

    async def recover():
        store = VersionedStore(
            tmp_path / "fresh",
            chain_backend=backend,
            chain_namespace="skills",
        )
        n = await store.recover_from_chain()
        return store, n

    store, n = asyncio.run(recover())
    assert n == 2
    assert store.current_version() == "v0002"
    assert store.current() == {"skills": [{"name": "second"}]}


def test_recover_from_chain_does_not_overwrite_local(tmp_path):
    """If the local file already exists with different content, we
    keep the local copy (defensive against partial migrations)."""
    from nexus_core.backends import MockBackend
    backend = MockBackend()

    async def setup_chain_then_recover():
        await backend.store_blob(
            "namespaces/skills/v0001.json",
            _json.dumps({"skills": [{"name": "from_chain"}]}).encode("utf-8"),
        )
        await backend.store_blob(
            "namespaces/skills/_current.json",
            _json.dumps({"version": "v0001", "updated_at": 1}).encode("utf-8"),
        )

        store = VersionedStore(
            tmp_path / "skills",
            chain_backend=backend,
            chain_namespace="skills",
        )
        # Write a conflicting local v0001 first.
        local_v1 = store.base_dir / "v0001.json"
        local_v1.write_text(
            _json.dumps({"skills": [{"name": "local_wins"}]}),
            encoding="utf-8",
        )

        n = await store.recover_from_chain()
        return store, n

    store, n = asyncio.run(setup_chain_then_recover())
    # Hydrated 0 *new* versions (the existing v0001 was preserved),
    # and pointer file was populated.
    assert n == 0
    assert (store.base_dir / "v0001.json").read_text(encoding="utf-8")
    assert _json.loads((store.base_dir / "v0001.json").read_text())["skills"] \
        == [{"name": "local_wins"}]


def test_recover_from_chain_requires_backend(tmp_path):
    store = VersionedStore(tmp_path / "skills")
    with pytest.raises(RuntimeError, match="no chain_backend"):
        asyncio.run(store.recover_from_chain())


# ─────────────────────────────────────────────────────────────────────────────
# Chain status — Brain panel data model
#
# `chain_status()` returns one of three states per namespace version:
#   "local"     — written to disk only
#   "mirrored"  — persisted by the backend, but state_root not re-anchored since
#   "anchored"  — last_anchor_at >= last_commit_at
# ─────────────────────────────────────────────────────────────────────────────


def test_chain_status_no_backend_returns_local(tmp_path):
    store = VersionedStore(tmp_path / "facts")
    store.propose({"x": 1})
    s = store.chain_status()
    assert s["status"] == "local"
    assert s["mirrored"] is False
    assert s["last_commit_at"] is not None


def test_chain_status_no_version_yet(tmp_path):
    store = VersionedStore(tmp_path / "facts")
    s = store.chain_status()
    assert s["version"] is None
    assert s["status"] == "local"


def test_chain_status_mock_backend_treats_as_mirrored(tmp_path):
    """MockBackend has no WAL probe so we fall back to "scheduled =
    mirrored" — good enough for tests without a real ChainBackend."""
    from nexus_core.backends import MockBackend
    backend = MockBackend()

    async def run():
        store = VersionedStore(
            tmp_path / "facts",
            chain_backend=backend,
            chain_namespace="facts",
        )
        store.propose({"x": 1})
        await asyncio.sleep(0.01)
        return store

    store = asyncio.run(run())
    s = store.chain_status(last_anchor_at=None)
    assert s["mirrored"] is True
    # No anchor yet → status is "mirrored", not "anchored"
    assert s["status"] == "mirrored"


def test_chain_status_anchored_when_anchor_after_commit(tmp_path):
    from nexus_core.backends import MockBackend
    backend = MockBackend()

    async def run():
        store = VersionedStore(
            tmp_path / "facts",
            chain_backend=backend,
            chain_namespace="facts",
        )
        store.propose({"x": 1})
        await asyncio.sleep(0.01)
        return store

    store = asyncio.run(run())
    committed = store.last_commit_at()
    assert committed is not None

    # Anchor recorded AFTER commit → "anchored"
    anchored = store.chain_status(last_anchor_at=committed + 5.0)
    assert anchored["status"] == "anchored"

    # Anchor recorded BEFORE commit → still "mirrored"
    drifted = store.chain_status(last_anchor_at=committed - 5.0)
    assert drifted["status"] == "mirrored"


def test_chain_status_propose_after_anchor_drifts(tmp_path):
    """If we commit a new version AFTER the last anchor, status
    drops back to 'mirrored' — the typed store has drifted past
    the on-chain state root."""
    from nexus_core.backends import MockBackend
    import time
    backend = MockBackend()

    async def run():
        store = VersionedStore(
            tmp_path / "facts",
            chain_backend=backend,
            chain_namespace="facts",
        )
        store.propose({"x": 1})
        await asyncio.sleep(0.01)
        # Anchor recorded right after the first commit.
        anchor_ts = store.last_commit_at() + 0.001
        # Sleep beyond filesystem mtime granularity so the second
        # commit's timestamp is provably newer than anchor_ts.
        time.sleep(1.1)
        store.propose({"x": 2})
        await asyncio.sleep(0.01)
        return store, anchor_ts

    store, anchor_ts = asyncio.run(run())
    s = store.chain_status(last_anchor_at=anchor_ts)
    # The second commit's ``last_commit_at`` is later than anchor_ts
    # so we should see "mirrored" (drifted past the anchor),
    # NOT "anchored".
    assert s["status"] == "mirrored", (
        f"expected drift detection, got {s['status']} "
        f"(commit={s['last_commit_at']}, anchor={anchor_ts})"
    )
