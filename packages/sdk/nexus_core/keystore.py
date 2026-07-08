"""
Rune Keystore — wallet and agent identity management.

Wraps the BNBAgent SDK's EVMWalletProvider and ERC8004Agent to provide
a unified interface for:
  - Creating / loading encrypted keystores (MetaMask/Geth-compatible)
  - Registering agents on-chain via ERC-8004
  - Managing agent endpoints (A2A, MCP, web)
  - Querying agent identity and metadata

Usage:
    from nexus_core.keystore import Keystore

    # Create a new wallet + register an agent in one call
    ks = Keystore(password="my-secret", network="bsc-testnet")
    agent = ks.register(
        name="my-defi-agent",
        description="Analyzes DeFi yield opportunities",
        endpoints=[{"name": "A2A", "endpoint": "https://my-agent.com/a2a"}],
    )
    print(agent["agentId"])  # ERC-721 tokenId

    # Later: reload the same wallet
    ks = Keystore(password="my-secret", address="0x1234...")
    info = ks.get_agent(agent_id=agent["agentId"])
"""

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("nexus_core.keystore")


class Keystore:
    """
    Unified wallet + agent identity manager for Nexus.

    Encapsulates:
      - EVMWalletProvider: encrypted keystore (scrypt + AES-128-CTR)
      - ERC8004Agent: on-chain agent registration and metadata

    Wallet files are stored at ~/.bnbagent/wallets/<address>.json
    (compatible with MetaMask / Geth keystore format).
    """

    def __init__(
        self,
        password: str,
        private_key: Optional[str] = None,
        address: Optional[str] = None,
        network: str = "bsc-testnet",
        wallets_dir: Optional[str] = None,
        debug: bool = False,
    ):
        """
        Create or load a Rune keystore.

        Args:
            password: Encryption password for the keystore file.
            private_key: Import an existing private key (hex, with or without 0x).
                         If omitted and no address given, creates a new wallet.
            address: Load an existing keystore by wallet address.
                     If omitted and only one wallet exists, auto-selects it.
            network: "bsc-testnet" or "bsc-mainnet".
            wallets_dir: Custom directory for keystore files.
                         Defaults to ~/.bnbagent/wallets/
            debug: Enable verbose logging for contract calls.
        """
        from bnbagent.wallets import EVMWalletProvider
        from bnbagent.erc8004 import ERC8004Agent, AgentEndpoint

        self._AgentEndpoint = AgentEndpoint

        # ── Wallet ───────────────────────────────────────────────────
        self.wallet = EVMWalletProvider(
            password=password,
            private_key=private_key,
            address=address,
            persist=True,
            wallets_dir=wallets_dir,
        )
        logger.info(
            "Wallet %s (%s)", self.wallet.address, self.wallet.source
        )

        # ── ERC-8004 Agent SDK (lazy — tolerates offline RPC) ────────
        self._network = network
        self._debug = debug
        self._sdk = None
        self._ERC8004Agent = ERC8004Agent

    @property
    def sdk(self):
        """Lazily initialize ERC8004Agent (requires RPC connection)."""
        if self._sdk is None:
            self._sdk = self._ERC8004Agent(
                wallet_provider=self.wallet,
                network=self._network,
                debug=self._debug,
            )
        return self._sdk

    # ── Wallet operations ────────────────────────────────────────────

    @property
    def address(self) -> str:
        """Wallet address (checksummed)."""
        return self.wallet.address

    @property
    def source(self) -> str:
        """How the wallet was loaded: 'imported', 'loaded_keystore', or 'created_new'."""
        return self.wallet.source

    def export_private_key(self) -> str:
        """Export the raw private key (hex with 0x prefix)."""
        return self.wallet.export_private_key()

    def export_keystore(self) -> dict:
        """Export the Keystore V3 JSON object."""
        return self.wallet.export_keystore()

    @staticmethod
    def list_wallets(wallets_dir: Optional[str] = None) -> list:
        """List all wallet addresses in the keystore directory."""
        from bnbagent.wallets import EVMWalletProvider
        return EVMWalletProvider.list_wallets(wallets_dir=wallets_dir)

    @staticmethod
    def keystore_exists(
        address: Optional[str] = None,
        wallets_dir: Optional[str] = None,
    ) -> bool:
        """Check if a keystore file exists for the given address."""
        from bnbagent.wallets import EVMWalletProvider
        return EVMWalletProvider.keystore_exists(
            address=address, wallets_dir=wallets_dir
        )

    # ── Agent registration ───────────────────────────────────────────

    def register(
        self,
        name: str,
        description: str,
        endpoints: Optional[list] = None,
        image: Optional[str] = None,
        metadata: Optional[list] = None,
    ) -> dict:
        """
        Register a new agent on-chain via ERC-8004.

        This mints an ERC-721 identity NFT and stores the agent's
        registration file (name, description, endpoints) on-chain.

        Args:
            name: Human-readable agent name.
            description: What the agent does.
            endpoints: List of service endpoints. Each can be:
                - AgentEndpoint object
                - dict with keys: name, endpoint, version (optional), capabilities (optional)
                Example: [{"name": "A2A", "endpoint": "https://my-agent.com/a2a"}]
            image: URL to agent avatar/icon.
            metadata: Additional on-chain metadata key-value pairs.
                Example: [{"key": "version", "value": "1.0.0"}]

        Returns:
            dict with keys: success, transactionHash, agentId, receipt, agentURI
        """
        # Convert dict endpoints to AgentEndpoint objects
        agent_endpoints = []
        for ep in (endpoints or []):
            if isinstance(ep, dict):
                agent_endpoints.append(self._AgentEndpoint(
                    name=ep["name"],
                    endpoint=ep["endpoint"],
                    version=ep.get("version"),
                    capabilities=ep.get("capabilities", []),
                ))
            else:
                agent_endpoints.append(ep)

        # Generate agent URI (base64 data URI with registration file)
        agent_uri = self.sdk.generate_agent_uri(
            name=name,
            description=description,
            endpoints=agent_endpoints,
            image=image,
        )

        # Register on-chain
        result = self.sdk.register_agent(
            agent_uri=agent_uri,
            metadata=metadata,
        )

        logger.info(
            "Agent registered: id=%s tx=%s",
            result.get("agentId"), result.get("transactionHash"),
        )
        return result

    # ── Agent queries ────────────────────────────────────────────────

    def get_agent(self, agent_id: int) -> dict:
        """
        Get agent info from ERC-8004 registry.

        Returns:
            dict with keys: agentId, agentAddress, owner, agentURI
        """
        return self.sdk.get_agent_info(agent_id)

    def find_agent(self, name: str) -> Optional[dict]:
        """
        Find an agent by name (filtered to current wallet owner).

        Returns:
            dict with keys: name, agent_id, agent_uri, owner_address
            or None if not found.
        """
        return self.sdk.get_local_agent_info(name)

    def list_agents(self, limit: int = 10, offset: int = 0) -> dict:
        """
        List all registered agents (via indexer API).

        Returns:
            dict with keys: items, total, limit, offset
        """
        return self.sdk.get_all_agents(limit=limit, offset=offset)

    def update_agent_uri(
        self,
        agent_id: int,
        name: str,
        description: str,
        endpoints: Optional[list] = None,
        image: Optional[str] = None,
    ) -> dict:
        """
        Update an existing agent's registration file.

        Args:
            agent_id: ERC-8004 tokenId to update.
            name: New agent name.
            description: New description.
            endpoints: New endpoint list.
            image: New image URL.

        Returns:
            dict with keys: success, transactionHash, receipt, agentURI
        """
        agent_endpoints = []
        for ep in (endpoints or []):
            if isinstance(ep, dict):
                agent_endpoints.append(self._AgentEndpoint(
                    name=ep["name"],
                    endpoint=ep["endpoint"],
                    version=ep.get("version"),
                    capabilities=ep.get("capabilities", []),
                ))
            else:
                agent_endpoints.append(ep)

        agent_uri = self.sdk.generate_agent_uri(
            name=name,
            description=description,
            endpoints=agent_endpoints,
            image=image,
            agent_id=agent_id,
        )
        return self.sdk.set_agent_uri(agent_id, agent_uri)

    # ── Metadata ─────────────────────────────────────────────────────

    def get_metadata(self, agent_id: int, key: str) -> str:
        """Read on-chain metadata for an agent."""
        return self.sdk.get_metadata(agent_id, key)

    def set_metadata(self, agent_id: int, key: str, value: str) -> dict:
        """Write on-chain metadata for an agent."""
        return self.sdk.set_metadata(agent_id, key, value)

    # ── Agent URI parsing ────────────────────────────────────────────

    @staticmethod
    def parse_agent_uri(agent_uri: str) -> Optional[dict]:
        """
        Parse an agent registration file from a data URI or URL.

        Returns:
            Parsed registration JSON, or None if invalid.
        """
        from bnbagent.erc8004 import ERC8004Agent
        return ERC8004Agent.parse_agent_uri(agent_uri)

    # ── Integration with Rune StateManager ───────────────────────────

    def create_state_manager(
        self,
        agent_state_address: Optional[str] = None,
        task_manager_address: Optional[str] = None,
    ):
        """
        Create a StateManager pre-configured with this keystore's wallet.

        This connects the agent identity (ERC-8004) with the Rune state
        layer (AgentStateExtension + TaskStateManager).

        Args:
            agent_state_address: Deployed AgentStateExtension contract address.
            task_manager_address: Deployed TaskStateManager contract address.

        Returns:
            StateManager in chain mode, ready for on-chain operations.
        """
        from .state import StateManager

        from .chain import BSC_TESTNET_RPC, BSC_MAINNET_RPC, ERC8004_ADDRESSES

        private_key = self.wallet.export_private_key()
        is_testnet = "testnet" in self._network
        network_key = "bsc_testnet" if is_testnet else "bsc_mainnet"

        # Try to get RPC from bnbagent SDK; if empty, use our defaults
        rpc_url = None
        try:
            rpc_url = self.sdk.network.get("rpc_url", "") or None
        except Exception:
            pass
        if not rpc_url:
            rpc_url = BSC_TESTNET_RPC if is_testnet else BSC_MAINNET_RPC

        # Try to get identity registry address from SDK, fallback to known
        try:
            id_addr = self.sdk.contract_address
        except Exception:
            id_addr = ERC8004_ADDRESSES.get(network_key)

        return StateManager(
            rpc_url=rpc_url,
            private_key=private_key,
            agent_state_address=agent_state_address,
            task_manager_address=task_manager_address,
            identity_registry_address=id_addr,
            network=network_key,
            mode="chain",
        )

    # ── Diagnostics ──────────────────────────────────────────────────

    def info(self) -> dict:
        """Get keystore and connection info."""
        result = {
            "address": self.address,
            "source": self.source,
            "network": self._network,
            "wallets": self.list_wallets(),
        }
        try:
            result["identity_registry"] = self.sdk.contract_address
        except Exception:
            result["identity_registry"] = "(not connected)"
        return result

    def __repr__(self) -> str:
        return (
            f"Keystore(address={self.address}, "
            f"source={self.source}, network={self._network})"
        )
