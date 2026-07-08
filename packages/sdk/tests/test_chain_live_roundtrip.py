"""End-to-end live round-trip against BNB Smart Chain testnet.

These tests are NOT mocked — they run a real ChainBackend against a
real BSC RPC. Their job is to catch the kind of bug that no
in-process stub can catch:

  * State-root contract correctness — a third party with the
    canonical bytes can recompute exactly what's on chain.

**Skipped by default.** Every test in this module gates on a full
set of env vars (private key + RPC + contract addresses). CI without
those env vars sees the whole module skipped, so this is opt-in for
hands-on / staging validation, not a blocker for the regular fast
test suite.

To run locally::

    export NEXUS_PRIVATE_KEY=0x...
    export NEXUS_TESTNET_RPC=https://data-seed-prebsc-1-s1.binance.org:8545
    export NEXUS_TESTNET_AGENT_STATE_ADDRESS=0x...
    export NEXUS_TESTNET_AGENT_ID=XXX               # ERC-8004 tokenId
    pytest packages/sdk/tests/test_chain_live_roundtrip.py -v
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid

import pytest


_REQUIRED_ENV = (
    "NEXUS_PRIVATE_KEY",
    "NEXUS_TESTNET_RPC",
    "NEXUS_TESTNET_AGENT_STATE_ADDRESS",
    "NEXUS_TESTNET_AGENT_ID",
)


def _missing() -> list[str]:
    return [k for k in _REQUIRED_ENV if not os.environ.get(k)]


pytestmark = pytest.mark.skipif(
    bool(_missing()),
    reason=(
        "live BSC round-trip requires "
        + ", ".join(_REQUIRED_ENV)
        + " in env (set "
        + ", ".join(_missing())
        + ")"
    ),
)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Per-test data directory so live tests don't collide with each
    other or with the developer's running server."""
    p = tmp_path / "live_cache"
    monkeypatch.setenv("NEXUS_CACHE_DIR", str(p))
    return p


@pytest.fixture
def backend(cache_dir):
    """Real ChainBackend pointed at testnet. No stub — every anchor
    genuinely lands on BSC."""
    from nexus_core.backends.chain import ChainBackend

    be = ChainBackend(
        private_key=os.environ["NEXUS_PRIVATE_KEY"],
        network="testnet",
    )
    yield be
    try:
        asyncio.get_event_loop().run_until_complete(be.close(grace_period=15))
    except Exception:
        pass


# ── State-root ↔ on-chain anchor round-trip ───────────────────────────


@pytest.mark.asyncio
async def test_state_root_anchor_round_trips_to_bsc(backend):
    """Compute a manifest's state-root locally, write it to BSC via
    the BSCClient, then read it back from the contract. Must match
    byte-for-byte. This is the verifiability contract third-party
    auditors rely on."""
    from nexus_core.anchor import build_anchor_batch

    if backend._chain_client is None:
        pytest.skip("BSC chain client not configured (no RPC / contract address)")

    agent_id = int(os.environ["NEXUS_TESTNET_AGENT_ID"])

    # Build a small manifest from synthetic events. prev_root pulled
    # live so the chain stays consistent with what we anchor next.
    prev = backend._chain_client.resolve_state_root(agent_id)
    prev_hex = "0x" + (prev.hex() if prev else "0" * 64)

    events = [
        {
            "client_created_at": "2026-05-01T00:00:00Z",
            "event_type": "user_message",
            "content": f"live test {uuid.uuid4().hex[:6]}",
            "session_id": "live-test",
            "sync_id": int(time.time()),
            "server_received_at": "2026-05-01T00:00:00Z",
        },
    ]
    batch = build_anchor_batch(
        user_id=f"live-{uuid.uuid4().hex[:8]}",
        prev_root=prev_hex,
        events=events,
    )
    canonical = batch.canonicalize()
    state_root = hashlib.sha256(canonical).digest()

    # Write to BSC. The wallet behind NEXUS_PRIVATE_KEY MUST be the
    # currently active runtime for this agent_id, otherwise the tx
    # reverts.
    tx_hash = await asyncio.to_thread(
        backend._chain_client.update_state_root,
        agent_id, state_root, backend._chain_client.address,
    )
    assert tx_hash, "BSC tx hash should be returned by update_state_root"

    # Wait for the new value to be visible on chain. Polling avoids
    # tying the test to RPC-specific block intervals.
    for _ in range(45):
        on_chain = backend._chain_client.resolve_state_root(agent_id)
        if on_chain and on_chain == state_root:
            break
        await asyncio.sleep(1)

    on_chain = backend._chain_client.resolve_state_root(agent_id)
    assert on_chain == state_root, (
        f"On-chain state_root {on_chain.hex() if on_chain else None!r} "
        f"does not match locally-computed {state_root.hex()!r} after 45s"
    )
