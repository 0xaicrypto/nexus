"""
Rune Chain Client — web3.py interface to BSC smart contracts.

Wraps the three contract layers:
  1. ERC-8004 Identity Registry (read-only, deployed by BNBChain)
  2. AgentStateExtension.sol (Rune contract)
  3. TaskStateManager.sol   (Rune contract)

Usage:
    from nexus_core.chain import BSCClient

    client = BSCClient(
        rpc_url="https://data-seed-prebsc-1-s1.bnbchain.org:8545",
        private_key="0x...",
        agent_state_address="0x...",
        task_manager_address="0x...",
    )

    # Update state root on-chain
    tx = await client.update_state_root(agent_id=1, state_root=b"...", runtime="0x...")

    # Read task from chain
    task = await client.get_task(task_id_bytes32)
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

logger = logging.getLogger("nexus_core.chain")

# ── Contract addresses ───────────────────────────────────────────────

# ERC-8004 Identity Registry (deployed by BNBChain)
ERC8004_ADDRESSES = {
    "bsc_testnet": "0x8004A818BFB912233c491871b3d84c89A494BD9e",
    "bsc_mainnet": "0xfA09B3397fAC75424422C4D28b1729E3D4f659D7",
}

BSC_TESTNET_RPC = "https://data-seed-prebsc-1-s1.bnbchain.org:8545"
BSC_MAINNET_RPC = "https://bsc-dataseed1.bnbchain.org"

# Task status enum (mirrors Solidity)
TASK_STATUS = {0: "pending", 1: "running", 2: "completed", 3: "failed"}
TASK_STATUS_REV = {v: k for k, v in TASK_STATUS.items()}


def _load_abi(name: str) -> list:
    """Load ABI JSON from the abi/ directory."""
    abi_dir = Path(__file__).parent / "abi"
    with open(abi_dir / f"{name}.json", "r") as f:
        return json.load(f)


class BSCClient:
    """
    Web3.py client for Nexus contracts on BSC.

    Handles all on-chain reads and writes for:
      - Agent state roots (AgentStateExtension)
      - Task lifecycle (TaskStateManager)
      - Identity resolution (ERC-8004, read-only)
    """

    def __init__(
        self,
        rpc_url: str = BSC_TESTNET_RPC,
        private_key: Optional[str] = None,
        agent_state_address: Optional[str] = None,
        task_manager_address: Optional[str] = None,
        identity_registry_address: Optional[str] = None,
        network: str = "bsc_testnet",
        gas_price_gwei: float = 3.0,
    ):
        # ── Web3 connection ──────────────────────────────────────────
        self._rpc_url = rpc_url
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        # BSC is a PoA chain — inject middleware for extraData handling
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        if not self.w3.is_connected():
            logger.warning("Web3 not connected to %s", rpc_url)

        # ── Account ──────────────────────────────────────────────────
        self._private_key = private_key
        if private_key:
            self.account = self.w3.eth.account.from_key(private_key)
            self.address = self.account.address
            logger.info("Rune chain client: account %s", self.address)
        else:
            self.account = None
            self.address = None
            logger.info("Rune chain client: read-only mode (no private key)")

        self._gas_price = Web3.to_wei(gas_price_gwei, "gwei")
        self._network = network

        # ── Local nonce management ───────────────────────────────────
        # Avoids redundant get_transaction_count RPC calls.
        # Initialized lazily on first tx; incremented locally after each send.
        # Protected by _nonce_lock for thread safety (FlushBuffer timer
        # may fire on a background thread while the main thread sends txs).
        self._local_nonce: Optional[int] = None
        self._nonce_lock = threading.Lock()

        # ── Load ABIs ────────────────────────────────────────────────
        self._agent_state_abi = _load_abi("AgentStateExtension")
        self._task_manager_abi = _load_abi("TaskStateManager")
        self._identity_abi = _load_abi("IIdentityRegistry")

        # ── Contract instances ───────────────────────────────────────
        # Normalize network name: "testnet" → "bsc_testnet", "mainnet" → "bsc_mainnet"
        normalized_network = network
        if network == "testnet":
            normalized_network = "bsc_testnet"
        elif network == "mainnet":
            normalized_network = "bsc_mainnet"
        self._network = normalized_network

        # Identity Registry (ERC-8004, always available)
        id_addr = identity_registry_address or ERC8004_ADDRESSES.get(normalized_network)
        if id_addr:
            self.identity_registry = self.w3.eth.contract(
                address=Web3.to_checksum_address(id_addr),
                abi=self._identity_abi,
            )
            logger.info("ERC-8004 Identity Registry: %s", id_addr)
        else:
            self.identity_registry = None

        # AgentStateExtension (must be deployed by user)
        if agent_state_address:
            self.agent_state = self.w3.eth.contract(
                address=Web3.to_checksum_address(agent_state_address),
                abi=self._agent_state_abi,
            )
            logger.info("AgentStateExtension: %s", agent_state_address)
        else:
            self.agent_state = None
            logger.info("AgentStateExtension: not configured (set agent_state_address)")

        # TaskStateManager (must be deployed by user)
        if task_manager_address:
            self.task_manager = self.w3.eth.contract(
                address=Web3.to_checksum_address(task_manager_address),
                abi=self._task_manager_abi,
            )
            logger.info("TaskStateManager: %s", task_manager_address)
        else:
            self.task_manager = None
            logger.info("TaskStateManager: not configured (set task_manager_address)")

    # ── Transaction helpers ──────────────────────────────────────────

    # BSC block time is sub-second; poll aggressively for receipts.
    _RECEIPT_POLL_LATENCY = 0.1  # seconds between polls
    _RECEIPT_TIMEOUT = 60        # seconds before giving up (testnet can be slow)

    def _get_nonce(self) -> int:
        """
        Return the next nonce to use (thread-safe).

        First call fetches from chain (get_transaction_count with "pending").
        Subsequent calls use local counter — avoids one RPC round-trip per tx.
        """
        with self._nonce_lock:
            if self._local_nonce is None:
                self._local_nonce = self.w3.eth.get_transaction_count(
                    self.address, "pending"
                )
                logger.debug("Nonce initialized from chain: %d", self._local_nonce)
            nonce = self._local_nonce
            self._local_nonce += 1
            return nonce

    def sync_nonce(self) -> int:
        """
        Re-sync local nonce from on-chain state (thread-safe).

        Call this after external transactions (e.g. bnbagent SDK's register)
        to avoid nonce conflicts between different web3 instances sharing
        the same wallet.

        Returns:
            The refreshed nonce value.
        """
        with self._nonce_lock:
            self._local_nonce = self.w3.eth.get_transaction_count(
                self.address, "pending"
            )
            logger.info("Nonce re-synced from chain: %d", self._local_nonce)
            return self._local_nonce

    # Retries for transient RPC errors (connection drops, timeouts)
    _MAX_RETRIES = 3
    _RETRY_BACKOFF = 2  # seconds, multiplied by attempt number

    def _build_and_send_tx(self, fn) -> str:
        """
        Build, sign, and send a transaction. Returns tx hash hex.

        Uses local nonce management to avoid redundant RPC calls.
        Retries on transient RPC errors (connection drops, timeouts).
        If a nonce collision is detected, re-syncs and retries once.
        """
        if not self._private_key:
            raise RuntimeError("Cannot send transactions in read-only mode")

        last_error = None
        for attempt in range(self._MAX_RETRIES):
            try:
                return self._try_send_tx(fn)
            except Exception as e:
                last_error = e
                err_msg = str(e).lower()
                # Transient connection errors — retry with backoff
                is_transient = any(kw in err_msg for kw in [
                    "connection aborted", "remotedisconnected", "connection reset",
                    "timeout", "timed out", "eof occurred", "connection refused",
                ])
                if is_transient and attempt < self._MAX_RETRIES - 1:
                    wait = self._RETRY_BACKOFF * (attempt + 1)
                    logger.warning("RPC error (attempt %d/%d), retrying in %ds: %s",
                                   attempt + 1, self._MAX_RETRIES, wait, e)
                    # Reset nonce — connection drop may have left state uncertain
                    self._local_nonce = None
                    time.sleep(wait)
                    continue
                raise
        raise last_error  # unreachable, but satisfies type checker

    def _dry_run(self, fn) -> None:
        """Simulate the transaction via eth_call. Raises on revert with decoded reason."""
        try:
            fn.call({"from": self.address})
        except Exception as e:
            err_msg = str(e)
            # Try to extract a human-readable revert reason
            reason = self._decode_revert_reason(err_msg)
            raise RuntimeError(
                f"Dry-run reverted: {reason}"
            ) from e

    @staticmethod
    def _decode_revert_reason(err_msg: str) -> str:
        """Extract human-readable reason from a web3 revert error."""
        # Common custom errors from our contracts
        if "AgentNotRegistered" in err_msg:
            return "Agent not registered in ERC-8004 Identity Registry"
        if "NotAgentOwner" in err_msg:
            return "Caller is not the owner of this agent"
        if "TaskNotFound" in err_msg:
            return "Task not found on-chain"
        # Standard revert reason string
        if "revert" in err_msg.lower():
            return err_msg
        return f"Unknown revert: {err_msg[:200]}"

    def _try_send_tx(self, fn) -> str:
        """Single attempt: dry-run first, then build, sign, send, and wait for receipt."""
        # Dry-run to catch reverts without wasting gas or time
        self._dry_run(fn)

        nonce = self._get_nonce()

        try:
            tx_hash = self._sign_and_send(fn, nonce)
        except Exception as e:
            err_msg = str(e).lower()
            # Nonce collision: another tx with this nonce already exists
            if "nonce" in err_msg or "already known" in err_msg or "replacement" in err_msg:
                logger.warning("Nonce %d collision, re-syncing: %s", nonce, e)
                self._local_nonce = None
                nonce = self._get_nonce()
                tx_hash = self._sign_and_send(fn, nonce)
            else:
                raise

        receipt = self.w3.eth.wait_for_transaction_receipt(
            tx_hash,
            timeout=self._RECEIPT_TIMEOUT,
            poll_latency=self._RECEIPT_POLL_LATENCY,
        )

        if receipt.status != 1:
            raise RuntimeError(
                f"Transaction reverted: {tx_hash.hex()} "
                f"(gas used: {receipt.gasUsed})"
            )

        logger.info("TX confirmed: %s (gas: %d, block: %d)",
                     tx_hash.hex(), receipt.gasUsed, receipt.blockNumber)
        return tx_hash.hex()

    def _sign_and_send(self, fn, nonce: int) -> bytes:
        """Build, sign, and broadcast a transaction. Returns raw tx hash."""
        tx = fn.build_transaction({
            "from": self.address,
            "nonce": nonce,
            "gas": 200_000,
            "gasPrice": self._gas_price,
        })
        signed = self.w3.eth.account.sign_transaction(tx, self._private_key)
        return self.w3.eth.send_raw_transaction(signed.raw_transaction)

    # ══════════════════════════════════════════════════════════════════
    # ERC-8004 Identity Registry
    # ══════════════════════════════════════════════════════════════════

    def agent_exists(self, agent_id: int) -> bool:
        """Check if agent exists in ERC-8004 registry.

        The real ERC-8004 contract does not expose an exists() function.
        We check by calling ownerOf() — if the token doesn't exist, it reverts.
        """
        if not self.identity_registry:
            raise RuntimeError("Identity registry not configured")
        try:
            self.identity_registry.functions.ownerOf(agent_id).call()
            return True
        except Exception:
            return False

    def register_agent(self, agent_name: str = "") -> int:
        """Register a new agent in the ERC-8004 Identity Registry.

        Calls the real ERC-8004 `register(agentURI)` function.
        The contract auto-assigns an agentId (returned from the event).

        Args:
            agent_name: A name/URI for the agent (used as agentURI).
                        Can be empty — the contract accepts register() with no args.

        Returns:
            The assigned agentId (uint256).
        """
        if not self.identity_registry:
            raise RuntimeError("Identity registry not configured")

        if agent_name:
            fn = self.identity_registry.functions.register(agent_name)
        else:
            fn = self.identity_registry.functions.register()

        tx_hash_hex = self._build_and_send_tx(fn)

        # Extract agentId from the Registered event in the receipt
        receipt = self.w3.eth.get_transaction_receipt(tx_hash_hex)
        registered_event = self.identity_registry.events.Registered()
        for log in receipt.logs:
            try:
                event_data = registered_event.process_log(log)
                agent_id = event_data["args"]["agentId"]
                logger.info(
                    "Agent registered in ERC-8004: agentId=%s, tx=%s",
                    agent_id, tx_hash_hex,
                )
                return agent_id
            except Exception:
                continue

        # Fallback: if event parsing fails, try to get agentId from balanceOf
        logger.warning(
            "Could not parse Registered event from tx %s, "
            "falling back to balanceOf-based lookup.",
            tx_hash_hex,
        )
        raise RuntimeError(
            f"Agent registered (tx={tx_hash_hex}) but could not extract agentId from event."
        )

    def mint_agent(self, agent_id: int) -> str:
        """Mint an agent with a specific ID (MockIdentityRegistry only).

        The real ERC-8004 does NOT have a public mint() function.
        This only works with MockIdentityRegistry deployed for testing.

        Args:
            agent_id: The tokenId to mint.

        Returns:
            Transaction hash.
        """
        if not self.identity_registry:
            raise RuntimeError("Identity registry not configured")
        fn = self.identity_registry.functions.mint(self.address, agent_id)
        return self._build_and_send_tx(fn)

    def ensure_agent_registered(self, agent_id: int, agent_name: str = "") -> tuple[bool, int]:
        """Check if agent exists; if not, register it via ERC-8004.

        Tries the real ERC-8004 `register()` first. Falls back to
        MockIdentityRegistry `mint()` if register() fails.

        Returns:
            (success: bool, actual_agent_id: int)
            - If already exists: (True, agent_id)
            - If newly registered: (True, new_agent_id) — note: may differ from input!
            - If failed: (False, agent_id)
        """
        if self.agent_exists(agent_id):
            return True, agent_id

        # Try real ERC-8004 register() first
        try:
            uri = agent_name or f"nexus-agent-{agent_id}"
            new_id = self.register_agent(uri)
            logger.info(
                "Agent registered via ERC-8004 register(): requested=%s, assigned=%s",
                agent_id, new_id,
            )
            return True, new_id
        except Exception as e:
            logger.debug("ERC-8004 register() failed: %s — trying mint()", e)

        # Fallback: try MockIdentityRegistry mint()
        try:
            tx_hash = self.mint_agent(agent_id)
            logger.info("Agent %s minted via MockIdentityRegistry: %s", agent_id, tx_hash)
            return True, agent_id
        except Exception as e:
            logger.warning(
                "Cannot auto-register agent %s: both register() and mint() failed. "
                "Last error: %s",
                agent_id, e,
            )
            return False, agent_id

    def agent_owner(self, agent_id: int) -> str:
        """Get owner address for agent from ERC-8004."""
        if not self.identity_registry:
            raise RuntimeError("Identity registry not configured")
        return self.identity_registry.functions.ownerOf(agent_id).call()

    def agent_wallet(self, agent_id: int) -> str:
        """Get agent wallet address from ERC-8004."""
        if not self.identity_registry:
            raise RuntimeError("Identity registry not configured")
        return self.identity_registry.functions.getAgentWallet(agent_id).call()

    def agent_uri(self, agent_id: int) -> str:
        """Get agent URI (tokenURI) from ERC-8004."""
        if not self.identity_registry:
            raise RuntimeError("Identity registry not configured")
        return self.identity_registry.functions.tokenURI(agent_id).call()

    # ══════════════════════════════════════════════════════════════════
    # AgentStateExtension
    # ══════════════════════════════════════════════════════════════════

    def update_state_root(
        self, agent_id: int, state_root: bytes, runtime: str
    ) -> str:
        """
        Update state root on-chain.

        Args:
            agent_id: ERC-8004 tokenId
            state_root: 32-byte SHA-256 hash (content hash of stored payload)
            runtime: Address of the currently executing runtime

        Returns:
            Transaction hash
        """
        if not self.agent_state:
            raise RuntimeError("AgentStateExtension not configured")

        # Ensure state_root is exactly 32 bytes
        if isinstance(state_root, str):
            state_root = bytes.fromhex(state_root.replace("0x", ""))
        if len(state_root) != 32:
            raise ValueError(f"state_root must be 32 bytes, got {len(state_root)}")

        fn = self.agent_state.functions.updateStateRoot(
            agent_id,
            state_root,
            Web3.to_checksum_address(runtime),
        )
        return self._build_and_send_tx(fn)

    def set_active_runtime(self, agent_id: int, runtime: str) -> str:
        """Change active runtime without updating state root."""
        if not self.agent_state:
            raise RuntimeError("AgentStateExtension not configured")
        fn = self.agent_state.functions.setActiveRuntime(
            agent_id, Web3.to_checksum_address(runtime)
        )
        return self._build_and_send_tx(fn)

    def resolve_state_root(self, agent_id: int) -> Optional[bytes]:
        """
        Read current state root from chain.

        Returns:
            32-byte state root, or None if agent has no state.
        """
        if not self.agent_state:
            raise RuntimeError("AgentStateExtension not configured")
        root = self.agent_state.functions.resolveStateRoot(agent_id).call()
        if root == b"\x00" * 32:
            return None
        return root

    def get_agent_state(self, agent_id: int) -> Tuple[bytes, str, int]:
        """
        Read full agent state from chain.

        Returns:
            (state_root: bytes32, active_runtime: address, updated_at: uint256)
        """
        if not self.agent_state:
            raise RuntimeError("AgentStateExtension not configured")
        return self.agent_state.functions.getAgentState(agent_id).call()

    def has_state(self, agent_id: int) -> bool:
        """Check if agent has any persisted state on-chain."""
        if not self.agent_state:
            raise RuntimeError("AgentStateExtension not configured")
        return self.agent_state.functions.hasState(agent_id).call()

    # ══════════════════════════════════════════════════════════════════
    # TaskStateManager
    # ══════════════════════════════════════════════════════════════════

    def create_task(self, task_id: bytes, agent_id: int) -> str:
        """
        Create a new task on-chain.

        Args:
            task_id: 32-byte task identifier
            agent_id: ERC-8004 tokenId of owning agent

        Returns:
            Transaction hash
        """
        if not self.task_manager:
            raise RuntimeError("TaskStateManager not configured")
        if isinstance(task_id, str):
            task_id = bytes.fromhex(task_id.replace("0x", ""))
        fn = self.task_manager.functions.createTask(task_id, agent_id)
        return self._build_and_send_tx(fn)

    def update_task(
        self,
        task_id: bytes,
        state_hash: bytes,
        status: str,
        expected_version: int,
    ) -> str:
        """
        Update task state with optimistic concurrency.

        Args:
            task_id: 32-byte task identifier
            state_hash: 32-byte content hash of the stored payload
            status: "pending" | "running" | "completed" | "failed"
            expected_version: Must match current on-chain version

        Returns:
            Transaction hash

        Raises:
            RuntimeError if version conflict (another runtime wrote first)
        """
        if not self.task_manager:
            raise RuntimeError("TaskStateManager not configured")

        if isinstance(task_id, str):
            task_id = bytes.fromhex(task_id.replace("0x", ""))
        if isinstance(state_hash, str):
            state_hash = bytes.fromhex(state_hash.replace("0x", ""))

        status_int = TASK_STATUS_REV.get(status)
        if status_int is None:
            raise ValueError(f"Invalid status: {status}. Use: {list(TASK_STATUS_REV)}")

        fn = self.task_manager.functions.updateTask(
            task_id, state_hash, status_int, expected_version
        )
        return self._build_and_send_tx(fn)

    def get_task(self, task_id: bytes) -> Optional[dict]:
        """
        Read task from chain.

        Returns:
            dict with keys: agent_id, state_hash, version, status, updated_at
            or None if task doesn't exist.
        """
        if not self.task_manager:
            raise RuntimeError("TaskStateManager not configured")

        if isinstance(task_id, str):
            task_id = bytes.fromhex(task_id.replace("0x", ""))

        if not self.task_manager.functions.taskExists(task_id).call():
            return None

        result = self.task_manager.functions.getTask(task_id).call()
        return {
            "agent_id": result[0],
            "state_hash": result[1],
            "version": result[2],
            "status": TASK_STATUS.get(result[3], "unknown"),
            "updated_at": result[4],
        }

    def get_agent_task_ids(self, agent_id: int) -> list:
        """Get all task IDs belonging to an agent."""
        if not self.task_manager:
            raise RuntimeError("TaskStateManager not configured")
        return self.task_manager.functions.getAgentTaskIds(agent_id).call()

    def get_agent_task_count(self, agent_id: int) -> int:
        """Get task count for an agent."""
        if not self.task_manager:
            raise RuntimeError("TaskStateManager not configured")
        return self.task_manager.functions.getAgentTaskCount(agent_id).call()

    def task_exists(self, task_id: bytes) -> bool:
        """Check if task exists on-chain."""
        if not self.task_manager:
            raise RuntimeError("TaskStateManager not configured")
        if isinstance(task_id, str):
            task_id = bytes.fromhex(task_id.replace("0x", ""))
        return self.task_manager.functions.taskExists(task_id).call()

    # ── Diagnostics ──────────────────────────────────────────────────

    def connection_info(self) -> dict:
        """Get connection status and contract info."""
        return {
            "connected": self.w3.is_connected(),
            "rpc_url": self._rpc_url,
            "network": self._network,
            "chain_id": self.w3.eth.chain_id if self.w3.is_connected() else None,
            "account": self.address,
            "balance_bnb": (
                float(Web3.from_wei(
                    self.w3.eth.get_balance(self.address), "ether"
                ))
                if self.address and self.w3.is_connected()
                else None
            ),
            "identity_registry": (
                self.identity_registry.address if self.identity_registry else None
            ),
            "agent_state_extension": (
                self.agent_state.address if self.agent_state else None
            ),
            "task_state_manager": (
                self.task_manager.address if self.task_manager else None
            ),
        }
