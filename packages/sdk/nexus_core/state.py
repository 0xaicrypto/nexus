"""
StateManager — unified interface to Nexus's on-chain state layer.

Architecture mirrors three separate smart contracts on BSC:

  1. ERC-8004 Identity Registry (EXISTING, not ours)
     - Agent registration (ERC-721 tokenId = agentId)
     - agentURI → off-chain registration file
     - Reputation & Validation registries
     - Scope: identity + trust. Does NOT store task/session/state data.

  2. AgentStateExtension.sol (NEW, our contract)
     - Extends ERC-8004 by agentId (foreign key to ERC-721 tokenId)
     - state_root: bytes32 hash pointing to the off-chain payload
     - active_runtime: address of current executor
     - Scope: stateless agent checkpoint/resume infrastructure.

  3. TaskStateManager.sol (NEW, our contract)
     - task_id → state_hash mapping
     - version counter for optimistic concurrency
     - Scope: task lifecycle tracking.

Storage layer:
  - BSC (contracts above): lightweight metadata + hash pointers (~200-300 bytes/agent)
  - Local object store: full payloads (sessions, messages, artifacts, 1KB-10MB)
  - Linked by SHA-256 content hashes: BSC stores the hash, the object
    store keeps the data

Both modes expose the SAME API — zero changes to session.py, artifact.py,
a2a_adapter.py, or any demo code.
"""

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("nexus_core.state")


def _agent_id_to_int(agent_id: str) -> int:
    """Convert a string agent_id to a deterministic uint256 for on-chain calls."""
    from .utils import agent_id_to_int
    return agent_id_to_int(agent_id)


# ── Data Models (mirror on-chain structs) ────────────────────────────


@dataclass
class ERC8004Identity:
    """
    ERC-8004 Identity Registry record.

    In production this is an ERC-721 NFT. The tokenId IS the agentId.
    We do NOT own this contract — it's the standard ERC-8004 deployment.
    Our SDK reads from it to resolve agent ownership.

    Fields we care about:
      - agent_id:  ERC-721 tokenId (globally unique root key)
      - owner:     NFT owner address
      - agent_uri: pointer to off-chain registration file (name, services, A2A/MCP endpoints)
    """
    agent_id: str             # ERC-721 tokenId
    owner: str                # NFT owner address
    agent_uri: str = ""       # off-chain registration file URL
    created_at: float = 0.0


@dataclass
class AgentStateRecord:
    """
    AgentStateExtension.sol record (OUR contract).

    Extends ERC-8004 identity with stateless agent infrastructure.
    Uses agent_id as foreign key to ERC-8004 tokenId.

    On-chain footprint: ~84 bytes per agent.
      - state_root:      bytes32 (32B) — SHA-256 hash → off-chain payload
      - active_runtime:  address (20B) — which runtime currently holds execution
      - updated_at:      uint256 (32B) — block timestamp
    """
    agent_id: str              # FK to ERC-8004 tokenId
    agent_address: str = ""    # Deterministic on-chain address (from ERC-8004)
    state_root: str = ""       # SHA-256 hash pointing to stored payload
    memory_root: str = ""      # SHA-256 hash → memory index payload
    active_runtime: str = ""   # address of current executing runtime
    updated_at: float = 0.0


@dataclass
class TaskRecord:
    """
    TaskStateManager.sol record (OUR contract).

    Tracks task lifecycle as an on-chain state machine.
    Uses agent_id as FK to ERC-8004 tokenId.

    On-chain footprint: ~129 bytes per task update.
      - task_id:     bytes32 (32B)
      - agent_id:    bytes32 (32B) — FK to ERC-8004
      - state_hash:  bytes32 (32B) — SHA-256 hash → stored snapshot
      - version:     uint256 (32B) — monotonic counter for optimistic concurrency
      - status:      uint8   (1B)  — 0=pending, 1=running, 2=completed, 3=failed
    """
    task_id: str
    agent_id: str              # FK to ERC-8004 tokenId
    state_hash: str = ""
    version: int = 0
    status: str = "pending"    # pending | running | completed | failed
    created_at: float = 0.0
    updated_at: float = 0.0


# ── StateManager ─────────────────────────────────────────────────────


class StateManager:
    """
    Unified interface to Nexus state layer.

    Two modes:
      - "local": File-based mock for development/testing
      - "chain": Real web3.py against the deployed BSC contracts

    Mode is auto-detected based on constructor args:
      - StateManager()                           → local mode
      - StateManager(base_dir=".nexus_state")     → local mode at custom dir
      - StateManager(                            → chain mode
            rpc_url="https://...",
            private_key="0x...",
            agent_state_address="0x...",
            task_manager_address="0x...",
        )

    Bulk payloads are stored in a local content-addressed object store
    (``{base_dir}/data``) in both modes; only the hash pointers differ
    (local JSON files vs. on-chain contracts).

    The public API is identical in both modes — all code above this
    layer (session.py, artifact.py, a2a_adapter.py) works without changes.
    """

    def __init__(
        self,
        # Local mode
        base_dir: str = ".nexus_state",
        # Chain mode — if any of these are set, uses real chain
        rpc_url: Optional[str] = None,
        private_key: Optional[str] = None,
        agent_state_address: Optional[str] = None,
        task_manager_address: Optional[str] = None,
        identity_registry_address: Optional[str] = None,
        network: str = "bsc_testnet",
        # Explicit mode override: "local" forces file-based, "chain" forces on-chain,
        # None (default) auto-detects from constructor args + env vars.
        mode: Optional[str] = None,
    ):
        # Auto-detect mode from environment or constructor args.
        # Supports network-specific env vars: NEXUS_TESTNET_* / NEXUS_MAINNET_*
        # Falls back to NEXUS_BSC_RPC / NEXUS_AGENT_STATE_ADDRESS for compat.
        env_network = os.environ.get("NEXUS_NETWORK", "bsc-testnet")
        if "mainnet" in (network or env_network):
            net_prefix = "MAINNET"
        else:
            net_prefix = "TESTNET"

        rpc_url = (
            rpc_url
            or os.environ.get(f"NEXUS_{net_prefix}_RPC")
            or os.environ.get("NEXUS_BSC_RPC")
        )
        private_key = private_key or os.environ.get("NEXUS_PRIVATE_KEY")
        agent_state_address = (
            agent_state_address
            or os.environ.get(f"NEXUS_{net_prefix}_AGENT_STATE_ADDRESS")
            or os.environ.get("NEXUS_AGENT_STATE_ADDRESS")
        )
        task_manager_address = (
            task_manager_address
            or os.environ.get(f"NEXUS_{net_prefix}_TASK_MANAGER_ADDRESS")
            or os.environ.get("NEXUS_TASK_MANAGER_ADDRESS")
        )
        identity_registry_address = (
            identity_registry_address
            or os.environ.get(f"NEXUS_{net_prefix}_IDENTITY_REGISTRY")
        )

        # Determine mode: explicit override > auto-detect from args/env
        if mode == "local":
            use_chain = False
        elif mode == "chain":
            use_chain = True
        else:
            use_chain = bool(rpc_url and private_key and agent_state_address)

        if use_chain:
            self._mode = "chain"
            self._init_chain_mode(
                rpc_url=rpc_url,
                private_key=private_key,
                agent_state_address=agent_state_address,
                task_manager_address=task_manager_address,
                identity_registry_address=identity_registry_address,
                network=network,
            )
        else:
            self._mode = "local"
            self._init_local_mode(base_dir)

        # agent_id → agent_address mapping (for storage folder names)
        self._address_map: dict[str, str] = {}

        logger.info("StateManager initialized in %s mode", self._mode)

    # ── Initialization ───────────────────────────────────────────────

    def _init_local_mode(self, base_dir: str):
        """File-based mock of the three contracts."""
        self.base_dir = Path(base_dir)
        self.chain_dir = self.base_dir / "chain"
        self.data_dir = self.base_dir / "data"
        self.chain_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Three separate "contract" files
        self._erc8004_file = self.chain_dir / "erc8004.json"
        self._agent_state_file = self.chain_dir / "agent_state.json"
        self._tasks_file = self.chain_dir / "tasks.json"

        if not self._erc8004_file.exists():
            self._write_file(self._erc8004_file, {"agents": {}})
        if not self._agent_state_file.exists():
            self._write_file(self._agent_state_file, {"states": {}})
        if not self._tasks_file.exists():
            self._write_file(self._tasks_file, {"tasks": {}})

        self._chain_client = None

    def _init_chain_mode(
        self,
        rpc_url: str,
        private_key: str,
        agent_state_address: str,
        task_manager_address: Optional[str],
        identity_registry_address: Optional[str],
        network: str,
    ):
        """Real web3.py against the deployed BSC contracts."""
        from .chain import BSCClient

        self._chain_client = BSCClient(
            rpc_url=rpc_url,
            private_key=private_key,
            agent_state_address=agent_state_address,
            task_manager_address=task_manager_address,
            identity_registry_address=identity_registry_address,
            network=network,
        )

        # Bulk payloads live in the local object store in chain mode too.
        self.data_dir = Path(".nexus_state") / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ── File I/O helpers (local mode only) ───────────────────────────

    @staticmethod
    def _read_file(path: Path) -> dict:
        with open(path, "r") as f:
            return json.load(f)

    @staticmethod
    def _write_file(path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.rename(path)

    # Keep backward-compatible aliases used by session.py / artifact.py
    def _read_chain(self) -> dict:
        """Read the agent_state contract (used by session/artifact services)."""
        if self._mode == "local":
            return self._read_file(self._agent_state_file)
        # Chain mode: not used directly (services call specific methods)
        raise RuntimeError("_read_chain not available in chain mode — use specific API methods")

    def _write_chain(self, data: dict) -> None:
        """Write the agent_state contract (used by session/artifact services)."""
        if self._mode == "local":
            self._write_file(self._agent_state_file, data)
            return
        raise RuntimeError("_write_chain not available in chain mode — use specific API methods")

    @property
    def mode(self) -> str:
        """Current mode: 'local' or 'chain'."""
        return self._mode

    @property
    def chain_client(self):
        """Access the underlying BSCClient (chain mode only)."""
        return self._chain_client

    def sync_nonce(self) -> None:
        """
        Re-sync the local nonce counter from on-chain state.

        Call this after external transactions (e.g. bnbagent SDK's register_agent)
        that share the same wallet but use a different web3 instance, to prevent
        nonce conflicts.

        Safe to call in local mode (no-op).
        """
        if self._mode == "chain" and self._chain_client:
            self._chain_client.sync_nonce()

    # ══════════════════════════════════════════════════════════════════
    # Contract 1: ERC-8004 Identity Registry (read-only in production)
    # ══════════════════════════════════════════════════════════════════

    def register_identity(self, agent_id: str, owner: str,
                          agent_uri: str = "") -> ERC8004Identity:
        """
        Mint an ERC-8004 identity NFT.

        In production: this is a call to the deployed ERC-8004 contract's
        registerAgent(address agentAddress, string agentURI).
        Our SDK would typically READ from this, not write — the agent
        creator mints the NFT separately.
        """
        if self._mode == "chain":
            # In chain mode, ERC-8004 identities are minted externally.
            # We only verify they exist. Return a synthetic record.
            if self._chain_client.agent_exists(_agent_id_to_int(agent_id)):
                chain_owner = self._chain_client.agent_owner(_agent_id_to_int(agent_id))
                return ERC8004Identity(
                    agent_id=agent_id, owner=chain_owner,
                    agent_uri=agent_uri, created_at=time.time(),
                )
            raise RuntimeError(
                f"Agent {agent_id} not found in ERC-8004. "
                "Mint the identity NFT first using the ERC-8004 registry."
            )

        # Local mode
        store = self._read_file(self._erc8004_file)
        now = time.time()
        record = ERC8004Identity(
            agent_id=agent_id, owner=owner,
            agent_uri=agent_uri, created_at=now,
        )
        store["agents"][agent_id] = asdict(record)
        self._write_file(self._erc8004_file, store)
        print(f"  [ERC-8004] Identity minted: {agent_id} (owner={owner})")
        return record

    def get_identity(self, agent_id: str) -> Optional[ERC8004Identity]:
        """Read agent identity from ERC-8004 registry."""
        if self._mode == "chain":
            try:
                aid = _agent_id_to_int(agent_id)
                if not self._chain_client.agent_exists(aid):
                    return None
                owner = self._chain_client.agent_owner(aid)
                return ERC8004Identity(
                    agent_id=agent_id, owner=owner,
                    created_at=0.0,
                )
            except Exception as e:
                logger.warning("ERC-8004 lookup failed for %s: %s", agent_id, e)
                return None

        store = self._read_file(self._erc8004_file)
        data = store["agents"].get(agent_id)
        if data is None:
            return None
        return ERC8004Identity(**data)

    def verify_owner(self, agent_id: str, caller: str) -> bool:
        """Check if caller is the ERC-721 owner of this agentId."""
        identity = self.get_identity(agent_id)
        if identity is None:
            return False
        return identity.owner.lower() == caller.lower()

    # ══════════════════════════════════════════════════════════════════
    # Contract 2: AgentStateExtension.sol (OUR contract)
    # ══════════════════════════════════════════════════════════════════

    def register_agent(
        self, agent_id: str, owner: str,
        agent_address: str = "",
    ) -> AgentStateRecord:
        """
        Register an agent in our state extension + verify ERC-8004 identity.

        Args:
            agent_id: ERC-8004 tokenId.
            owner: Owner wallet address.
            agent_address: Deterministic on-chain agent address from ERC-8004.
                Used as the storage folder name (instead of tokenId).

        In production these are two separate transactions:
          1. Verify ERC-8004 NFT exists (or mint it externally)
          2. First updateStateRoot call registers the agent implicitly
        """
        if self._mode == "chain":
            # Verify ERC-8004 identity
            if not self._chain_client.agent_exists(_agent_id_to_int(agent_id)):
                raise RuntimeError(
                    f"Agent {agent_id} not found in ERC-8004. "
                    "Mint the identity NFT first."
                )
            record = AgentStateRecord(
                agent_id=agent_id,
                agent_address=agent_address,
                updated_at=time.time(),
            )
            # Cache address mapping
            if agent_address:
                self._address_map[agent_id] = agent_address
            return record

        # Local mode
        if self.get_identity(agent_id) is None:
            self.register_identity(agent_id, owner)

        store = self._read_file(self._agent_state_file)
        now = time.time()
        record = AgentStateRecord(
            agent_id=agent_id,
            agent_address=agent_address,
            updated_at=now,
        )
        store["states"][agent_id] = asdict(record)
        self._write_file(self._agent_state_file, store)
        if agent_address:
            self._address_map[agent_id] = agent_address
        print(f"  [AgentStateExtension] Registered: {agent_id}")
        return record

    def get_agent(self, agent_id: str) -> Optional[AgentStateRecord]:
        """Read agent state record from AgentStateExtension."""
        if self._mode == "chain":
            try:
                aid = _agent_id_to_int(agent_id)
                if not self._chain_client.has_state(aid):
                    return None
                state_root, runtime, updated = self._chain_client.get_agent_state(aid)
                addr = self._address_map.get(agent_id, "")
                return AgentStateRecord(
                    agent_id=agent_id,
                    agent_address=addr,
                    state_root=state_root.hex() if state_root != b"\x00" * 32 else "",
                    active_runtime=runtime,
                    updated_at=float(updated),
                )
            except Exception as e:
                logger.warning("get_agent failed for %s: %s", agent_id, e)
                return None

        store = self._read_file(self._agent_state_file)
        data = store["states"].get(agent_id)
        if data is None:
            return None
        record = AgentStateRecord(**data)
        # Populate cache from stored data
        if record.agent_address:
            self._address_map[agent_id] = record.agent_address
        return record

    def update_state_root(self, agent_id: str, state_root: str,
                          runtime_id: str = "") -> None:
        """
        Update the state root hash on AgentStateExtension.

        In production: AgentStateExtension.updateStateRoot(agentId, stateRoot, runtime)
        This is the critical write that links BSC to the stored payload.
        """
        if self._mode == "chain":
            # Convert hex string to bytes32
            root_bytes = bytes.fromhex(state_root) if state_root else b"\x00" * 32
            # Pad to 32 bytes if needed
            if len(root_bytes) < 32:
                root_bytes = root_bytes.ljust(32, b"\x00")

            # runtime_id in chain mode should be an address
            # If it's a name like "runtime-orch-A", derive a deterministic address
            if runtime_id and not runtime_id.startswith("0x"):
                runtime_addr = self._runtime_id_to_address(runtime_id)
            else:
                runtime_addr = runtime_id or "0x" + "0" * 40

            try:
                self._chain_client.update_state_root(
                    _agent_id_to_int(agent_id), root_bytes, runtime_addr,
                )
                logger.info("State root updated on-chain: %s -> %s", agent_id, state_root[:16])
            except Exception as e:
                logger.warning("BSC anchor failed for %s (agent may not be registered in ERC-8004): %s", agent_id, e)
            return

        # Local mode
        store = self._read_file(self._agent_state_file)
        if agent_id not in store["states"]:
            raise KeyError(f"Agent {agent_id} not registered in AgentStateExtension")
        store["states"][agent_id]["state_root"] = state_root
        store["states"][agent_id]["active_runtime"] = runtime_id
        store["states"][agent_id]["updated_at"] = time.time()
        self._write_file(self._agent_state_file, store)
        print(f"  [AgentStateExtension] State root updated: {agent_id} -> {state_root[:16]}...")

    def resolve_state_root(self, agent_id: str) -> Optional[str]:
        """
        Resolve current state root for an agent.

        In production: AgentStateExtension.resolveStateRoot(agentId) → bytes32
        """
        if self._mode == "chain":
            try:
                root = self._chain_client.resolve_state_root(_agent_id_to_int(agent_id))
                if root is not None:
                    return root.hex()
            except Exception as e:
                logger.warning("BSC resolve failed for %s: %s", agent_id, e)
            return None

        record = self.get_agent(agent_id)
        if record is None or not record.state_root:
            return None
        return record.state_root

    # ── Memory Root (extends AgentStateExtension) ─────────────────

    def update_memory_root(self, agent_id: str, memory_root: str,
                           runtime_id: str = "") -> None:
        """
        Update the memory root hash for an agent.

        Mirrors update_state_root but for the memory index hash.
        In production: AgentStateExtension.updateMemoryRoot(agentId, memoryRoot)
        """
        if self._mode == "chain":
            # Phase 2: call contract method once deployed
            # For now, piggyback on local-mode logic for testnet too
            logger.warning("memory_root chain write not yet implemented; storing locally")

        # Local mode (and chain-mode fallback)
        store = self._read_file(self._agent_state_file)
        if agent_id not in store["states"]:
            raise KeyError(f"Agent {agent_id} not registered in AgentStateExtension")
        store["states"][agent_id]["memory_root"] = memory_root
        store["states"][agent_id]["updated_at"] = time.time()
        self._write_file(self._agent_state_file, store)
        print(f"  [AgentStateExtension] Memory root updated: {agent_id} -> {memory_root[:16]}...")

    def resolve_memory_root(self, agent_id: str) -> Optional[str]:
        """
        Resolve the current memory root for an agent.

        In production: AgentStateExtension.memoryRoot(agentId) -> bytes32
        """
        if self._mode == "chain":
            # Phase 2: read from contract
            pass

        record = self.get_agent(agent_id)
        if record is None:
            return None
        # memory_root is stored alongside state_root in local mode
        store = self._read_file(self._agent_state_file)
        agent_data = store.get("states", {}).get(agent_id, {})
        return agent_data.get("memory_root") or None

    @staticmethod
    def _runtime_id_to_address(runtime_id: str) -> str:
        """Derive a deterministic Ethereum address from a runtime name."""
        h = hashlib.sha256(runtime_id.encode()).hexdigest()
        return "0x" + h[:40]

    # ══════════════════════════════════════════════════════════════════
    # Contract 3: TaskStateManager.sol (OUR contract)
    # ══════════════════════════════════════════════════════════════════

    def create_task(self, task_id: str, agent_id: str) -> TaskRecord:
        """
        Create a new task entry.

        In production: TaskStateManager.createTask(taskId, agentId)
        Requires: caller == owner of agentId (checked via ERC-8004).
        """
        if self._mode == "chain":
            task_bytes = self._task_id_to_bytes32(task_id)
            self._chain_client.create_task(task_bytes, _agent_id_to_int(agent_id))
            return TaskRecord(
                task_id=task_id, agent_id=agent_id,
                status="pending", version=0,
                created_at=time.time(), updated_at=time.time(),
            )

        # Local mode
        store = self._read_file(self._tasks_file)
        now = time.time()
        record = TaskRecord(
            task_id=task_id, agent_id=agent_id,
            status="pending", version=0,
            created_at=now, updated_at=now,
        )
        store["tasks"][task_id] = asdict(record)
        self._write_file(self._tasks_file, store)
        print(f"  [TaskStateManager] Task created: {task_id}")
        return record

    def update_task(self, task_id: str, state_hash: str,
                    status: str = "running",
                    expected_version: Optional[int] = None) -> TaskRecord:
        """
        Update task state with optimistic concurrency.

        In production: TaskStateManager.updateTask(taskId, stateHash, status, expectedVersion)
        Reverts if version != expected_version (another runtime wrote first).
        """
        if self._mode == "chain":
            task_bytes = self._task_id_to_bytes32(task_id)
            hash_bytes = bytes.fromhex(state_hash) if state_hash else b"\x00" * 32
            if len(hash_bytes) < 32:
                hash_bytes = hash_bytes.ljust(32, b"\x00")

            ev = expected_version if expected_version is not None else 0
            self._chain_client.update_task(
                task_bytes, hash_bytes, status, ev,
            )

            # Construct TaskRecord from known parameters instead of reading
            # back from chain. BSC testnet has read-after-write latency that
            # can cause get_task() to return None immediately after a write.
            new_version = ev + 1
            return TaskRecord(
                task_id=task_id,
                agent_id="",  # caller already knows the agent_id
                state_hash=state_hash,
                version=new_version,
                status=status,
                updated_at=time.time(),
            )

        # Local mode
        store = self._read_file(self._tasks_file)
        task_data = store["tasks"].get(task_id)
        if task_data is None:
            raise KeyError(f"Task {task_id} not found in TaskStateManager")
        if expected_version is not None and task_data["version"] != expected_version:
            raise ValueError(
                f"Version conflict on task {task_id}: "
                f"expected {expected_version}, got {task_data['version']}"
            )
        task_data["state_hash"] = state_hash
        task_data["status"] = status
        task_data["version"] += 1
        task_data["updated_at"] = time.time()
        store["tasks"][task_id] = task_data
        self._write_file(self._tasks_file, store)
        print(f"  [TaskStateManager] Task updated: {task_id} v{task_data['version']} [{status}]")
        return TaskRecord(**task_data)

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        """Read task record from TaskStateManager."""
        if self._mode == "chain":
            task_bytes = self._task_id_to_bytes32(task_id)
            task_data = self._chain_client.get_task(task_bytes)
            if task_data is None:
                return None
            return TaskRecord(
                task_id=task_id,
                agent_id=str(task_data["agent_id"]),
                state_hash=task_data["state_hash"].hex(),
                version=task_data["version"],
                status=task_data["status"],
                updated_at=float(task_data["updated_at"]),
            )

        store = self._read_file(self._tasks_file)
        data = store["tasks"].get(task_id)
        return TaskRecord(**data) if data else None

    @staticmethod
    def _task_id_to_bytes32(task_id: str) -> bytes:
        """Convert a string task_id to a bytes32 for on-chain storage."""
        if task_id.startswith("0x") and len(task_id) == 66:
            return bytes.fromhex(task_id[2:])
        return hashlib.sha256(task_id.encode()).digest()

    # ══════════════════════════════════════════════════════════════════
    # Object store (off-chain data layer)
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _content_hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    # ── Storage path helpers ─────────────────────────────────────────

    def set_agent_address(self, agent_id: str, agent_address: str) -> None:
        """
        Register the on-chain address for an agent ID.

        Once set, all storage paths for this agent will use
        ``{address}/{tokenId}`` as the folder name. This keeps
        agents browsable by owner address while preserving
        per-agent isolation (multiple agents can share the same
        owner address).
        """
        if agent_address:
            self._address_map[agent_id] = agent_address

    def agent_folder(self, agent_id: str) -> str:
        """
        Resolve the storage folder name for an agent.

        Returns ``{address}/{tokenId}`` if an on-chain address is
        registered, otherwise falls back to ``{agent_id}`` (local mode).

        Layout:
            rune/agents/{ownerAddress}/{tokenId}/sessions/...
            rune/agents/{ownerAddress}/{tokenId}/artifacts/...
        """
        addr = self._address_map.get(agent_id)
        if addr:
            return f"{addr}/{agent_id}"
        return agent_id

    @staticmethod
    def storage_path(
        agent_id: str,
        category: str,
        content_hash: str,
        *,
        sub_key: str = "",
        filename: str = "",
    ) -> str:
        """
        Build a structured object-store path.

        Layout:
            rune/agents/{ownerAddress}/{tokenId}/state/{hash}.json
            rune/agents/{ownerAddress}/{tokenId}/sessions/{sessionId}/{hash}.json
            rune/agents/{ownerAddress}/{tokenId}/tasks/{taskId}/{hash}.json
            rune/agents/{ownerAddress}/{tokenId}/artifacts/{filename}

        The ``agent_id`` parameter should be the resolved folder name
        (call ``state_mgr.agent_folder(agent_id)`` to get ``{address}/{tokenId}``).

        Args:
            agent_id: Agent folder name (from agent_folder(), or raw tokenId in local mode).
            category: One of "state", "sessions", "tasks", "artifacts".
            content_hash: SHA-256 hex string.
            sub_key: Optional sub-directory (session_id or task_id).
            filename: Optional human-readable filename (for artifacts).
        """
        base = f"rune/agents/{agent_id}/{category}"
        if sub_key:
            base = f"{base}/{sub_key}"
        if filename:
            return f"{base}/{filename}"
        return f"{base}/{content_hash}.json"

    def store_data(self, data: bytes, object_path: str = "") -> str:
        """
        Store raw bytes in the object store; return content hash.

        Args:
            data: Raw bytes to store.
            object_path: Optional structured path. Built via
                ``StateManager.storage_path()``. If empty, uses the
                flat content-hash naming.
        """
        content_hash = self._content_hash(data)
        path = self.data_dir / content_hash
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                f.write(data)
            tmp.rename(path)
            print(f"  [Store] Stored {len(data)} bytes -> {content_hash[:16]}...")

        # Also store at structured path for browsability
        if object_path:
            structured = self.data_dir / object_path
            structured.parent.mkdir(parents=True, exist_ok=True)
            if not structured.exists():
                try:
                    structured.symlink_to(path.resolve())
                except (OSError, NotImplementedError):
                    import shutil
                    shutil.copy2(path, structured)

        return content_hash

    def load_data(self, content_hash: str, object_path: str = "") -> Optional[bytes]:
        """
        Load raw bytes from the object store by content hash.

        Args:
            content_hash: SHA-256 hex hash of the object.
            object_path: Optional structured path (tries this first).
        """
        # Try structured path first, then canonical
        if object_path:
            structured = self.data_dir / object_path
            if structured.exists():
                with open(structured, "rb") as f:
                    data = f.read()
                print(f"  [Store] Loaded {len(data)} bytes <- {object_path}")
                return data

        path = self.data_dir / content_hash
        if not path.exists():
            return None
        with open(path, "rb") as f:
            data = f.read()
        print(f"  [Store] Loaded {len(data)} bytes <- {content_hash[:16]}...")
        return data

    def store_json(self, obj: Any, object_path: str = "") -> str:
        """Convenience: serialize to JSON, store, return hash."""
        data = json.dumps(obj, default=str, sort_keys=True).encode("utf-8")
        return self.store_data(data, object_path=object_path)

    def load_json(self, content_hash: str, object_path: str = "") -> Optional[Any]:
        """Convenience: load by hash, deserialize from JSON."""
        data = self.load_data(content_hash, object_path=object_path)
        if data is None:
            return None
        return json.loads(data.decode("utf-8"))

    def list_agent_objects(self, agent_id: str, category: str = "") -> list[dict]:
        """
        List all stored objects for an agent.

        Args:
            agent_id: The agent's ERC-8004 ID (resolves to address if known).
            category: Optional filter — "state", "sessions", "tasks", "artifacts".
                      If empty, lists everything under the agent.

        Returns:
            List of {"key": "rune/agents/{address}/...", "size": 1024, ...}
        """
        folder = self.agent_folder(agent_id) if agent_id else ""
        prefix = f"rune/agents/{folder}/"
        if category:
            prefix += f"{category}/"

        results: list[dict] = []
        base = self.data_dir / prefix
        if not base.exists():
            return results
        for p in sorted(base.rglob("*")):
            if p.is_file():
                rel = p.relative_to(self.data_dir)
                results.append({
                    "key": str(rel),
                    "size": p.stat().st_size,
                    "modified": p.stat().st_mtime,
                })
        return results

    # ── Diagnostics ──────────────────────────────────────────────────

    def info(self) -> dict:
        """Get current configuration and connection status."""
        result = {
            "mode": self._mode,
            "data_dir": str(self.data_dir),
        }
        if self._chain_client:
            result["chain"] = self._chain_client.connection_info()
        return result
