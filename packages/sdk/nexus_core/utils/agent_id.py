"""Agent ID conventions for ERC-8004 agents.

* :func:`agent_id_to_int` converts a string agent_id to a deterministic
  uint256 for on-chain contract calls. Used by StateManager and
  ChainBackend.
"""

import hashlib


def agent_id_to_int(agent_id: str) -> int:
    """Convert a string agent_id to a deterministic uint256.

    Priority: numeric string → hex string → SHA-256 hash.
    Always returns a positive integer suitable for uint256 contract params.
    """
    if not agent_id:
        return 0

    # Try as plain integer first
    try:
        return int(agent_id)
    except (ValueError, TypeError):
        pass

    # Try as hex
    if isinstance(agent_id, str) and agent_id.startswith("0x"):
        try:
            return int(agent_id, 16)
        except (ValueError, TypeError):
            pass

    # Fallback: deterministic hash
    return int.from_bytes(
        hashlib.sha256(agent_id.encode("utf-8")).digest(), "big"
    )
