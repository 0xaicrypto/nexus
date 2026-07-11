# Modes — chain mode vs local mode

A twin runs in one of two modes. Knowing which mode applies and why is
the difference between "my chats are anchored on BSC" and "my chats are
in a SQLite file on the server's disk".

## The two modes

### Chain mode

- Every event_log append → durable local data-store write.
- Periodically → BSC `AgentStateExtension.updateStateRoot(token_id, hash)`
  call (verifiability).
- Identity is on-chain: ERC-8004 token in `IdentityRegistry`.
- Bucket: `nexus-agent-{token_id}`, owned by the server's signing wallet.

### Local mode

- Every event_log append → just SQLite. No BSC.
- No on-chain identity.

Functionally **the chat works identically in both modes** — same DPM,
same ABC, same self-evolution. The only difference is whether the
durable copy + on-chain anchor happens.

## When each mode kicks in

The decision is made per-user at twin creation time, in
`twin_manager._resolve_chain_kwargs(user_id)`:

```python
def _resolve_chain_kwargs(user_id):
    # Gate 1: server has a private key to sign with?
    if not config.SERVER_PRIVATE_KEY:
        return {}                # → local mode

    # Gate 2: BSC RPC reachable?
    if not config.chain_active_rpc:
        return {}                # → local mode

    # Gate 3: user has been registered on chain?
    token_id = _read_chain_agent_id(user_id)
    if token_id is None:
        # auto-bootstrap once
        token_id = bootstrap_chain_identity(user_id)
        if token_id is None:
            return {}            # → local mode (registration failed)

    # All three gates passed → chain mode
    return {
        "private_key": config.SERVER_PRIVATE_KEY,
        "network": config.network_short,
        "rpc_url": config.chain_active_rpc,
        "agent_state_address": ...,
        "task_manager_address": ...,
        "identity_registry_address": ...,
    }
```

So the gates are: **wallet, network, identity**. All three or local.

## What's local-mode for

Three legitimate use cases:

1. **Dev / CI** — server has no `SERVER_PRIVATE_KEY` configured, or the
   developer doesn't want test traffic costing testnet gas.
2. **Pre-registration** — user just signed up, hasn't completed chain
   bootstrap (transient state). Twin runs in local mode for the first
   few seconds while `bootstrap_chain_identity` is in flight; after
   it returns, the next twin creation picks chain mode.
3. **Standalone / CLI use** — someone uses Nexus's `DigitalTwin.create`
   directly without `private_key=` to play with the agent on their
   laptop, no chain anywhere. (`Rune.local(base_dir=...)` returns a
   provider with `LocalBackend`.)

## What chain mode commits to

When chain mode is active, **every event_log.append is persisted to
the backend's durable local store synchronously** — once the call
returns, the data is on disk.

```python
# In SDK ChainBackend.store_json:
self._cache_write(path, raw)              # synchronous durable write
return content_hash                       # SHA-256 of the bytes
```

State-root anchoring on BSC is **not per-event**. The ChainBackend
batches and anchors periodically (configurable). For an idle agent,
no BSC tx fires — the cost is bounded by activity, not time.

## What's *server-managed* about chain mode

The signing wallet is `SERVER_PRIVATE_KEY` — one per server deployment.
This is **custodial chain mode**: web2 users without their own wallets
get on-chain agents because the server signs for them. The trade-off:

- **Pro**: zero-friction onboarding. Sign in with username + password, immediately
  have a chain-anchored agent.
- **Con**: the server operator can sign for the user. Not Web3-native.

A future *non-custodial* mode would have the user sign in with their
wallet (MetaMask / WalletConnect) and the server pass through the
user's address. That'd require:

- Auth flow change (password → wallet signature)
- ChainBackend's `private_key` becomes per-twin instead of server-wide
- Cost shifts to user (each twin pays its own gas)

Out of scope today.

## How the mode shows in the desktop UI

`/api/v1/agent/state.on_chain` is true iff `users.chain_agent_id IS NOT
NULL`. The desktop top bar renders:

- `Connected · ERC-8004 #{token_id}` when on-chain
- `Connected · chain disabled (server has no key)` when server has no
  `SERVER_PRIVATE_KEY` (gate 1 fails)
- `Connected · chain register failed: {reason}` when registration tried
  and failed

So a user can always see whether their twin is currently on-chain just
from the top bar.

## When you'd touch each piece

| You want to… | Touch |
|---|---|
| Run server in dev without spending testnet gas | Just don't set `SERVER_PRIVATE_KEY` — automatic local mode |
| Force a particular user into local mode | `UPDATE users SET chain_agent_id = NULL WHERE id = ?` (forces re-bootstrap which will fail without prereqs) |
| Add a non-custodial mode | New code in `twin_manager._resolve_chain_kwargs` to extract per-user wallet from JWT or a `wallets` table |
| Run BSC mainnet | Set `NEXUS_NETWORK=bsc-mainnet` + `NEXUS_MAINNET_RPC=...` + the mainnet contract addresses |
| Test chain mode without spending gas | Use a fake `BSCClient` via `chain_proxy._chain_client_test_override` |

## Chain mode requirements (env)

For chain mode to engage, server `.env` needs:

```bash
SERVER_PRIVATE_KEY=0x...                       # the custodial wallet
RUNE_NETWORK=bsc-testnet                       # or bsc-mainnet
RUNE_TESTNET_RPC=https://...                   # or RUNE_MAINNET_RPC
RUNE_TESTNET_AGENT_STATE_ADDRESS=0x10d8...     # contract addrs from
RUNE_TESTNET_IDENTITY_REGISTRY=0x8004...       # packages/sdk/contracts/
RUNE_TESTNET_TASK_MANAGER_ADDRESS=0x892E...    # deployments.json
```

Plus enough native BNB on the wallet to pay gas for ERC-8004
registration (one-time per user) + state-root anchors (periodic).

## File pointers

- `packages/server/nexus_server/twin_manager.py` —
  `_resolve_chain_kwargs`, `bootstrap_chain_identity`
- `packages/sdk/nexus_core/backends/chain.py` — ChainBackend
- `packages/sdk/nexus_core/backends/local.py` — LocalBackend
- `packages/server/nexus_server/config.py` — env variable parsing
- `packages/sdk/contracts/deployments.json` — testnet / mainnet contract
  addresses

## See also

- [identity](identity.md) — the three-IDs explainer (relevant when
  chain mode is the active mode)
- [data-flow](data-flow.md) — what each chat write looks like in chain
  mode
