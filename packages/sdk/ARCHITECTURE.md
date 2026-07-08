# Nexus SDK — Architecture

## Project Structure

```
bnbchain_agent/
  memory/             # DPM: EventLog (SQLite+FTS5) + CuratedMemory
    event_log.py      #   Append-only event log with full-text search
    curated.py        #   Hermes-style MEMORY.md + USER.md
  contracts/          # ABC: Agent Behavioral Contracts
    spec.py           #   YAML contract definition + user rules
    engine.py         #   Runtime enforcement (pre/post check)
    drift.py          #   Behavioral drift score (compliance + distributional)
  tools/              # Tool framework
    base.py           #   BaseTool, ToolResult, ToolRegistry
    web_search.py     #   WebSearchTool (Tavily)
    url_reader.py     #   URLReaderTool (Jina)
  mcp/                # MCP client (Model Context Protocol)
    client.py         #   MCPClient (stdio), MCPManager, MCPServerConfig
  skills/             # Skill management
    manager.py        #   Install from GitHub, LobeHub Skills, LobeHub MCP
  core/               # Abstract interfaces
    providers.py      #   StorageBackend + 5-provider AgentRuntime ABCs
    models.py         #   Checkpoint, MemoryEntry, Artifact, Social models
    backend.py        #   StorageBackend base class
    flush.py          #   FlushPolicy, WriteAheadLog
  backends/           # Storage implementations
    local.py          #   File-based, no chain
    chain.py          #   Local data store + BSC anchoring
    mock.py           #   In-memory for tests
  providers/          # Domain-specific data managers
    session.py        #   SessionProviderImpl (with backend load)
    memory.py         #   MemoryProviderImpl (dirty tracking, bulk_add)
    artifact.py       #   ArtifactProviderImpl (rollback)
    task.py           #   TaskProviderImpl
    impression.py     #   ImpressionProviderImpl (social)
  adapters/           # Framework integrations
    adk.py            #   Google ADK
    langgraph.py      #   LangGraph
    crewai.py         #   CrewAI
    a2a.py            #   A2A Protocol
    a2a_task_store.py #   A2A TaskStore
    registry.py       #   AdapterRegistry
  social/             # Social protocol primitives
    gossip.py, graph.py, profile.py
  utils/              # Shared utilities
    json_parse.py     #   robust_json_parse (LLM output repair)
    dotenv.py         #   .env file loader
    agent_id.py       #   Agent ID to uint256 conversion
  builder.py          # local() / testnet() / mainnet() / builder() entry points
  state.py            # StateManager
  chain.py            # BSCClient (BSC contracts)
  keystore.py         # Keystore (encrypted wallet)
```

## Layered Architecture

```
┌───────────────────────────────────────────────────────┐
│  Contracts (ABC enforcement, drift detection)         │
├───────────────────────────────────────────────────────┤
│  Skills / MCP / Tools (capabilities)                  │
├───────────────────────────────────────────────────────┤
│  Memory (EventLog + CuratedMemory)                    │
├───────────────────────────────────────────────────────┤
│  Adapters (ADK, LangGraph, CrewAI, A2A)               │
├───────────────────────────────────────────────────────┤
│  Providers (Session, Memory, Artifact, Task)           │
├───────────────────────────────────────────────────────┤
│  Backends (Local, Chain, Mock)                         │
├───────────────────────────────────────────────────────┤
│  BNB Chain (BSC anchoring)                             │
└───────────────────────────────────────────────────────┘
```

Each layer depends only on the layer below. No circular dependencies.

## Memory Architecture (DPM)

Based on "Stateless Decision Memory for Enterprise AI Agents" (arXiv:2604.20158).

```
Conversation event → EventLog.append() [SQLite, instant]
                          │
                          ▼
Decision time → Projection π(E, T, B) [one LLM call]
                          │
                          ▼
                   Memory view M (FACTS + CONTEXT + USER_PROFILE)
                          │
                          ▼
                   Injected into system prompt
```

EventLog is the single source of truth. Events are never edited, summarized, or deleted. The projection is a pure function over the log — same log + same model = same output.

Enterprise properties: deterministic replay, auditable rationale (2 LLM calls vs 83-97), multi-tenant isolation, stateless scale.

### Auto-Compact (EventLogCompactor)

When the event log grows beyond 30K chars, the compactor triggers a background projection and writes the result back to the EventLog as a `memory_compact` event. This event is persisted like any other, and its state root is anchored on-chain.

```python
from bnbchain_agent.memory import EventLogCompactor

compactor = EventLogCompactor(event_log, curated_memory, projection_fn=my_llm)

if compactor.should_compact(turn_count=20):
    await compactor.compact(session_id="session_abc")
    # 1. Projection → appended to EventLog as memory_compact event
    # 2. CuratedMemory (MEMORY.md / USER.md) updated as derived view
```

## Object-Store Data Structure

The decentralised object-storage data plane (BNB Greenfield) has been
removed. ChainBackend persists data to a local content-addressed store
(``NEXUS_CACHE_DIR``); an S3-compatible mirror is planned as the
remote durability layer.

```
{NEXUS_CACHE_DIR}/
  agents__{agent_id}__event_log__snapshot.json   ← EventLog snapshots
  namespaces__{ns}__v{NNNN}.json                 ← typed-store versions
  files__{file_id}__{name}                       ← uploaded file blobs
```

EventLog events (including `memory_compact`) are the canonical data. Everything else is a derived view. On-chain verification: `SHA-256(stored_data) == bsc_state_root`.

## Contract Architecture (ABC)

Based on "Agent Behavioral Contracts" (arXiv:2602.22302).

Contract C = (P, I_hard, I_soft, G_hard, G_soft, R):

```
User message
    │
    ▼
Pre-check (Hard Governance) ──── Blocked? → Return error
    │
    ▼
LLM generates response
    │
    ▼
Post-check (Invariants) ──── Hard violation? → Recovery
    │                         Soft violation? → Track + recover in k steps
    ▼
Update Drift Score D(t) = w_c × compliance + w_d × distributional
    │
    ├── D(t) < θ₁ → normal
    ├── θ₁ < D(t) < θ₂ → warning
    └── D(t) > θ₂ → intervention
```

User-defined rules (from conversation) are persisted as soft constraints. Cannot override hard constraints.

## Data Persistence

```
Agent calls store_json(path, data)
    │
    └── Write to the local content-addressed store (synchronous,
        durable on return)
```

## Storage Split

**BSC**: Small tamper-proof commitments (32-byte SHA-256 hashes). ERC-8004/8183 identity registry.

**Object store**: Actual data (sessions, memories, artifacts). Content-addressed: hash(payload) = on-chain pointer.

Verification: `SHA-256(stored_data) == bsc_state_root`
