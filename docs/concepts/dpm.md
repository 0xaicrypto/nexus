# DPM — Deterministic Projection Memory

The agent's memory model. Lives in SDK
(`packages/sdk/nexus_core/memory/`).

## The one-sentence version

> The event log is the only durable store. "Memory" is a function that
> reads the log and returns what's relevant for the current turn.

## Why this design

The naive memory model is a separate "memory store" — a database of
"things the agent remembers", written to and pruned as the agent runs.
That model has three failure modes:

1. **State divergence** — what the agent thinks it remembers and what
   it has access to drift over time.
2. **Compaction is destructive** — once you "summarise" 100 events into
   1 sentence, the original detail is gone.
3. **Hard to audit** — you can't reconstruct what the agent knew at any
   given moment.

DPM solves all three by inverting the model: **append-only log + pure
projection**. The agent never deletes anything; it just chooses which
slice of the log to render into the current prompt.

## The three components

```
        ┌─────────────────────┐
   write│                     │
  ──────▶  EventLog (SQLite)  │   ← append-only, single source of truth
        │                     │
        └──────────┬──────────┘
                   │ read
                   ▼
        ┌─────────────────────┐
        │ EventLogCompactor   │   ← decides "is it time to compact?"
        │                     │     calls projection_fn, writes one
        │                     │     memory_compact event back to log
        └──────────┬──────────┘
                   │
                   ▼
        ┌─────────────────────┐
        │ projection function │   ← LLM call: read log → output
        │ (caller-supplied)   │     structured FACTS / CONTEXT /
        │                     │     USER_PROFILE summary
        └──────────┬──────────┘
                   │
                   ▼
        ┌─────────────────────┐
        │ CuratedMemory       │   ← derived view: the latest
        │ (MEMORY.md +        │     compaction's output, materialised
        │  USER.md files)     │     as files for fast prompt-time read
        └─────────────────────┘
```

### EventLog (`memory/event_log.py`)

Append-only SQLite. Schema:

```sql
CREATE TABLE events (
    idx          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    REAL NOT NULL,
    event_type   TEXT NOT NULL,    -- 'user_message' | 'assistant_response' |
                                   -- 'memory_compact' | 'attachment_distilled' | …
    content      TEXT NOT NULL,
    metadata     TEXT DEFAULT '{}',
    agent_id     TEXT NOT NULL,
    session_id   TEXT DEFAULT ''
);
```

Storage is on disk per agent at
`{base_dir}/event_log/{agent_id}.db`. Read APIs: `recent(limit, session_id)`,
`search(query)`, `count()`, `get_trajectory(session_id)`.

In chain mode, every `event_log.append(...)` is persisted through
`ChainBackend.store_json` — a synchronous write to the backend's
durable local store.

### EventLogCompactor (`memory/compactor.py`)

Triggers compaction when both:
- `turn_count >= self.compact_interval` (default 20), AND
- the event log has grown enough since the last compaction
  (`COMPACT_THRESHOLD` chars).

When triggered, it calls `projection_fn(events) -> str` (the caller
supplies it), wraps the result in a synthetic `memory_compact` event,
and appends it back to the log. So a compaction is itself an event —
recursively projectable.

### Projection function (Nexus's `evolution/projection.py`)

The actual LLM call that turns "120 raw events" into "## FACTS / ##
CONTEXT / ## USER_PROFILE" Markdown. Three sections, terse, ~1500
characters total. The function is dependency-injected — SDK's compactor
doesn't import an LLM, the caller does.

### CuratedMemory (`memory/curated.py`)

Materialised view of the latest projection. Two files:
- `MEMORY.md` — the structured FACTS / CONTEXT block.
- `USER.md` — the USER_PROFILE block (preferences, communication style,
  recurring requests).

Why two files and not one: the prompt-builder injects both as separate
sections in the system prompt, with USER profile taking different
priority than current-turn context. Splitting the storage matches the
splitting at consumption.

## The chat-time read strategy (Nexus's `twin.chat`)

```python
event_count = self.event_log.count()

if needs_recall and event_count > 5:
    # Explicit recall ("do you remember when…") — synchronous projection,
    # one LLM call, ~8s timeout. Returns a fresh summary tailored to the
    # current question.
    evo_context = await self._projection.project(user_message, budget=2000)

elif event_count > 10:
    # Mid/long session — read the cached CuratedMemory snapshot.
    # Zero ms, no LLM call.
    evo_context = self.curated_memory.get_prompt_context()

# Auto-compact: if EventLogCompactor.should_compact() is true, fire a
# background task that does a fresh projection and updates CuratedMemory.
if self._compactor.should_compact(self._turn_count):
    self._bg_task("auto-compact", self._auto_compact())
```

So a "memory" the user sees in the UI is a slice of the event log,
re-derived on demand. The same log can be projected differently per
turn — explicit recall ≠ background compact.

## The anchor

Every event_log append in chain mode → durable data-store write.
Periodically the agent's ChainBackend computes a content hash over
recent state and calls `AgentStateExtension.updateStateRoot(token_id,
hash)` . A third party with read access to the bucket can
recompute the hash and verify the agent's state is what it claims.

Crucially, the anchor is over the **log**, not over a
projection. Whoever holds the bucket can replay every projection the
agent ever computed if they want to — the projections themselves are
events in the log too (`memory_compact`).

## When you'd touch each component

| You want to… | Touch |
|---|---|
| Add a new event type | `EventLog` schema is generic — just pass a new `event_type` string. No code change. |
| Change when compaction triggers | `EventLogCompactor.compact_interval` / `COMPACT_THRESHOLD` |
| Change what compaction produces | The `projection_fn` you pass to `EventLogCompactor`; in Nexus it's `ProjectionMemory.project` |
| Change the prompt-time read strategy | `twin.chat` (Nexus) — the `if/elif` block above |
| Add a new section to the curated snapshot | `CuratedMemory` API + the projection prompt |

## File pointers

- `packages/sdk/nexus_core/memory/event_log.py` — EventLog
- `packages/sdk/nexus_core/memory/curated.py` — CuratedMemory
- `packages/sdk/nexus_core/memory/compactor.py` — EventLogCompactor
- `packages/nexus/nexus/evolution/projection.py` — ProjectionMemory
- `packages/nexus/nexus/twin.py` — chat-time orchestration

## See also

- [ABC](abc.md) — the safety layer that runs alongside DPM
- [data-flow](data-flow.md) — how DPM fits in the end-to-end chat
