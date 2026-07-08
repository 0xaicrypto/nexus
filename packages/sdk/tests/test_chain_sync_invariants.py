"""Chain backend storage invariants.

These tests validate the contract that the chain backend must keep:

  Invariant 1: every successful caller-visible write is durably
  recorded in the local data store before the call returns.

  Invariant 2: the content hash of a payload is path-independent
  and write-path-independent — the same bytes always produce the
  same SHA-256 (so anchor manifests are reproducible by third
  parties).

  Invariant 3: reads-your-writes — once ``store_json`` returns,
  ``load_json`` for that path returns the same data.

  Invariant 4: state-root for a manifest is reproducible — a
  third party with the canonical bytes can recompute the same
  hash that was anchored on BSC.

Each test instantiates a real ``ChainBackend``. The BSC client side
stays ``None`` (chain anchoring is a separate concern, exercised by
``test_anchor.py``).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from nexus_core.backends.chain import ChainBackend


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def chain_dir(tmp_path, monkeypatch):
    """Per-test data directory. Pinned via NEXUS_CACHE_DIR so
    ChainBackend's __init__ honours it."""
    cache = tmp_path / "cache"
    monkeypatch.setenv("NEXUS_CACHE_DIR", str(cache))
    return cache


@pytest.fixture
def backend(chain_dir, monkeypatch):
    """A real ChainBackend with no BSC client.

    RPC / contract addresses are unset so _chain_client stays None.
    """
    for var in (
        "NEXUS_TESTNET_RPC", "NEXUS_BSC_RPC",
        "NEXUS_TESTNET_AGENT_STATE_ADDRESS", "NEXUS_AGENT_STATE_ADDRESS",
    ):
        monkeypatch.delenv(var, raising=False)

    return ChainBackend(
        private_key="0x" + "a" * 64,        # dummy — never used because BSC client is None
        network="testnet",
    )


# ── Invariant 1 — successful write goes durable through the store ────


@pytest.mark.asyncio
async def test_store_json_writes_local_store_synchronously(backend, chain_dir):
    """``store_json`` must populate the local data store before
    returning, so a crash *immediately* after the call still
    preserves the data."""
    chash = await backend.store_json("memory/index.json", {"a": 1, "b": "two"})
    assert chash, "content hash should be returned"

    # Store file exists with the canonical JSON bytes.
    cache_path = backend._cache_path("memory/index.json")
    assert cache_path.exists()
    raw = cache_path.read_bytes()
    assert json.loads(raw.decode()) == {"a": 1, "b": "two"}


@pytest.mark.asyncio
async def test_store_blob_round_trips(backend):
    """Raw bytes written via ``store_blob`` come back byte-identical
    via ``load_blob``."""
    raw = b"%PDF-1.4 dummy uploaded paper bytes"
    chash = await backend.store_blob("files/abc/paper.pdf", raw)
    assert chash == hashlib.sha256(raw).hexdigest()
    assert await backend.load_blob("files/abc/paper.pdf") == raw


# ── Invariant 2 — content hash is deterministic ──────────────────────


@pytest.mark.asyncio
async def test_content_hash_is_deterministic_across_paths(backend):
    """Same JSON payload at two different paths → same content_hash.
    The hash commits to the bytes, not the path; this is what makes
    dedup + manifest verifiability work."""
    h1 = await backend.store_json("memory/a.json", {"k": [1, 2, 3]})
    h2 = await backend.store_json("memory/b.json", {"k": [1, 2, 3]})
    assert h1 == h2 == hashlib.sha256(
        backend.json_bytes({"k": [1, 2, 3]})
    ).hexdigest()


# ── Invariant 3 — read-your-writes ───────────────────────────────────


@pytest.mark.asyncio
async def test_load_json_returns_just_written_value(backend):
    """``load_json`` must serve the value written this session."""
    await backend.store_json("artifacts/persona.json", {"version": 3})
    got = await backend.load_json("artifacts/persona.json")
    assert got == {"version": 3}


@pytest.mark.asyncio
async def test_load_json_unknown_path_returns_none(backend):
    """A miss on an unknown path returns None, not an exception.

    Calling code (twin._initialize, evolution loaders, etc.) treats
    None as 'not yet exists'."""
    got = await backend.load_json("memory/never-written.json")
    assert got is None


@pytest.mark.asyncio
async def test_list_paths_reflects_written_prefixes(backend):
    """``list_paths`` enumerates stored objects under a prefix so
    recovery / migration code can discover prior writes."""
    await backend.store_json("namespaces/facts/v0001.json", {"v": 1})
    await backend.store_json("namespaces/facts/_current.json", {"version": "v0001"})
    await backend.store_json("namespaces/skills/v0001.json", {"v": 1})

    facts = await backend.list_paths("namespaces/facts/")
    assert sorted(facts) == [
        "namespaces/facts/_current.json",
        "namespaces/facts/v0001.json",
    ]


@pytest.mark.asyncio
async def test_is_path_mirrored_tracks_persistence(backend):
    """``is_path_mirrored`` is the probe VersionedStore.chain_status
    uses — True once the blob is durably stored, False otherwise."""
    assert backend.is_path_mirrored("namespaces/facts/v0042.json") is False
    await backend.store_json("namespaces/facts/v0042.json", {"v": 42})
    assert backend.is_path_mirrored("namespaces/facts/v0042.json") is True


# ── Invariant 4 — third-party state-root reproducibility ─────────────


def test_state_root_is_reproducible_from_canonical_bytes():
    """Anyone with the canonical manifest bytes can recompute the
    same SHA-256 we anchor on BSC. This is the verifiability
    contract third parties rely on to audit an agent's growth."""
    from nexus_core.anchor import (
        build_anchor_batch, SCHEMA_V1, ZERO_DIGEST_HEX,
    )

    events = [
        {"client_created_at": "2026-05-01T12:00:00Z",
         "event_type": "user_message", "content": "hi",
         "session_id": "s1", "sync_id": 1,
         "server_received_at": "2026-05-01T12:00:00Z"},
        {"client_created_at": "2026-05-01T12:00:01Z",
         "event_type": "assistant_response", "content": "hello",
         "session_id": "s1", "sync_id": 2,
         "server_received_at": "2026-05-01T12:00:01Z"},
    ]
    batch_a = build_anchor_batch(
        user_id="user-abc",
        prev_root="0x" + "0" * 64,
        events=events,
    )
    bytes_a = batch_a.canonicalize()
    root_a = "0x" + hashlib.sha256(bytes_a).hexdigest()

    # Third party rebuild: same events, same prev root, same agent →
    # byte-for-byte identical canonical form, identical SHA-256.
    batch_b = build_anchor_batch(
        user_id="user-abc",
        prev_root="0x" + "0" * 64,
        events=list(events),
    )
    bytes_b = batch_b.canonicalize()
    root_b = "0x" + hashlib.sha256(bytes_b).hexdigest()

    assert bytes_a == bytes_b, "canonical encoding must be byte-stable"
    assert root_a == root_b, "state root must be deterministic"
    assert batch_a.schema == SCHEMA_V1
    # Empty manifest sanity: never collides with the zero digest.
    assert root_a.replace("0x", "") != ZERO_DIGEST_HEX


def test_state_root_changes_with_a_single_byte_diff():
    """Tampering: any change to even one event byte must produce a
    different state root. This is the property third-party auditors
    rely on to detect manipulation of the agent's history."""
    from nexus_core.anchor import build_anchor_batch
    base = [
        {"client_created_at": "2026-05-01T12:00:00Z",
         "event_type": "user_message", "content": "hello world",
         "session_id": "s1", "sync_id": 1,
         "server_received_at": "2026-05-01T12:00:00Z"},
    ]
    tampered = [dict(base[0])]
    tampered[0]["content"] = "hello worle"  # one-byte change

    root_base = hashlib.sha256(
        build_anchor_batch(
            user_id="x", prev_root="0x" + "0" * 64, events=base,
        ).canonicalize()
    ).hexdigest()
    root_tampered = hashlib.sha256(
        build_anchor_batch(
            user_id="x", prev_root="0x" + "0" * 64, events=tampered,
        ).canonicalize()
    ).hexdigest()
    assert root_base != root_tampered


# ── Invariant 5 — content hash chain stops a forked history ──────────


def test_prev_state_root_chains_into_current_hash():
    """Each anchor commits to the previous state-root, so two forks
    that share an event prefix but differ on prev_root produce
    distinct current roots. Chain backend uses this to detect
    runtime hand-off / fork attempts."""
    from nexus_core.anchor import build_anchor_batch
    events = [
        {"client_created_at": "2026-05-01T12:00:00Z",
         "event_type": "user_message", "content": "same event",
         "session_id": "s1", "sync_id": 1,
         "server_received_at": "2026-05-01T12:00:00Z"},
    ]
    root_a = hashlib.sha256(
        build_anchor_batch(
            user_id="x",
            prev_root="0x" + "1" * 64,
            events=events,
        ).canonicalize()
    ).hexdigest()
    root_b = hashlib.sha256(
        build_anchor_batch(
            user_id="x",
            prev_root="0x" + "2" * 64,
            events=events,
        ).canonicalize()
    ).hexdigest()
    assert root_a != root_b
