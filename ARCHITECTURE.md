# Architecture

> **Read this together with
> [`docs/design/RESEARCH_WORKSPACE_DESIGN.md`](docs/design/RESEARCH_WORKSPACE_DESIGN.md).**
> The design doc is the *what* and *why* of the product (research-first
> clinical workstation, 4 axes of accumulation, anti-pattern list). This
> doc is the *how* — four layers of code that together implement it.

Four layers, single-direction dependency. Read top-down — each layer is
explained in terms of what it adds to the layer below.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Desktop v2 (packages/desktop-v2)         — Tauri 2.0 + React + TS   │
│    UI only. Auth token in sessionStorage, user_id in localStorage.   │
│    Spawns the server as a sidecar (src-tauri/lib.rs) and talks to    │
│    it over HTTP / SSE. No on-disk data of its own.                   │
└────────────────────────┬─────────────────────────────────────────────┘
                         │ HTTP + JWT, SSE for /agent/chat
                         ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Server (packages/server, nexus_server)         — FastAPI / Python   │
│    Multi-tenant HTTP frontend + clinical workflow engine.            │
│    Persistence: rune_server.db (auth, patients, studies, DICOM,      │
│      event-sourcing graph, billing, async tasks, vector index)       │
│      + per-user twin event_log SQLite under ~/.nexus_server/twins/.  │
└──────┬───────────────────────────────────────┬───────────────────────┘
       │                                       │
       │ Per-user agent abstraction            │ Direct (rare:
       │ (TwinManager.get_twin(user_id))       │  bootstrap, distill)
       ▼                                       │
┌──────────────────────────────────┐           │
│  Nexus (packages/nexus)          │           │
│    DigitalTwin class.            │           │
│    9-step chat flow (legacy      │           │
│    /api/v1/llm/chat path).       │           │
│    Self-evolution (persona /     │           │
│    skills / memory / knowledge / │           │
│    social).                      │           │
└────────────────┬─────────────────┘           │
                 │                             │
                 │ Uses every primitive        │
                 ▼                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│  SDK (packages/sdk, nexus_core)                  — Python            │
│    Entry points: testnet() / mainnet() / local() → AgentRuntime      │
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

A separate `packages/relay` Python service (Fly.io) provides webhook /
outbound-email plumbing the desktop sidecar can't (or shouldn't) run.
Schemas drift between server and relay are tracked in ROADMAP §D.

## Dependency direction

Imports flow strictly downward. Verified:

- **SDK** imports from neither Nexus nor Server. (`grep "from nexus\|from nexus_server" packages/sdk/` returns nothing.)
- **Nexus** imports from SDK only. (`from nexus_core import ...`.)
- **Server** imports from Nexus + (rarely) SDK directly for utilities like
  `distill`, `BSCClient`.
- **Desktop v2** talks to Server over HTTP only.

This invariant is the single most important property of the architecture.
A PR that adds an upward import (e.g. SDK importing Nexus) should be
rejected on principle — that direction lock is what lets us version each
layer independently.

A previous Avalonia / .NET desktop (`packages/desktop`) was removed when
desktop-v2 reached parity; it remains available at git tag
`legacy/avalonia-final` for historical reference.

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
the 9-step chat flow used by the legacy `/api/v1/llm/chat` path, when to
compact memory, when to evolve persona, when to learn skills, how to
project relevant memory for a turn.

**Doesn't know about**: HTTP, JWT, multi-tenancy. It's a Python class. You
hand it config + private_key, it gives you `.chat()`.

**Public entry point**: `DigitalTwin.create(...)` returns an initialised
twin. `await twin.chat("hello")` returns the assistant's reply.

**Why it exists separately from Server**: the same DigitalTwin can be
embedded in a CLI, a Telegram bot, or a peer-to-peer agent runtime. HTTP
is just one way to expose it.

### Server — `packages/server/nexus_server/`

**Knows about**: HTTP routes, JWT verification, username + password
auth (bcrypt), multi-tenancy (one twin per user), rate limiting, CORS, the desktop's
view-shape API endpoints, the desktop's onboarding flow, and the entire
clinical workflow stack: patient registry, DICOM ingestion + viewer
bridge, MONAI segmentation runtime, the clinical event-sourcing graph,
research studies + roster + protocol parser, scheduler, billing
(Stripe), the SSE tier-classified chat router, vector index for memory
retrieval, and live thinking streams.

**Doesn't know about**: how the legacy `/llm/chat` turn works inside
(delegated to `await twin.chat(...)`), how anchoring works (delegated to
twin's ChainBackend), how memory is structured at the SDK level (it just
opens twin's SQLite read-only).

**Public entry point**: `uvicorn nexus_server.main:create_app --factory`
(the application is built by `create_app()` — there is no top-level
`app` variable). The Tauri sidecar binary launches via the `nexus-server`
console script registered in `pyproject.toml`.

**Why this layer exists**: agents need a multi-tenant, authenticated HTTP
front so a desktop / web / mobile UI can hit them without each twin owning
its own port. Server is also where every cross-twin concern lives —
billing, scheduling, clinical graph projection, etc.

### Desktop v2 — `packages/desktop-v2/`

**Knows about**: rendering chat (Today / Patient / Research workspace
modes), DICOM viewer launch, file picker UI, polling endpoints for status,
username + password authentication on launch.

**Doesn't know about**: chat history (pulled from server every login),
memories (rendered from server), anchors (rendered from server), agent
identity (read from server).

**Why it's a thin client**: server's twin is the single source of truth;
desktop is a view layer. The bundled `.dmg` launches the FastAPI server
as a Tauri sidecar on `127.0.0.1:8001` and `pnpm tauri:dev` does the same
with hot reload over Vite.

## Two chat paths

The server exposes **two** chat endpoints. They share a database but use
different flows; new code should target the v2 path.

| Path | Endpoint | Transport | Pipeline | Used by |
|---|---|---|---|---|
| Legacy | `POST /api/v1/llm/chat` | JSON request / JSON response | `twin.chat()` 9-step (pre-check → event_log append → project memory → llm.chat → post-check → DriftScore → event_log append → on-event mirror → background evolution) | Tests, CLI, the legacy Avalonia client at tag `legacy/avalonia-final` |
| v2 | `POST /api/v1/agent/chat` | JSON request / SSE response | tier classification → multi-tier retrieval (`retrieval_tiers.retrieve_async`) → reasoning chunks → final answer chunks → citations → turn-complete | Desktop v2's Today / Patient / Research chat panels |

The v2 SSE schema is the typed `ChatStreamChunk` discriminated union
declared in `packages/desktop-v2/src/lib/types.ts` (15+ frames including
`turn_started`, `tier_classified`, `reasoning_chunk`, `web_search_*`,
`final_answer_chunk`, `citations`, `scheduled_task_proposed`,
`turn_complete`, `error`). **Every frame's discriminator is `type`** —
never `kind`. The server's chunk dataclass renames its internal `.kind`
field to `"type"` at the wire boundary in `chat_router.py`, so the
front-end sees a uniform shape.

A third (orthogonal) SSE channel, `/api/v1/agent/thinking/stream`
(`thinking_stream.py`), broadcasts the agent's live cognition events
(memory_recall, reasoning, tool_call, …) for debugging panels. Those
frames currently use a `kind` discriminator. Desktop v2 doesn't consume
this channel today.

## Data flow: one v2 chat turn

```
desktop-v2  ──POST /api/v1/agent/chat──▶  chat_router.chat
                                              │
                                              ▼
                                  event_log.append("user_message", …)
                                              │
                                              ▼
                                  tier_classified  (yield SSE)
                                              │
                                              ▼
                              retrieve_async(scope, patient_hash, text)
                                  ├─▶ reasoning_chunk*   (yield SSE)
                                  ├─▶ web_search_started/results  (if Tavily)
                                  └─▶ final_answer_chunk*  (yield SSE)
                                              │
                                              ▼
                                  citations  (yield SSE)
                                              │
                                              ▼
                                  event_log.append("assistant_response", …)
                                  + chat_ingester (background) extracts
                                    clinical entities into event_sourcing
                                              │
                                              ▼
                                  turn_complete  (yield SSE)
```

The legacy `/llm/chat` 9-step flow is documented inline in
`packages/server/nexus_server/llm_gateway.py`.

## Where data lives

Server-owned tables (all in `rune_server.db` under the configured
`DATABASE_URL`):

| Concern | Where |
|---|---|
| Users + JWT secrets | `users` |
| Per-user twin event log | `~/.nexus_server/twins/{user_id}/event_log/{agent_id}.db` (managed by SDK's EventLog) |
| Per-user CuratedMemory snapshot | `~/.nexus_server/twins/{user_id}/curated_memory.md` |
| Per-user persona evolution history | `~/.nexus_server/twins/{user_id}/persona.json` |
| Per-user contracts + drift state | `~/.nexus_server/twins/{user_id}/contracts/` |
| Patients + their identifiers | `patients`, `patient_*` |
| DICOM studies / series / instances | `dicom_studies`, `dicom_series`, `dicom_instances`, `dicom_*` |
| Clinical event-sourcing graph | `event_sourcing.*` (events + entity / relation projections + summaries) |
| Research studies, rosters, candidates | `studies`, `roster`, `candidates`, `protocols` (see `research/` subpackage) |
| Memorization queue (`pending_memories`) | `memorization.*` |
| Vector index for memory retrieval | `vector_index` (sqlite-vec extension when available) |
| Billing (Stripe) | `billing_*` |
| Async tasks / progress | `async_tasks` (separate file under `~/.nexus_server/`) |
| Scheduled future-action proposals | `scheduled_tasks` |
| Chain mode: durable event store | `NEXUS_CACHE_DIR` local content-addressed store (S3-compatible mirror planned) |
| Chain mode: state-root hashes | BSC `AgentStateExtension` per token |
| Identity registration | BSC ERC-8004 IdentityRegistry |
| Pre-S4 anchor history (read-only) | `sync_anchors` |
| Twin chain activity log | `twin_chain_events` |

The first `nexus-server` boot calls `init_event_sourcing_schema()` and a
set of `migrations/` modules to bring the file up to current schema.

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

## See also

- [`HISTORY.md`](HISTORY.md) — how we got to this architecture (currently
  fronted-up to Phase F; the M0..M4 clinical pivot still needs writing up)
- [`docs/concepts/`](docs/concepts/) — the core mental models
- [`docs/how-to/`](docs/how-to/) — step-by-step recipes
- Per-package READMEs / ARCHITECTUREs at `packages/{layer}/`
