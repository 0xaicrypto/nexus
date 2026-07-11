"""Blockchain chain proxy router.

Server-side custodial path: when the operator has configured
``SERVER_PRIVATE_KEY`` (and the network's RPC + contract addresses are
reachable via env), this router calls the real ERC-8004 Identity Registry
on BSC via the SDK's :class:`BSCClient`.

If chain config is incomplete the router degrades gracefully to mock
responses (status="pending"), so the rest of the product keeps working
in dev environments without a private key set.
"""

import asyncio
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus_server.auth import get_current_user
from nexus_server.config import get_config
from nexus_server.database import get_db_connection

logger = logging.getLogger(__name__)
config = get_config()

router = APIRouter(prefix="/api/v1/chain", tags=["chain"])


# ───────────────────────────────────────────────────────────────────────────
# Request/Response Models
# ───────────────────────────────────────────────────────────────────────────


class ChainAgentRegisterRequest(BaseModel):
    """Chain agent registration request.

    ``agent_name`` is optional: when omitted (or blank) the server falls
    back to the authenticated user's stored ``display_name`` so the
    desktop client doesn't have to plumb a separate "what should we
    register you as?" prompt through the login flow.
    """

    agent_name: Optional[str] = Field(default=None, max_length=255)
    metadata: Optional[dict] = None


class ChainAgentRegisterResponse(BaseModel):
    """Chain agent registration response."""

    agent_id: str
    tx_hash: Optional[str]
    status: str  # "registered" | "pending" | "failed"


class ChainAgentInfo(BaseModel):
    """On-chain agent information."""

    agent_id: str
    user_id: str
    agent_name: str
    created_at: str
    metadata: Optional[dict]


# ───────────────────────────────────────────────────────────────────────────
# Lazy chain client (constructed once per process when first needed)
# ───────────────────────────────────────────────────────────────────────────


_chain_client = None
_chain_client_lock = threading.Lock()
# Allow tests to inject a stub. When non-None, _get_chain_client returns this
# instead of constructing a real BSCClient. Reset to None to use real.
_chain_client_test_override = None


def _verify_contract_consistency(client, expected_id_registry: str) -> None:
    """Cross-check the deployed contracts agree on the identity registry.

    AgentStateExtension stores its IdentityRegistry pointer as `immutable`,
    set at deploy time. If the registered agent_state_address points at a
    contract whose internal registry != the registry our env says we're
    using, *every* onlyAgentOwner-protected call (setActiveRuntime,
    updateStateRoot) reverts with AgentNotRegistered — silently, because
    it's a custom error. This check makes that failure mode loud.
    """
    if client.agent_state is None or not expected_id_registry:
        return
    try:
        actual = client.agent_state.functions.identityRegistry().call()
    except Exception as e:
        logger.warning(
            "Could not read AgentStateExtension.identityRegistry(): %s", e
        )
        return
    from web3 import Web3
    actual_cs = Web3.to_checksum_address(actual)
    expected_cs = Web3.to_checksum_address(expected_id_registry)
    if actual_cs != expected_cs:
        logger.error(
            "DEPLOYMENT MISMATCH: AgentStateExtension at %s has identityRegistry()=%s "
            "but env NEXUS_TESTNET_IDENTITY_REGISTRY=%s. "
            "Every chain anchor will revert with AgentNotRegistered. "
            "Check packages/sdk/contracts/deployments.json for the right addresses.",
            client.agent_state.address, actual_cs, expected_cs,
        )
    else:
        logger.info(
            "Contract consistency OK: AgentStateExtension.identityRegistry == %s",
            expected_cs,
        )


def _get_chain_client():
    """Return a singleton BSCClient or None if chain is not configured.

    Tests can monkey-patch ``_chain_client_test_override`` with a fake that
    has ``register_agent(name) -> int`` and we'll use that instead.
    """
    global _chain_client
    if _chain_client_test_override is not None:
        return _chain_client_test_override

    if not config.chain_is_configured:
        return None

    if _chain_client is not None:
        return _chain_client

    with _chain_client_lock:
        if _chain_client is not None:
            return _chain_client

        try:
            from nexus_core.chain import BSCClient
        except ImportError as e:
            logger.warning(
                "BSCClient unavailable (%s); chain ops disabled.", e
            )
            return None

        # Normalize private key (allow with or without 0x prefix)
        pk = config.SERVER_PRIVATE_KEY or ""
        if pk and not pk.startswith("0x"):
            pk = "0x" + pk

        is_mainnet = "mainnet" in config.NEXUS_NETWORK
        rpc = config.chain_active_rpc
        identity_addr = (
            config.NEXUS_MAINNET_IDENTITY_REGISTRY
            if is_mainnet
            else config.NEXUS_TESTNET_IDENTITY_REGISTRY
        )
        agent_state_addr = (
            None
            if is_mainnet
            else config.NEXUS_TESTNET_AGENT_STATE_ADDRESS
        )
        task_manager_addr = (
            None
            if is_mainnet
            else config.NEXUS_TESTNET_TASK_MANAGER_ADDRESS
        )

        try:
            _chain_client = BSCClient(
                rpc_url=rpc,
                private_key=pk,
                identity_registry_address=identity_addr,
                agent_state_address=agent_state_addr,
                task_manager_address=task_manager_addr,
                network="bsc_mainnet" if is_mainnet else "bsc_testnet",
            )
            logger.info(
                "BSCClient ready: network=%s, signer=%s",
                config.NEXUS_NETWORK,
                getattr(_chain_client, "address", "?"),
            )
            _verify_contract_consistency(_chain_client, identity_addr)
        except Exception as e:
            logger.warning(
                "Could not initialize BSCClient: %s — falling back to mock.",
                e,
            )
            _chain_client = None

    return _chain_client


def _persist_chain_id(user_id: str, chain_agent_id: int, tx_hash: str) -> None:
    """Record the on-chain agent id (and registration tx) on the user row."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET chain_agent_id = ?, chain_register_tx = ?, "
            "updated_at = ? WHERE id = ?",
            (
                chain_agent_id,
                tx_hash,
                datetime.now(timezone.utc).isoformat(),
                user_id,
            ),
        )
        conn.commit()


def _read_chain_id(user_id: str) -> tuple[Optional[int], Optional[str]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chain_agent_id, chain_register_tx FROM users WHERE id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
    if not row:
        return None, None
    return row[0], row[1]


# ───────────────────────────────────────────────────────────────────────────
# Routes
# ───────────────────────────────────────────────────────────────────────────


@router.post(
    "/register-agent",
    response_model=ChainAgentRegisterResponse,
    deprecated=True,
)
async def register_chain_agent(
    request: ChainAgentRegisterRequest,
    current_user: str = Depends(get_current_user),
) -> ChainAgentRegisterResponse:
    """[DEPRECATED — pending removal in Round 2-C]

    Twin auto-registers ERC-8004 identity on first chat now (S6); the
    desktop no longer needs to call this endpoint at signup. We keep
    it during the transition because:

      * The desktop's onboarding flow still POSTs here.
      * Operators sometimes want an explicit "register now" affordance
        before any chat happens (e.g. to pre-warm a fleet of users).

    The endpoint delegates to ``twin_manager.bootstrap_chain_identity``
    so all registration logic — name resolution, RPC call, persist to
    ``users.chain_agent_id`` — lives in exactly one place.

    Status semantics (unchanged for back-compat):
      - ``registered`` — token id known (cached or just registered).
      - ``pending``    — chain unconfigured; synthetic id, NOT persisted.
      - ``failed``     — chain configured but the call blew up.

    The ``request.agent_name`` field is a no-op now: the helper resolves
    a name from the user's stored display_name with a synthetic
    fallback. The field is kept for wire compatibility — the desktop
    sends an empty string today.
    """
    # If we already registered for this user, just return the cached row.
    cached_id, cached_tx = _read_chain_id(current_user)
    if cached_id is not None:
        logger.info(
            "Chain agent already registered for %s: agent_id=%s",
            current_user, cached_id,
        )
        return ChainAgentRegisterResponse(
            agent_id=str(cached_id),
            tx_hash=cached_tx,
            status="registered",
        )

    # Chain unconfigured fallback — keep returning the synthetic
    # "pending" id so dev/test environments without SERVER_PRIVATE_KEY
    # keep working (and the desktop's pre-S6 onboarding doesn't crash).
    client = _get_chain_client()
    if client is None:
        logger.info(
            "Chain unconfigured; returning pending mock for user %s.",
            current_user,
        )
        return ChainAgentRegisterResponse(
            agent_id=str(uuid.uuid4()),
            tx_hash=None,
            status="pending",
        )

    # Delegate to the twin_manager helper. ``register_agent`` is a
    # synchronous web3 call (sign + send + receipt poll) → offload to
    # a thread so we don't block the event loop.
    from nexus_server import twin_manager
    token_id = await asyncio.to_thread(
        twin_manager.bootstrap_chain_identity, current_user,
    )
    if token_id is None:
        logger.error("Auto-bootstrap returned None for user %s", current_user)
        return ChainAgentRegisterResponse(
            agent_id="", tx_hash=None, status="failed",
        )

    logger.info("Agent %s registered on-chain for user %s", token_id, current_user)
    return ChainAgentRegisterResponse(
        agent_id=str(token_id),
        tx_hash=None,  # bootstrap doesn't surface tx_hash today
        status="registered",
    )


@router.get("/me", response_model=ChainAgentInfo)
async def get_my_chain_agent_info(
    current_user: str = Depends(get_current_user),
) -> ChainAgentInfo:
    """Convenience endpoint: 'who am I on chain?'

    Returns the same shape as ``/agent/{agent_id}``, but uses the
    authenticated user as the lookup key. Used by the desktop UI to
    render the ERC-8004 token id pill in the top bar without needing
    to remember an agent_id from a previous registration call.
    """
    return await get_chain_agent_info(agent_id="me", current_user=current_user)


@router.get("/agent/{agent_id}", response_model=ChainAgentInfo)
async def get_chain_agent_info(
    agent_id: str,
    current_user: str = Depends(get_current_user),
) -> ChainAgentInfo:
    """Get on-chain agent information.

    Currently returns DB-cached info (display_name, registration time).
    Live chain queries (state root, task counts) can be layered in later
    once we wire `agent_state` / `task_manager` reads here.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT display_name, chain_agent_id, chain_register_tx, "
                "created_at FROM users WHERE id = ?",
                (current_user,),
            )
            row = cursor.fetchone()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )

        display_name, chain_agent_id, register_tx, created_at = row
        on_chain = chain_agent_id is not None

        return ChainAgentInfo(
            agent_id=str(chain_agent_id) if on_chain else agent_id,
            user_id=current_user,
            agent_name=display_name or f"Agent {current_user[:8]}",
            created_at=created_at or datetime.now(timezone.utc).isoformat(),
            metadata={
                "on_chain": on_chain,
                "register_tx": register_tx,
                "network": config.NEXUS_NETWORK,
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get chain agent error: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to retrieve agent info",
        )
