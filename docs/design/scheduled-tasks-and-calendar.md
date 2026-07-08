# Calendar & Scheduled Tasks — Design Proposal (Draft)

Status: **Proposed** (2026-06-14)
Owner: TBD
Related: `docs/design/nexus-architecture.md`, `packages/server/nexus_server/async_tasks.py` (v1)

---

## Motivation

The medic is mid-conversation with Nexus and naturally wants to delegate
future actions: "in two hours email Dr Smith the CT findings", "tomorrow
9am give me a brief on J.D.", "every Monday morning summarise pending
follow-ups". Today these intents have no surface — the medic has to set
their own reminder externally, then re-paste context into Nexus when
the time comes.

This design adds a thin scheduling layer that turns chat-stated future
intents into persisted, auditable, cancel-able tasks that fire on
schedule and surface their results back into the workflow.

## User journey (target)

```
Doctor: "两小时后帮我把刚才的 CT 发现发邮件给 dr.smith@hosp.org"
Nexus:  [identifies scheduling intent] → renders inline confirmation card:
        ┌─────────────────────────────────────────────┐
        │ 📅 Scheduled task                           │
        │ 14:32 (in 2h) · send_email                  │
        │ To: dr.smith@hosp.org                       │
        │ Subject: CT findings · J.D.                 │
        │ Body: [preview]                             │
        │                                             │
        │  [Confirm]  [Edit]  [Cancel]                │
        └─────────────────────────────────────────────┘
Doctor: [clicks Confirm]
Nexus:  ✓ Scheduled. I'll send it at 14:32 and let you know.
```

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│ chat_router_v2.chat (per turn, after ASSISTANT_RESPONSE)           │
│   → schedule_intent_extractor.extract(user_text)                    │
│     stage 1: regex prefilter for time tokens                        │
│     stage 2: structured LLM call (Gemini response_schema)           │
│   → if proposal: emit SCHEDULED_TASK_PROPOSED                       │
│     UI receives SSE event → renders inline confirmation card        │
└────────────────────────────────────────────────────────────────────┘
                              │
                              ▼ user confirms (button click)
┌────────────────────────────────────────────────────────────────────┐
│ POST /api/v1/schedule/confirm { proposal_id }                       │
│   → emit SCHEDULED_TASK_CREATED                                     │
│   → projection inserts into scheduled_tasks table                   │
└────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────┐
│ Background worker (started in main.py lifespan, asyncio.Task)       │
│   every 30s:                                                        │
│     SELECT * FROM scheduled_tasks                                   │
│      WHERE status='pending' AND fire_at <= now()                    │
│      ORDER BY fire_at ASC LIMIT 20                                  │
│   for each: status='running' → dispatch by kind → status='done'/   │
│                                                  'error'           │
│   on dispatch:                                                      │
│     send_email   → email_send.send_email_async(...)                │
│     chat_brief   → run a programmatic chat turn, store answer       │
│     reminder     → emit TASK_DUE event, UI polls / shows toast      │
│   emit SCHEDULED_TASK_FIRED (carries result_json)                   │
│   if recurrence_cron: croniter computes next fire_at, status←pending│
└────────────────────────────────────────────────────────────────────┘
```

## Three key decisions

### 1. Scheduling engine — build vs APScheduler

**Recommendation: build it ourselves**, extending the v1
`async_tasks.py` worker + SQLite queue pattern.

- v1 already has `_init_db()` + `start_worker()` — add `fire_at` and
  `recurrence_cron` columns, ~30 lines of polling code.
- Zero new heavy dependencies. The cron parsing comes from `croniter`
  (~50 KB, pure-Python).
- APScheduler would pull ~10 MB plus its own learning curve, and its
  persistent jobstore is overkill for our scale (≤50 tasks/medic).

### 2. Intent extraction — heuristic vs LLM

**Recommendation: two-stage.**

Stage 1 — Regex prefilter on `req.text`. Match common time tokens:

```
\b(明天|tomorrow|今天|today|后天|in \d+ (min|minute|hour|h|day|d|week|周)|
   每(周|月|日|day|week|month)|every (monday|tuesday|.../周\w+)|
   \d{1,2}:\d{2}|\d{1,2} ?(am|pm|点)|next \w+|周\w+)\b
```

90%+ of chat turns bypass LLM at this gate. Only matches reach Stage 2.

Stage 2 — Structured LLM call (Gemini `response_schema`):

```json
{
  "is_future_intent": true,
  "kind": "send_email",
  "fire_at_local": "2026-06-14T14:32:00",
  "recurrence_cron": null,
  "payload": { "to": "...", "subject": "...", "body": "..." },
  "user_tz": "Asia/Shanghai",
  "summary_zh": "两小时后给 Dr Smith 发邮件，主题 'CT 影像所见 · J.D.'"
}
```

Or `{ "is_future_intent": false }`. The model is told to err
conservative — "is this an instruction about the FUTURE" — to keep
false-positives manageable.

### 3. Persistence — event log + projection (Rev-8 pattern)

New `EventKind`s:

- `SCHEDULED_TASK_PROPOSED` — LLM proposed, not yet confirmed (audit:
  helps us measure LLM false-positive rate over time)
- `SCHEDULED_TASK_CREATED` — confirmed; projection inserts
- `SCHEDULED_TASK_FIRED` — worker ran it; carries `result_json`
- `SCHEDULED_TASK_CANCELLED` — user cancelled via UI

Projection table:

```sql
CREATE TABLE scheduled_tasks (
    task_id        TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    patient_hash   TEXT,                 -- nullable for cross-patient tasks
    session_id     TEXT,                 -- traceability back to the conversation
    kind           TEXT NOT NULL,        -- 'send_email' | 'chat_brief' | 'reminder'
    payload_json   TEXT NOT NULL,
    fire_at        INTEGER NOT NULL,     -- unix seconds (UTC)
    user_tz        TEXT NOT NULL,        -- 'Asia/Shanghai' etc, for display
    recurrence_cron TEXT,                -- NULL for one-shot
    status         TEXT NOT NULL DEFAULT 'pending',
    last_run_at    INTEGER,
    last_error     TEXT,
    result_json    TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    cancelled_at   INTEGER
);
CREATE INDEX idx_sched_status_fire ON scheduled_tasks (status, fire_at);
CREATE INDEX idx_sched_user        ON scheduled_tasks (user_id);
```

Add as Alembic migration `0003_scheduled_tasks.py`.

## UI surfaces

Four entry points, ordered by visibility:

1. **Chat inline confirmation card** — proposal emits SSE event,
   `EncounterMode` renders a card with Confirm / Edit / Cancel buttons.
2. **TodayMode header card** — "今日待办：3 个计划任务" with a
   collapsible list, pinned to top.
3. **New Calendar mode** (📅 tab) — full list of pending / recent /
   cancelled tasks, grouped by date, with patient filter + edit/cancel.
4. **AccountMenu entry** — "我的计划任务 (N)" jump-link.

The Calendar mode is Phase 2; Phase 1 can ship with just (1) and the
AccountMenu link.

## Three action kinds (Phase 1 starter set)

| Kind         | Payload                          | Execution                                                                    |
| ------------ | -------------------------------- | ---------------------------------------------------------------------------- |
| `send_email` | `{to, cc, subject, body}`        | Call `email_send.send_email_async` (already shipped). Result: relay message  |
| `chat_brief` | `{prompt, patient_hash?}`        | Run a programmatic chat turn server-side, persist the answer to result_json. |
| `reminder`   | `{text}`                         | Emit TASK_DUE, UI polls / shows toast + Today card.                          |

Easy follow-on kinds for Phase 2:
- `mdt_export` — multi-recipient PDF + email
- `followup_check` — query a patient's status delta since last check

## Safety policy

1. **Recipient allow-list re-checked at fire time** — the bundled-creds
   allow-list might change between `t=0` (scheduled) and `t=2h`
   (fired). Re-evaluate `_validate_recipients` inside the worker.
2. **Bundled-creds guard still applies** — same #115 v1 invariant.
3. **LLM cannot auto-schedule sends.** Every `send_email` task MUST
   pass through the UI's Confirm button. Stage 2's LLM proposal alone
   is not enough — only `SCHEDULED_TASK_CREATED` (which requires a
   `POST /schedule/confirm` from an authenticated UI session) actually
   activates the row.
4. **Cancellation is soft delete** — `status='cancelled'` + write
   `SCHEDULED_TASK_CANCELLED` event. Original payload remains in event
   log for audit.
5. **Per-user quota** — `MAX_PENDING_TASKS_PER_USER = 50` to prevent
   accidental loops / abuse.

## Phasing

### Phase 1 — MVP (~2 days)

- `scheduled_tasks` table + Alembic `0003_scheduled_tasks` migration
- New `scheduler_router.py`:
  - `POST /api/v1/schedule/confirm` — body: `{proposal_id}` or full payload
  - `GET  /api/v1/schedule/list?status=...`
  - `DELETE /api/v1/schedule/{task_id}`
- Worker started in `main.py` lifespan (single asyncio task, 30s poll)
- `send_email` kind only, one-shot only (`recurrence_cron=NULL`)
- Heuristic-only intent extraction (no LLM call)
- Chat inline confirmation card + AccountMenu list entry
- Tests: regex extractor, worker dispatch, fire-time re-validation,
  TZ edge cases (DST, cross-day)

### Phase 2 — Full feature (~3 days)

- LLM-based intent extraction (Gemini `response_schema`)
- `recurrence_cron` support (croniter)
- `chat_brief` and `reminder` kinds
- New Calendar mode (replaces AccountMenu entry as primary surface)
- TodayMode upcoming-tasks card
- Result-delivery: `chat_brief` result auto-pins to the patient + a
  Today card "1 new brief"

### Phase 3 — Optional

- Native macOS notification center (via Tauri plugin)
- Cohort tasks ("every patient with pending biopsy")
- Google Calendar bidirectional sync (OAuth)

## Open discussion items

1. **Timezone semantics** — user input "明天 9 点" is in user-local
   time, sidecar runs UTC. Recommended approach: frontend sends
   user_tz to backend; Stage 2 LLM is told the user_tz; storage is
   unix-sec UTC; rendering converts back to user_tz. DST safe via
   `zoneinfo`.

2. **Offline behaviour** — what if the task is due but the sidecar
   isn't running (laptop closed)? Recommended: on next sidecar boot,
   the worker's first scan catches all overdue `pending` rows and
   catch-up-executes in time order. Each kind can opt out via
   `skip_if_late=True` in payload (e.g., a "remind me in 30min"
   shouldn't fire 8 hours later).

3. **`chat_brief` result destination** — email to medic? Pin in
   Calendar? Pin to patient? Recommended default: pin to patient +
   Today header card "1 new brief", with optional "also email me"
   payload flag.

4. **Intent-extraction false positives** — "我之前在 2024 年 6 月看
   过这个病人" should NOT trigger scheduling. Recommended: Stage 2
   LLM's first field is `is_future_intent: bool` — when false, skip
   everything else. We log the false-positive rate via the
   `SCHEDULED_TASK_PROPOSED` event without a corresponding
   `_CREATED` (proposed-but-rejected ratio).

5. **Editing a scheduled task** — full re-prompt vs inline form?
   Recommended: inline form (fire_at, payload fields editable);
   re-emit `SCHEDULED_TASK_CREATED` with a `replaces=<old_task_id>`
   field; old task gets `status='cancelled'`.

## Out of scope

- Multi-user delegation ("schedule this on Dr Lee's calendar")
- Conditional triggers ("when this patient's lab result arrives,
  email me") — that's an event-driven primitive, different design
- Stripe-style retries on transient failure (Phase 1: one shot, hard
  fail → status='error', medic re-schedules manually)
