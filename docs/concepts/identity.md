# Identity — three IDs, one user

This trips everyone up on first read. There are **three identifiers** in
play across the system. Knowing which one a piece of code wants is the
difference between "this works" and "I'm in the wrong codepath".

## The three IDs

| Name | Type | Issued by | Lifetime | Used for |
|---|---|---|---|---|
| **`user_id`** | UUID string | Server (`/auth/register`) | Per server account | Database keys, JWT subject, server-side scoping |
| **`agent_id`** | string `"user-{user_id[:8]}"` | Server (`twin_manager._agent_id_for`) | Per twin instance | Local SQLite path, log lines |
| **`token_id`** | int (ERC-8004 NFT token id) | BSC `IdentityRegistry.register` | Forever, on-chain | On-chain identity |

## How they connect

```
                            ┌─────────────────┐
   password login           │                 │
  ──────────────────────────▶   server.users   │
                            │  table row      │
                            │                 │
                            │  id: UUID         ← user_id
                            │  display_name     │
                            │  chain_agent_id   ← token_id (nullable
                            │                 │   until first chat)
                            └────────┬────────┘
                                     │
        first /llm/chat call ────────┤
                                     │
                                     ▼
                  bootstrap_chain_identity(user_id):
                    ┌──────────────────────────────────────┐
                    │  if users.chain_agent_id is NULL:    │
                    │    BSCClient.register_agent(name)    │
                    │      → returns int token_id          │
                    │    UPDATE users SET                  │
                    │      chain_agent_id = token_id       │
                    └──────────────────────────────────────┘
                                     │
                                     ▼
                  TwinManager._create_twin(user_id):
                    ┌──────────────────────────────────────┐
                    │  agent_id = f"user-{user_id[:8]}"    │
                    │  base_dir = ~/.nexus_server/twins/{user_id}/
                    │                                      │
                    │  DigitalTwin.create(                 │
                    │      agent_id=agent_id,              │
                    │      cached_agent_id=token_id, …)    │
                    └──────────────────────────────────────┘
```

So the three IDs map cleanly:

```
user_id (UUID)  ──┬──── lookup → users.chain_agent_id → token_id (int)
                  │
                  ├──── derive  → agent_id = "user-" + user_id[:8]
                  │
                  └──── path    → ~/.nexus_server/twins/{user_id}/...
```

## Why three and not one

You could imagine collapsing to a single ID. Each layer would object:

- **Server** wants `user_id` to be a UUID — 128 bits of entropy makes a
  good database PK and JWT subject. ERC-8004 token ids are sequential
  ints, leaking signup order.
- **SDK / Twin** want a string `agent_id` that's stable across sessions
  and filesystem-safe. UUIDs work but are noisy in logs (`user-22183952`
  is more readable than the full UUID).
- **BSC** has its own ID space — ERC-8004 IdentityRegistry mints
  sequential `tokenId`s. We can't pre-assign them; the contract decides.

So the system has three coordinate spaces and just maps between them at
the boundaries.

## Where each is canonical

| File / table | Stores | Reads what to look up X |
|---|---|---|
| `nexus_server.db.users.id` | `user_id` (PK) | — |
| `nexus_server.db.users.chain_agent_id` | `token_id` for a `user_id` | JWT → user_id → SELECT |
| `nexus_server.db.users.chain_register_tx` | The BSC tx that minted the token | Audit only |
| BSC `IdentityRegistry` | The `token_id` ↔ wallet mapping | `BSCClient.agent_exists(token_id)` |
| BSC `AgentStateExtension` | State-root hashes per `token_id` | `BSCClient.get_state_root(token_id)` |
| `~/.nexus_server/twins/{user_id}/` | Twin's local files (event log + curated memory) | server-side path |
| Twin's EventLog SQLite (file inside ↑) | `agent_id` column = `"user-{user_id[:8]}"` | Twin's own |

## The "user-{user_id[:8]}" convention

Server's `twin_manager._agent_id_for(user_id)` returns
`f"user-{user_id[:8]}"`. This is **derived, not stored** — anywhere we
need `agent_id` we recompute it.

Why first-8-chars and not full UUID:
- Human-readable in logs
- Filesystem-safe (UUID dashes are fine, we just chose to be conservative)
- 8 hex chars = 32 bits — collision probability is `~1 in 4 billion`
  per user pair on the same server. For a single-operator deployment
  that's effectively zero. (For SaaS at scale we'd want a longer prefix
  or stored mapping.)

The reverse mapping (`agent_id` → `user_id`) is **lossy**. We do it in
exactly one place: `twin_manager._user_id_for_agent` looks up
`users WHERE id LIKE '{prefix}%'` and returns the row only if there's
a unique match. Used by the chain-activity log handler to attribute
SDK log lines (`agent=user-22183952`) back to a `user_id` for
`twin_chain_events` row attribution.


The bucket is keyed on `token_id` (the on-chain ID), not `user_id` or
`agent_id`. Why: the bucket is the **on-chain-verifiable** copy of the
agent's data. A third party with the bucket name can independently
reconstruct everything; the only ID that means anything to a third
party is the on-chain one.

The function explicitly refuses None / empty input — there is no shared
bucket fallback. Pre-S6 there was a `nexus-agent-state` shared bucket
for unregistered agents; that was a multi-tenant data leak (one agent's
data co-mingled with another's). Killed in
[task #47](../../HISTORY.md#layer-leakage-cleanup).

## Identity bootstrap timing

Three states a user can be in:

1. **Just registered, never chatted**:
   `users.chain_agent_id IS NULL`. No twin exists. No bucket.
2. **First chat in flight**:
   `bootstrap_chain_identity` running. Mints the token, populates
   `users.chain_agent_id`, then twin starts in chain mode and writes
   its first event. SDK's `_ensure_bucket_once()` auto-creates the
   bucket on first PUT.
3. **Steady state**:
   Token id stable forever. Bucket stable. Twin writes events; periodic
   state-root anchors update on BSC.

The user never knows about state 2 — it's transparent inside the first
chat round-trip. Bug 1 / Bug 2 fixes (see [HISTORY.md](../../HISTORY.md))
ensure this transition is atomic.

## File pointers

- `packages/sdk/nexus_core/utils/agent_id.py` — `agent_id_to_int`
- `packages/sdk/nexus_core/chain/...` — `BSCClient.register_agent`
- `packages/server/nexus_server/twin_manager.py` — `bootstrap_chain_identity`,
  `_agent_id_for`, `_user_id_for_agent`
- `packages/server/nexus_server/database.py` — `users.chain_agent_id`
  column

## See also

- [modes](modes.md) — what changes when the user has no `token_id` yet
- [data-flow](data-flow.md) — bootstrap timing in context
