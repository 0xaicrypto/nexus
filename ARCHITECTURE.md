# Architecture

Four layers, single-direction dependency. Read top-down — each layer is
explained in terms of what it adds to the layer below.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Desktop (RuneDesktop.UI / .Core)             — Avalonia C#          │
│    UI only. Holds nothing on disk except JWT. Pulls history,        │
│    memories, anchors from server.                                    │
└────────────────────────┬─────────────────────────────────────────────┘
                         │ HTTP + JWT
                         ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Server (nexus_server)                          — FastAPI / Python    │
│    Multi-tenant HTTP frontend. One DigitalTwin per logged-in user.  │
│    Persistence: nexus_server.db (auth/users/twin_chain_events/...)   │
│    + per-user twin event_log SQLite under ~/.nexus_server/twins/.    │
└──────┬───────────────────────────────────────┬───────────────────────┘
       │                                       │
       │ Per-user agent abstraction            │ Direct (rare:
       │ (TwinManager.get_twin(user_id))       │  bootstrap, distill)
       ▼                                       │
┌──────────────────────────────────┐           │
│  Nexus (nexus)               │           │
│    DigitalTwin class.            │           │
│    9-step chat flow.             │           │
│    Self-evolution (persona /     │           │
│    skills / memory / knowledge / │           │
│    social).                      │           │
└────────────────┬─────────────────┘           │
                 │                             │
                 │ Uses every primitive        │
                 ▼                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│  SDK (nexus_core)                          — Python              │
│    Entry points: testnet() / mainnet() / local() → AgentRuntime     │
│    Storage backends: ChainBackend / LocalBackend / MockBackend       │
│    Memory primitives: EventLog / CuratedMemory / EventLogCompactor   │
│    Contract primitives: ContractEngine / DriftScore                  │
│    LLMClient / ToolRegistry / SkillManager / MCPManager              │
│    BSCClient (web3)                                                  │
│    distill() / utils                                                 │
└─────────────────────────┬────────────────────────────────────────────┘
                          │
                          ▼
                       BSC RPC
```

## Dependency direction

Imports flow strictly downward. Verified:

- **SDK** imports from neither Nexus nor Server. (`grep "from nexus\|from nexus_server" packages/sdk/` returns nothing.)
- **Nexus** imports from SDK only. (`from nexus_core import ...`.)
- **Server** imports from Nexus + (rarely) SDK directly for utilities like
  `distill`, `BSCClient`.
- **Desktop** talks to Server over HTTP only.

This invariant is the single most important property of the architecture.
A PR that adds an upward import (e.g. SDK importing Nexus) should be
rejected on principle — that direction lock is what lets us version each
layer independently.

## What each layer is responsible for

### SDK — `packages/sdk/nexus_core/`

**Knows about**: BSC web3, append-only event
logs, content-hash anchoring, curated-memory file format, contract spec
parsing, LLM provider abstraction, tool function-calling.

**Doesn't know about**: agents, users, HTTP, JWT, multi-tenancy, twins.

**Public entry point**: `nexus_core.testnet(...)` / `nexus_core.mainnet(...)` /
`nexus_core.local(...)` returns an `AgentRuntime` with `.sessions / .tasks /
.memory / .artifacts / .impressions` namespaces.

**Why it exists separately from Nexus**: someone could build an entirely
different agent framework on top of these primitives. The SDK is the
"contract with BNB Chain"; what you build on top is your business.

### Nexus — `packages/nexus/nexus/`

**Knows about**: the lifecycle of one specific kind of agent (DigitalTwin),
the 9-step chat flow, when to compact memory, when to evolve persona,
when to learn skills, how to project relevant memory for a turn.

**Doesn't know about**: HTTP, JWT, multi-tenancy. It's a Python class. You
hand it config + private_key, it gives you `.chat()`.

**Public entry point**: `DigitalTwin.create(...)` returns an initialised
twin. `await twin.chat("hello")` returns the assistant's reply.

**Why it exists separately from Server**: the same DigitalTwin can be
embedded in a CLI, a Telegram bot, or a peer-to-peer agent runtime. HTTP
is just one way to expose it. Pre-server use cases drove this split and
they remain valid.

### Server — `packages/server/nexus_server/`

**Knows about**: HTTP routes, JWT verification, WebAuthn passkeys,
multi-tenancy (one twin per user), rate limiting, CORS, the desktop's
view-shape API endpoints (`/agent/messages`, `/agent/timeline`, etc.),
the desktop's onboarding flow (chain registration on signup).

**Doesn't know about**: how chat actually works inside a turn (delegated
to `await twin.chat(...)`), how anchoring works (delegated to twin's
ChainBackend), how memory is structured (it just opens twin's SQLite
read-only).

**Public entry point**: `uvicorn nexus_server.main:app`.

**Why this layer exists**: agents need a multi-tenant, authenticated HTTP
front so a desktop / web / mobile UI can hit them without each twin owning
its own port. Server is the "operating concerns" layer.

### Desktop — `packages/desktop/`

**Knows about**: rendering chat, file picker UI, polling endpoints for
status, passkey authentication on launch.

**Doesn't know about**: chat history (pulled from server every login),
memories (rendered from server), anchors (rendered from server), agent
identity (read from server).

**Why it's a thin client**: in the original design the desktop kept a
local SQLite event log and pushed events to the server. After Round 2
that's gone — server's twin is the single source of truth, desktop is a
view layer.

## Data flow: one chat turn

```
desktop  ──POST /api/v1/llm/chat────────▶  server.chat
                                              │
                                              ▼
                              twin = TwinManager.get_twin(user_id)
                                  (lazy create + chain bootstrap if first time)
                                              │
                                              ▼
                                        twin.chat(message)  ── 9 steps:
                                              │     1. ContractEngine.pre_check
                                              │     2. event_log.append("user_message", …)
                                              │        → ChainBackend → durable store
                                              │     3. project memory (CuratedMemory or
                                              │        ProjectionMemory)
                                              │     4. llm.chat(messages, system, tools)
                                              │     5. ContractEngine.post_check
                                              │     6. DriftScore.update
                                              │     7. event_log.append("assistant_response", …)
                                              │        → ChainBackend → durable store
                                              │     8. on_event mirror → server.sync_events
                                              │     9. background:
                                              │        - evolution.after_conversation_turn
                                              │          (extract memories, learn skills,
                                              │           reflect on persona)
                                              │        - save session checkpoint
                                              │        - periodic: state-root anchor on BSC
                                              ▼
                                        return reply
                                              │
                                              ▼
                                  HTTP 200 { reply, model, … }
```

For the full byte-level trace see [`docs/concepts/data-flow.md`](docs/concepts/data-flow.md).

## Where data lives

| Data | Where | Owner |
|---|---|---|
| User auth + JWT secret | `nexus_server.db.users` | Server |
| Per-user twin event log | `~/.nexus_server/twins/{user_id}/event_log/{agent_id}.db` | Twin (SDK EventLog format) |
| Per-user CuratedMemory snapshot | `~/.nexus_server/twins/{user_id}/curated_memory.md` | Twin |
| Per-user persona evolution history | `~/.nexus_server/twins/{user_id}/persona.json` | Twin |
| Per-user contracts + drift state | `~/.nexus_server/twins/{user_id}/contracts/...` | Twin |
| Chain mode: durable event store | `NEXUS_CACHE_DIR` local store (S3-compatible mirror planned) | ChainBackend (SDK) |
| Chain mode: state-root hashes | BSC `AgentStateExtension` per token | ChainBackend (SDK) |
| Identity registration | BSC ERC-8004 IdentityRegistry | SDK (`BSCClient.register_agent`) |
| Server-side audit mirror | `nexus_server.db.sync_events` | Server (transitional) |
| Pre-S4 anchor history | `nexus_server.db.sync_anchors` | Server (legacy, read-only) |
| Twin chain activity log | `nexus_server.db.twin_chain_events` | Server (Bug 3 visibility) |

## Three IDs, one user

This trips everyone up. There are three identifiers in play:

| Name | Type | Where issued | Lifetime |
|---|---|---|---|
| `user_id` | UUID string | Server `auth.register` | Per server account |
| `agent_id` | string `user-{user_id[:8]}` | Server `twin_manager._agent_id_for` | Per twin instance (matches user 1:1) |
| `token_id` | int (ERC-8004) | BSC `IdentityRegistry.register` | Forever, on-chain |

Mapping:

- `user_id` ←→ `token_id`: stored in `users.chain_agent_id` column.
- `user_id` → `agent_id`: derived (first 8 chars).

The local SQLite paths are `user_id`/`agent_id`-keyed
(server's own convention). Chain registrations are token-id keyed
(forever). See [`docs/concepts/identity.md`](docs/concepts/identity.md)
for the full mapping diagram.

## What the four layers cost

| Layer | Lines | Test count | Test runtime |
|---|---|---|---|
| SDK | ~10k | 271 (post-distill) | ~0.2s |
| Nexus | ~6k | 192 | ~0.6s |
| Server | ~3k | 65 | ~3s |
| Desktop | ~5k C# | (manual) | n/a |

A full test pass on Python is under 4 seconds.

## See also

- [`HISTORY.md`](HISTORY.md) — how we got to this architecture
- [`docs/concepts/`](docs/concepts/) — the five core mental models
- [`docs/how-to/`](docs/how-to/) — step-by-step recipes
- Per-package READMEs at `packages/{layer}/README.md`
