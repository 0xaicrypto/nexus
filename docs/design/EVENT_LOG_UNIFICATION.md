# Event Log Unification — Phase 2 Migration

> **Status**: in progress (this branch).
> **Owner**: see git blame.
> **Related**: ARCHITECTURE.md §"Where data lives", commit history around E14 dual-write.

## Why this exists

The server has two event stores (see chat with user on this branch for the
full archaeology):

| Store | Path | Schema | Writers | Readers |
|---|---|---|---|---|
| **Shared** (canonical going forward) | `nexus_server.db` table `twin_event_log` | `(event_idx, event_kind, event_kind_version, user_id, patient_hash, ts_us, payload_json, caused_by)` | `event_sourcing.Store.emit_and_apply` (chat_router_v2, research_router, clinical_graph handlers, …) | `chat_ingester._concat_source_text`, any new event-sourcing projection |
| **Per-user file** (legacy) | `~/.nexus_server/twins/{user_id}/event_log/{agent_id}.db` table `events` | `(idx, timestamp, event_type, content, metadata, agent_id, session_id)` | `twin_event_log.append_event` (workflows_router, RunWorkflowTool); `_mirror_chat_to_per_user_log` (E14 band-aid) | `twin_event_log.list_messages` (= `/api/v1/agent/messages` endpoint, used by desktop history pane + `_recent_history_messages` LLM-context loader) |

The split makes desktop history pane + LLM history context **silently
empty** for any event that didn't get dual-written. That's the bug E14
just papered over.

## Endgame

```
            ┌──────────────────────────────┐
            │  twin_event_log (SHARED)     │
            │  · canonical source of truth │
            │  · every event kind          │
            │  · indexed by user_id        │
            └──────────────┬───────────────┘
                           │
       ┌───────────────────┼───────────────────────┐
       ▼                   ▼                       ▼
  list_messages       chat_ingester        clinical_graph
  (history pane,      (entity extraction)  (M3 projection)
   LLM context)
                           │
                           ▼
              per-user file (derived export only,
                             chain backup, GDPR
                             "give me my data")
```

## Schema mapping

The two tables encode the same conceptual rows differently. The unified
reader needs a one-to-one translation:

| Per-user `events` field | Shared `twin_event_log` source |
|---|---|
| `idx` | `event_idx` |
| `timestamp` (seconds float) | `ts` (microseconds int) → divide by 1e6 |
| `event_type` | `event_kind` (already same enum) |
| `content` | `payload_json -> 'text'` |
| `metadata` | other `payload_json` fields, projected into a dict |
| `session_id` | `payload_json -> 'session_id'` |
| `agent_id` (`user-{user_id[:8]}`) | derived from `user_id` |

Both tables agree on event_kind names (`user_message`,
`assistant_response`, `workflow_run`, …) so no taxonomy change.

## Migration phases

### Phase 2a — read path (this commit)

1. Rewrite `twin_event_log.list_messages` to query the shared
   `twin_event_log` table via `nexus_server.database.get_db_connection`,
   filtering by `user_id`, `event_kind IN (user_message,
   assistant_response, workflow_run)`, and (when supplied) the
   `session_id` field inside `payload_json`.
2. Same for the helpers `_recent_history_messages`,
   `list_messages_after`, the count helpers that back the UI's session
   list ("12 msgs · 40s ago" badges).
3. Keep the per-user file readable (don't delete it) — Phase 2b will
   prune writes; if we delete the read fallback now and Phase 2b
   regresses, we lose history.

### Phase 2b — write path (next, small follow-up)

1. Migrate the two remaining write sites that still target the per-user
   file directly:
   - `nexus_server.tools_workflow.RunWorkflowTool` (writes
     `event_type='workflow_run'` cards mid-chat).
   - `nexus_server.workflows_router.start_run_in_chat_endpoint`
     (same kind, different entry point).
   Replace `twin_event_log.append_event(...)` with
   `Store.emit_and_apply(kind=EventKind.WORKFLOW_RUN, payload={...},
   apply_fn=_h_workflow_run, ...)`. Add the matching apply_fn (no-op,
   chat substrate) + register in EVENT_REGISTRY.
2. Remove the E14 dual-write helper
   (`event_sourcing.handlers._mirror_chat_to_per_user_log`) — Phase 2a
   makes it redundant. `_h_user_message` / `_h_assistant_response` go
   back to `pass` (they're chat substrate; no projection needed).

### Phase 2c — backup path (deferred, can wait until SaaS)

Per-user file becomes a derived export. A periodic job (or "Export my
data" button) reads the shared table filtered by `user_id` and emits a
fresh per-user SQLite. For chain mode, this same exporter feeds
Greenfield uploads.

## Multi-tenant implications

The shared table already has the right shape for multi-tenant:
- `user_id` is on every row → row-level filter is one `WHERE` clause.
- WAL mode lets many concurrent readers, one writer — fine until ~100
  concurrent chat turns/sec, then we shard by user_id range or move to
  Postgres.
- Per-user data isolation is enforced at the API layer
  (`get_current_user` Depends in every router); the table itself is
  shared but no router lets one user query another's rows.

This is the standard "multi-tenant single DB" pattern (Slack / Linear /
Notion all run this way). The earlier "per-user file" approach was
inherited from the chain-first design where each user's twin had its
own ERC-8004 token + Greenfield bucket; that's still respected, but the
**read path** is no longer dependent on it.

## Regression test plan

After Phase 2a:

1. **Desktop history pane** loads on patient open. Send a message, switch
   patient, switch back — message should still be there.
2. **Cross-research chat** history persists across EmptyState mount /
   unmount cycles.
3. **`_recent_history_messages`** in `yield_t3_llm`: the LLM's
   `history=N` log line should be >0 after the second turn of any
   chat. If `history=0`, Phase 2a's filter is wrong.
4. **chat_ingester** still extracts entities (it reads from the shared
   table directly, unaffected).
5. **workflow_run cards** still render in chat (writes still go to
   per-user file in Phase 2a — they'll move in Phase 2b). Make sure
   list_messages still surfaces them via the per-user fallback before
   we cut it.

After Phase 2b:

6. Workflows → run-in-chat card → appears in chat history immediately
   (single-DB write).
7. Delete E14 + smoke-test all four chat surfaces (CrossPatient,
   PatientMode encounter, Research per-study ChatTab, CrossResearch).

## Rollback

If Phase 2a regresses, revert just the one function (`list_messages`).
The per-user files still exist on disk, so no data loss. Phase 2b is
independently revertable.
