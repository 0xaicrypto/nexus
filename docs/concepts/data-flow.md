# Data flow — one chat turn, end to end

This doc traces a single chat round-trip across all four layers, byte
by byte. If you're trying to figure out "where does X happen", this is
the file to read.

Assumes:

- User has a registered server account.
- `SERVER_PRIVATE_KEY` is set (chain mode).
- User has been chatting for a while (twin already exists in memory,
  has an ERC-8004 token).

## Stage 1 — desktop sends

User types "hello" and presses Enter.

```
ChatViewModel.SendMessageAsync
  ├── Optimistic UI: append user message to in-memory list
  ├── Build request:
  │     ChatRequest {
  │         Messages = [{ role="user", content="hello" }],
  │         SystemPrompt = null,        // server's twin owns prompts
  │         Attachments = [],
  │         ToolDefinitions = [],
  │     }
  └── _api.SendChatAsync(req)
        └── HTTP POST /api/v1/llm/chat
              Authorization: Bearer <JWT>
              Content-Type: application/json
              Body: { messages: [...], system_prompt: null, ... }
```

Desktop holds nothing about the message after sending — server is the
source of truth.

## Stage 2 — server receives

```
nexus_server.main FastAPI
  ├── middleware: CORS + rate limit
  ├── auth.get_current_user (JWT verify) → user_id
  └── llm_gateway.llm_chat(request, current_user=user_id)
        ├── Rate limit check (RATE_LIMIT_LLM_REQUESTS_PER_MINUTE)
        ├── _validate_attachment_total([])  ← no-op for empty
        ├── _twin_enabled() → True (USE_TWIN=1 default)
        │
        ├── Extract last user message
        │     last_user_msg = "hello"
        │
        └── twin = await get_twin(user_id)
              └── TwinManager.get_twin(user_id):
                    ├── Cache hit? sess = _sessions[user_id]
                    │   ├── if yes: sess.touch(); return sess.twin
                    │   └── if no: cold start (see Stage 2b below)
                    └── return DigitalTwin instance
              reply = await twin.chat("hello")
              return LLMChatResponse { content=reply, model="twin", … }
```

### Stage 2b — cold-start path (only if twin not cached)

```
TwinManager._create_twin(user_id):
  ├── chain_kwargs = _resolve_chain_kwargs(user_id)
  │     ├── SERVER_PRIVATE_KEY set? yes
  │     ├── chain_active_rpc? yes
  │     ├── _read_chain_agent_id(user_id) → 866 (cached from prior chat)
  │     └── return { private_key, network="testnet", rpc_url,
  │                  agent_state_address, identity_registry_address,
  │                  task_manager_address }
  │
  ├── DigitalTwin.create(
  │       agent_id="user-22183952",
  │       cached_agent_id=866,         ← Bug 2 fix: skip re-register
  │       …chain_kwargs)
  │     └── In SDK: nexus_core.testnet(private_key=...)
  │           ├── ChainBackend.__init__:
  │           │     ├── local data store at NEXUS_CACHE_DIR
  │           │     └── BSCClient(rpc_url, private_key, ...)
  │           └── return AgentRuntime(backend=ChainBackend)
  │
  ├── twin._save_identity_cache(866, wallet) ← Bug 2: pre-seed
  ├── twin._initialize():
  │     ├── identity check (cache hit, no chain call)
  │     ├── evolution.initialize() — load persona, skills, knowledge
  │     ├── restore last session checkpoint from the data store
  │     └── ProjectionMemory + EventLogCompactor wired
  │
  └── twin.on_event = _build_on_event(user_id)
      _sessions[user_id] = TwinSession(twin)
```

Cold start takes a few seconds on first ever chat (BSC ID lookup +
session restore). Subsequent users hit the `_sessions` cache and skip
all of this.

## Stage 3 — twin.chat (the 9 steps)

Inside `DigitalTwin.chat("hello")`:

```
1. ContractEngine.pre_check("hello")
   └── No regex hits, no hard rules matched
       pre.blocked = False  → continue

2. event_log.append("user_message", "hello", session_id="session_ab12")
   ├── SDK EventLog: INSERT INTO events (...)
   │     SQLite at ~/.nexus_server/twins/{user_id}/event_log/user-22183952.db
   ├── ChainBackend.store_json:
   │     └── _cache_write(...) — synchronous local data store write

3. Build context:
   event_count = self.event_log.count() = 67
   ├── 67 > 50, no recall keyword in "hello"
   └── evo_context = curated_memory.get_prompt_context()
       ↑ reads ~/.nexus_server/twins/{user_id}/curated_memory.md (zero LLM)

   Maybe trigger background compact:
   if compactor.should_compact(turn_count=33):
     bg_task("auto-compact", _auto_compact())  ← runs in parallel

4. Build system prompt:
   persona = evolution.get_current_persona()
   system = persona
            + capabilities  + identity_context (token_id=866, network=testnet,
                                                wallet, contract addrs)
            + memory snapshot (FACTS / CONTEXT / USER_PROFILE)
            + skill index
            + tool instructions

   Tools registered: web_search, read_url, generate_file, read_uploaded_file

   await llm.chat(messages=[..., {"role":"user","content":"hello"}],
                  system=system,
                  tools=self.tools)
   ├── google.genai.GenerativeModel.generate_content(...)
   ├── If LLM returns tool_call: server executes tool, feeds result back
   └── Returns: "Hi there! How can I help you today?"

5. ContractEngine.post_check(response)
   └── No regex hits → post.hard_violation = False

6. DriftScore.update(hard_score=1.0, soft_score=1.0, "chat")

7. event_log.append("assistant_response", response, session_id=...)
   └── Same as step 2: SQLite + data-store write

8. on_event mirror — IF twin emits any event during chat (e.g.
   memory_compact background fire), _build_on_event hook writes a row
   to nexus_server.db.sync_events with the canonical event_type. Lets
   /agent/state's mirror-aware reads see it.

9. Background _post_response_work:
   ├── evolution.after_conversation_turn(messages):
   │     ├── MemoryEvolver: extract memory items from this turn
   │     ├── SkillEvolver: detect new skills used
   │     └── (every 10 turns) PersonaEvolver.trigger_reflection()
   ├── Save session checkpoint:
   │     └── Checkpoint to the data store (chain mode) — durable
   └── (occasionally) ChainBackend.compute_state_root() →
                       BSCClient.update_state_root(token_id, hash)
                       log: "[WRITE][BSC] Anchor OK: agent=user-...
                                                hash=... tx=0x..."
                       ↑ captured by _ChainActivityLogHandler too
```

Returns `"Hi there! How can I help you today?"`.

## Stage 4 — server response

```
LLMChatResponse {
    role: "assistant",
    content: "Hi there! How can I help you today?",
    model: "twin",
    stop_reason: "stop",
    tool_calls_executed: [],
    attachment_summaries: [],
}
```

Sent back as JSON to the desktop.

## Stage 5 — desktop renders

```
ChatViewModel.SendMessageAsync (continuation):
  ├── reply = await _api.SendChatAsync(...).Reply
  ├── Messages.Add(ChatMessage.Assistant(reply))   ← UI updates
  └── TurnCount += 1
```

That's it. No local persistence. If the user reloads the desktop, history
re-fetches from `GET /api/v1/agent/messages` (Round 2-A).

When the user opens the **Brain panel** (Phase D 续 / #159), the
desktop fans out two parallel reads:

- `GET /api/v1/agent/chain_status` — per-namespace 3-state status
  (`local` / `mirrored` / `anchored`) plus a chain-health card
  (BSC anchor readiness).
- `GET /api/v1/agent/learning_summary?window=7d` — 7-day timeline
  + data-flow stage snapshot + just-learned feed.

Both endpoints are pure projections over the typed namespace stores +
EventLog window — no LLM calls, no chain traffic.

## What happens in parallel (the background fan-out)

After Stage 3 returns the reply, three things continue:

1. **BSC anchors** (if scheduled) are still in flight.
2. **Auto-compact** (if triggered): another LLM call producing a fresh
   `memory_compact` event, writes to event_log + emits
   `memory_compact` via on_event → twin_manager mirror writes
   `sync_events` row → `/agent/memories` next reads it.
3. **Self-evolution** (every chat): MemoryEvolver / SkillEvolver run.
   Every 10 turns: PersonaEvolver fires, may rewrite persona.
4. **State-root anchor** (periodic, not every turn): BSC `updateStateRoot`
   tx. Costs gas. Captured by log handler →
   `twin_chain_events.kind='bsc_anchor', status='ok'` row.

The desktop sees the reply at Stage 5 even if all of (1)–(4) are still
running. Polling endpoints (`/agent/state` every 15s, `/agent/timeline`
every few seconds) will pick up the resulting events as they land.

## Latency budget (rough)

| Step | Time |
|---|---|
| HTTP request → server | ~10ms LAN |
| JWT verify + middleware | ~1ms |
| Twin cache hit | ~0ms |
| event_log append (local SQLite) | ~1ms |
| Build context (no LLM, just file read) | ~5ms |
| LLM call (Gemini 2.5 Flash, "hello") | ~1500ms |
| Post-check + drift | ~1ms |
| event_log append response | ~1ms |
| HTTP response → desktop | ~10ms |
| **Total visible latency** | **~1.5s** |
| Background BSC anchor (if fires) | ~1.5s |

## What's NOT happening on each turn

- BSC anchor — only periodic (every N turns OR when batch grows large enough).
- Auto-compact — only every ~20 turns AND when log >30k chars.
- Persona reflection — only every 10 turns.
- Identity registration — only first time the user ever chats.

## File pointers (for grep)

| Stage | Where |
|---|---|
| Stage 1 (desktop) | `packages/desktop/RuneDesktop.UI/ViewModels/ChatViewModel.cs` |
| Stage 2 (server route) | `packages/server/nexus_server/llm_gateway.py:llm_chat` |
| Stage 2b (twin cold start) | `packages/server/nexus_server/twin_manager.py:_create_twin` |
| Stage 3 (twin.chat 9 steps) | `packages/nexus/nexus/twin.py:chat` |
| Step 2/7 (event_log + storage) | `packages/sdk/nexus_core/memory/event_log.py`, `packages/sdk/nexus_core/backends/chain.py` |
| Step 3 (memory projection) | `packages/nexus/nexus/evolution/projection.py`, `packages/sdk/nexus_core/memory/curated.py` |
| Step 5 (post-check) | `packages/sdk/nexus_core/contracts/engine.py` |
| Step 9 (evolution) | `packages/nexus/nexus/evolution/engine.py` |
| Brain panel chain status | `packages/server/nexus_server/agent_state.py:chain_status` |
| Brain panel learning summary | `packages/server/nexus_server/agent_state.py:learning_summary` |
| VersionedStore chain_status helper | `packages/sdk/nexus_core/memory/versioned.py:VersionedStore.chain_status` |
| Bug 3 chain log capture | `packages/server/nexus_server/twin_manager.py:_ChainActivityLogHandler` |
