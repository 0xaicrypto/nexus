"""
ChainBackend — BSC-anchored storage for production.

Stores data in a local content-addressed cache and anchors content
hashes on BSC (BNB Smart Chain) for verifiability.

Storage model:
  - Data plane: local file cache (synchronous, durable on return).
    A remote S3-compatible mirror will complement it in a future task.
  - Anchor plane: SHA-256 content hashes anchored on BSC via
    AgentStateExtension (fire-and-forget, never blocks chat).

Requires: web3, eth_account (pip install web3)
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from ..core.backend import StorageBackend

logger = logging.getLogger("nexus_core.backend.chain")


class ChainBackend(StorageBackend):
    """
    Production storage backend: local data cache + BSC anchoring.

    - JSON/blob data  → local content-addressed cache (NEXUS_CACHE_DIR)
    - Content hashes  → BSC (on-chain anchoring for verifiability)
    """

    def __init__(
        self,
        private_key: str,
        network: str = "testnet",
        rpc_url: Optional[str] = None,
        agent_state_address: Optional[str] = None,
        task_manager_address: Optional[str] = None,
        identity_registry_address: Optional[str] = None,
    ):
        import os

        self._network = network
        self._private_key = private_key

        # Resolve config from env if not provided
        net_prefix = "MAINNET" if "mainnet" in network else "TESTNET"
        self._rpc_url = (
            rpc_url
            or os.environ.get(f"NEXUS_{net_prefix}_RPC")
            or os.environ.get("NEXUS_BSC_RPC")
        )
        self._agent_state_address = (
            agent_state_address
            or os.environ.get(f"NEXUS_{net_prefix}_AGENT_STATE_ADDRESS")
            or os.environ.get("NEXUS_AGENT_STATE_ADDRESS")
        )
        self._identity_registry_address = (
            identity_registry_address
            or os.environ.get(f"NEXUS_{net_prefix}_IDENTITY_REGISTRY_ADDRESS")
            or os.environ.get(f"NEXUS_{net_prefix}_IDENTITY_REGISTRY")
            or os.environ.get("NEXUS_IDENTITY_REGISTRY_ADDRESS")
            or os.environ.get("NEXUS_IDENTITY_REGISTRY")
        )
        self._task_manager_address = (
            task_manager_address
            or os.environ.get(f"NEXUS_{net_prefix}_TASK_MANAGER_ADDRESS")
            or os.environ.get("NEXUS_TASK_MANAGER_ADDRESS")
        )

        # Initialize chain client (optional — not all operations need it)
        self._chain_client = None
        if self._rpc_url and self._agent_state_address:
            try:
                from ..chain import BSCClient
                self._chain_client = BSCClient(
                    rpc_url=self._rpc_url,
                    private_key=private_key,
                    agent_state_address=self._agent_state_address,
                    task_manager_address=self._task_manager_address,
                    identity_registry_address=self._identity_registry_address,
                    network=network,
                )
            except ImportError:
                logger.warning("web3 not installed, chain anchoring disabled")

        # Local fallback for anchor operations when chain client unavailable
        self._local_anchors: dict[str, dict[str, str]] = {}
        # Phase D 续 — Brain panel chain status: timestamp of the most
        # recent successful state-root anchor per agent. Used to compare
        # against a namespace's ``last_commit_at`` so the UI can tell
        # whether a typed-store version is "anchored" or "drifted past
        # last anchor".
        self._last_anchor_at: dict[str, float] = {}

        # Track agents that failed on-chain: agent_id -> (skip_until_ts, backoff_seconds)
        self._anchor_skip_until: dict[str, float] = {}
        self._anchor_backoff: dict[str, float] = {}  # agent_id -> current backoff seconds

        # Map string agent_id → actual on-chain agentId (may differ for ERC-8004 register())
        self._agent_id_map: dict[str, int] = {}

        # ── Local data store ─────────────────────────────────────────
        # The data plane. Writes land here synchronously; once
        # store_json/store_blob returns, the data is durable on disk.
        cache_base = os.environ.get("NEXUS_CACHE_DIR", ".rune_cache")
        self._cache_dir = Path(cache_base)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # Track background tasks (BSC anchors) for graceful shutdown
        self._pending_tasks: set[asyncio.Task] = set()

        logger.info("ChainBackend initialized: network=%s, cache=%s", network, self._cache_dir)

    # ── Local cache helpers ───────────────────────────────────────────

    def _cache_path(self, path: str) -> Path:
        """Convert a storage path to a local cache file path."""
        safe = path.replace("/", "__").replace("\\", "__")
        return self._cache_dir / safe

    def _cache_write(self, path: str, data: bytes) -> None:
        """Write data to local cache."""
        try:
            self._cache_path(path).write_bytes(data)
        except OSError as e:
            # Covers disk full (ENOSPC), permission denied, etc.
            logger.warning("Cache write failed for %s: %s", path, e)
        except Exception as e:
            logger.debug("Cache write failed for %s: %s", path, e)

    def _cache_read(self, path: str) -> Optional[bytes]:
        """Read data from local cache. Returns None on miss."""
        try:
            cp = self._cache_path(path)
            if cp.exists():
                return cp.read_bytes()
        except Exception as e:
            logger.debug("Cache read failed for %s: %s", path, e)
        return None

    # ── Brain panel chain status (Phase D 续) ─────────────────────

    def is_path_mirrored(self, path: str) -> bool:
        """Has the blob at ``path`` been persisted by this backend?

        Writes are synchronous to the local data store, so this is
        simply "does the cache file exist".
        """
        try:
            return self._cache_path(path).exists()
        except Exception:
            return False

    def last_anchor_at(self, agent_id: str) -> Optional[float]:
        """POSIX timestamp of the most recent successful BSC
        ``updateStateRoot`` for this agent, or ``None`` if no
        successful anchor has been recorded this process lifetime.
        Used by the Brain panel to decide whether each typed-store
        namespace is still anchored or has drifted past the last
        anchor.
        """
        ts = self._last_anchor_at.get(agent_id)
        return float(ts) if ts is not None else None

    # How long a BSC anchor failure keeps influencing the health
    # snapshot. BSC's anchor cadence is slow (anchors fire on commit,
    # not on every write), so we keep a generous window — a single
    # failed anchor IS worth flagging for a while since the next
    # anchor attempt might be minutes away.
    _BSC_FAILURE_STALE_AFTER = 120.0

    def _bsc_anchor_failure_active(self) -> bool:
        """True iff a BSC anchor failed inside the staleness window.

        Without this check, the desktop's bsc_ready dot stayed solid
        green even when every anchor was reverting (RPC down, nonce
        stuck, gas exhausted) — a silent-failure bug.
        """
        last = getattr(self, "_last_bsc_anchor_failure_at", None)
        if last is None:
            return False
        return (time.time() - last) < self._BSC_FAILURE_STALE_AFTER

    def chain_health_snapshot(self) -> dict:
        """Compact summary for the Brain panel's Chain Health card.

        Returns::

            {
              "bsc_ready": True,
              "bsc_failure_active": False,
              "last_bsc_anchor_error": None | {agent_id, content_hash, error, at},
            }

        ``bsc_ready`` flips False on a recent anchor failure so the
        desktop's dot reflects reality instead of merely "a chain
        client was constructed".
        """
        bsc_failure_active = self._bsc_anchor_failure_active()
        return {
            "bsc_ready": (
                self._chain_client is not None and not bsc_failure_active
            ),
            "bsc_failure_active": bsc_failure_active,
            "last_bsc_anchor_error": getattr(self, "_last_bsc_anchor_error", None),
        }

    # ── Background task management ─────────────────────────────────

    def _fire_and_forget(self, coro, label: str = "background") -> None:
        """Launch an async coroutine as a tracked fire-and-forget task.

        Tasks are tracked so close() can gracefully cancel them on shutdown.
        CancelledError is swallowed so shutdown never crashes.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("No event loop for %s, skipping", label)
            return

        async def _wrapped():
            try:
                await coro
            except asyncio.CancelledError:
                logger.debug("[%s] Task cancelled (shutdown)", label)
            except Exception as e:
                logger.warning("[%s] Task failed: %s", label, e)

        task = loop.create_task(_wrapped())
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    # ── JSON ────────────────────────────────────────────────────────

    async def store_json(self, path: str, data: dict) -> str:
        raw = self.json_bytes(data)
        content_hash = self.content_hash(raw)
        self._cache_write(path, raw)
        return content_hash

    async def load_json(self, path: str) -> Optional[dict]:
        import json
        cached = self._cache_read(path)
        if cached is None:
            logger.debug("[READ][Cache] MISS %s", path)
            return None
        logger.debug("[READ][Cache] HIT %s (%d bytes)", path, len(cached))
        return json.loads(cached.decode("utf-8"))

    # ── Blobs ───────────────────────────────────────────────────────

    async def store_blob(self, path: str, data: bytes) -> str:
        content_hash = self.content_hash(data)
        self._cache_write(path, data)
        return content_hash

    async def load_blob(self, path: str) -> Optional[bytes]:
        cached = self._cache_read(path)
        if cached is not None:
            logger.debug("[READ][Cache] HIT blob %s (%d bytes)", path, len(cached))
        else:
            logger.debug("[READ][Cache] MISS blob %s", path)
        return cached

    # ── Anchoring ───────────────────────────────────────────────────

    @staticmethod
    def _agent_id_to_int(agent_id: str) -> int:
        """Convert a string agent_id to a deterministic uint256 for on-chain calls."""
        from ..utils import agent_id_to_int
        return agent_id_to_int(agent_id)

    async def anchor(self, agent_id: str, content_hash: str, namespace: str = "state") -> None:
        # Always store locally (instant, never blocks)
        self._local_anchors.setdefault(agent_id, {})[namespace] = content_hash

        # Skip on-chain anchor if this agent recently failed (exponential backoff)
        skip_until = self._anchor_skip_until.get(agent_id, 0)
        if skip_until > time.time():
            logger.debug("[BSC] Skipping anchor for %s (cooldown until %.0fs from now)", agent_id, skip_until - time.time())
            return
        elif agent_id in self._anchor_skip_until:
            del self._anchor_skip_until[agent_id]  # Cooldown expired, retry
            # Reset backoff on successful retry window
            self._anchor_backoff.pop(agent_id, None)

        if self._chain_client and namespace == "state":
            # Fire-and-forget: BSC chain calls run in background, never block chat
            self._fire_and_forget(
                self._anchor_on_chain(agent_id, content_hash),
                label=f"BSC-Anchor:{agent_id}",
            )

    def _next_backoff(self, agent_id: str) -> float:
        """Return next backoff delay and double it (capped at 300s)."""
        current = self._anchor_backoff.get(agent_id, 15)  # start at 15s
        self._anchor_backoff[agent_id] = min(current * 2, 300)
        return current

    async def _anchor_on_chain(self, agent_id: str, content_hash: str) -> None:
        """Background task: register + anchor state root on BSC. Never blocks chat."""
        try:
            root_bytes = bytes.fromhex(content_hash)
        except ValueError as e:
            logger.error("[BSC] Invalid content_hash format for %s: %s", agent_id, e)
            return
        if len(root_bytes) < 32:
            root_bytes = root_bytes.ljust(32, b"\x00")

        # Use cached on-chain ID if available (avoids re-registration on every anchor)
        if agent_id in self._agent_id_map:
            numeric_id = self._agent_id_map[agent_id]
            logger.debug("[BSC] Using cached on-chain ID %s for %s", numeric_id, agent_id)
        else:
            # First anchor for this agent — register in ERC-8004
            numeric_id = self._agent_id_to_int(agent_id)
            try:
                t0 = time.time()
                success, actual_id = self._chain_client.ensure_agent_registered(
                    numeric_id, agent_name=agent_id,
                )
                reg_elapsed = time.time() - t0
                if not success:
                    self._anchor_skip_until[agent_id] = time.time() + self._next_backoff(agent_id)
                    logger.warning(
                        "[WRITE][ERC-8004] Agent %s registration failed (%.2fs) — local fallback",
                        agent_id, reg_elapsed,
                    )
                    return
                numeric_id = actual_id
                self._agent_id_map[agent_id] = actual_id
                logger.info(
                    "[WRITE][ERC-8004] Agent %s → on-chain ID %s (%.2fs)",
                    agent_id, actual_id, reg_elapsed,
                )
            except Exception as e:
                self._anchor_skip_until[agent_id] = time.time() + 300
                logger.warning("[WRITE][ERC-8004] Registration check failed for %s: %s", agent_id, e)
                return

        try:
            t0 = time.time()
            tx_hash = self._chain_client.update_state_root(
                numeric_id, root_bytes, "0x" + "0" * 40,
            )
            anchor_elapsed = time.time() - t0
            logger.info(
                "[WRITE][BSC] Anchor OK: agent=%s hash=%s tx=%s (%.2fs)",
                agent_id, content_hash[:16], tx_hash[:16] if tx_hash else "?", anchor_elapsed,
            )
            # Phase D 续 — Brain panel: record anchor timestamp so
            # VersionedStore.chain_status can decide "anchored" vs
            # "drifted past last anchor".
            self._last_anchor_at[agent_id] = time.time()
            # Successful anchor clears any prior BSC failure marker, so
            # the desktop's bsc_ready dot returns to green automatically
            # when the chain recovers.
            self._last_bsc_anchor_failure_at = None
            self._last_bsc_anchor_error = None
        except Exception as e:
            self._anchor_skip_until[agent_id] = time.time() + 300
            # Structured failure log: twin_manager's _RE_BSC_FAIL picks
            # this up and records a `degraded` chain event row that the
            # desktop renders. Without this, BSC anchor failures were
            # silent — the UI showed the bsc dot solid green while every
            # anchor attempt was reverting.
            err_msg = str(e)[:300]
            logger.warning(
                "[FALLBACK][BSC] Anchor failed for %s — agent=%s hash=%s reason=%s",
                agent_id, agent_id, content_hash[:16], err_msg,
            )
            self._last_bsc_anchor_failure_at = time.time()
            self._last_bsc_anchor_error = {
                "agent_id": agent_id,
                "content_hash": content_hash[:16],
                "error": err_msg,
                "at": time.time(),
            }

    async def resolve(self, agent_id: str, namespace: str = "state") -> Optional[str]:
        # Check local cache first (instant) — avoids blocking on BSC RPC during startup
        local = self._local_anchors.get(agent_id, {}).get(namespace)
        if local is not None:
            logger.info("[READ][Cache] Resolve agent=%s hash=%s (local)", agent_id, local[:16])
            return local

        # Try chain with timeout so startup is never blocked
        if self._chain_client and namespace == "state":
            numeric_id = self._agent_id_map.get(agent_id, self._agent_id_to_int(agent_id))
            try:
                t0 = time.time()
                root = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, self._chain_client.resolve_state_root, numeric_id,
                    ),
                    timeout=10.0,
                )
                elapsed = time.time() - t0
                if root is not None:
                    root_hex = root.hex()
                    logger.info("[READ][BSC] Resolve agent=%s hash=%s (%.2fs)", agent_id, root_hex[:16], elapsed)
                    # Cache locally for next time
                    self._local_anchors.setdefault(agent_id, {})[namespace] = root_hex
                    return root_hex
                else:
                    logger.info("[READ][BSC] Resolve agent=%s → not found (%.2fs)", agent_id, elapsed)
            except asyncio.TimeoutError:
                logger.warning("[READ][BSC] Resolve timed out for %s (10s) — using local fallback", agent_id)
            except Exception as e:
                logger.warning(
                    "[READ][BSC] Resolve failed for %s (falling back to local): %s",
                    agent_id, e,
                )

        return local

    # ── Listing ─────────────────────────────────────────────────────

    async def list_paths(self, prefix: str) -> list[str]:
        """List stored paths under a prefix (local data store)."""
        paths: list[str] = []
        try:
            for p in self._cache_dir.iterdir():
                if not p.is_file():
                    continue
                logical = p.name.replace("__", "/")
                if logical.startswith(prefix):
                    paths.append(logical)
        except OSError as e:
            logger.debug("list_paths(%s) failed: %s", prefix, e)
        paths.sort()
        logger.debug("[READ][Cache] LIST %s → %d objects", prefix, len(paths))
        return paths

    # ── Session-scoped cleanup ──────────────────────────────────────

    async def delete_session_objects(self, session_id: str) -> dict:
        """Delete locally stored objects for a session.

        Returns a result dict shaped like::

            {"listed": N, "deleted": M, "note": "..."}

        Only the local data store is touched here — BSC state-root
        anchors are immutable on chain and stay.
        """
        if not session_id:
            return {"listed": 0, "deleted": 0,
                    "note": "Empty session_id — nothing to delete."}

        listed = 0
        deleted = 0
        try:
            for p in list(self._cache_dir.iterdir()):
                if not p.is_file():
                    continue
                if session_id in p.name:
                    listed += 1
                    try:
                        p.unlink()
                        deleted += 1
                    except OSError as e:
                        logger.debug("delete failed for %s: %s", p, e)
        except OSError as e:
            logger.debug("delete_session_objects scan failed: %s", e)

        return {
            "listed": listed,
            "deleted": deleted,
            "note": (
                "Local agent state for this session has been removed. "
                "BSC state-root anchors are immutable by design and stay "
                "on chain."
            ),
        }

    # ── Lifecycle ───────────────────────────────────────────────────

    async def close(self, grace_period: float = 30.0) -> None:
        """Graceful shutdown: wait for pending BSC anchors to finish.

        Args:
            grace_period: Max seconds to wait for pending background
                anchor tasks to finish before cancelling them.
        """
        if self._pending_tasks:
            n = len(self._pending_tasks)
            logger.info(
                "Waiting up to %.0fs for %d pending background task(s)...",
                grace_period, n,
            )
            done, pending = await asyncio.wait(
                self._pending_tasks, timeout=grace_period,
            )
            if done:
                logger.info("%d background task(s) completed successfully", len(done))
            if pending:
                logger.warning(
                    "%d task(s) still pending after %.0fs — cancelling",
                    len(pending), grace_period,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

            self._pending_tasks.clear()
