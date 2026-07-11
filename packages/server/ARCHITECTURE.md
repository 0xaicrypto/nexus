# nexus-server architecture

> Component diagram + per-module breakdown for the
> `nexus_server` package. The cross-cutting story (DPM, ABC,
> identity flow, three-layer split) lives in the root
> [`ARCHITECTURE.md`](../../ARCHITECTURE.md).

## What this package is

A multi-tenant FastAPI HTTP frontend. The server **does not** run
agent intelligence itself — every request flows through a per-user
`nexus.DigitalTwin` instance (`twin_manager.get_twin(user_id)`)
which holds the EventLog, ContractEngine, MemoryEvolver, and
ChainBackend. Server modules are mostly:

* HTTP routers that translate request/response shapes.
* Read-only views over each twin's per-user EventLog SQLite.
* Lifecycle / background concerns (idle reaper, chain bootstrap,
  chain-activity log capture).

```
                ┌───────────────────────────┐
HTTP / browser ─┤  FastAPI app (main.py)    │
                └──────────────┬────────────┘
            ┌──────────────────┴───────────────────┐
            │                  │                   │
        Routers          Views (read)        Background
            │                  │                   │
   auth.routes              agent_state    twin_manager (lifecycle,
   llm_gateway              twin_event_log          idle reaper,
   chain_proxy                                 chain bootstrap,
   files / user_profile                        chain activity log)
            │                                         │
            │                                         │
            └──────────────────┬──────────────────────┘
                               ▼
                  per-user nexus.DigitalTwin
                       │              │
              EventLog SQLite       ChainBackend
            (~/.nexus_server/        (BSC anchoring
             twins/{uid}/…)           via nexus_core)
```

## Modules

| Module | Role | Routes |
| --- | --- | --- |
| `main.py` | FastAPI assembly, lifespan (twin reaper + chain log handler), `.env` loading from cwd → `packages/server/.env` → `packages/sdk/.env` | `/health` |
| `config.py` | Settings dataclass, `NEXUS_USE_TWIN`, `NEXUS_TWIN_BASE_DIR`, etc. | – |
| `database.py` | SQLite init for the auth/users DB. The legacy `sync_events` mirror table was dropped in Phase B. | – |
| `middleware.py` | Rate limiting, shared utilities | – |
| `auth/` (real package, Phase C) | Username + password (bcrypt) + JWT — `routes.py`. `get_current_user` dependency, `create_jwt_token`. | `/api/v1/auth/*` |
| `llm_gateway.py` | `/api/v1/llm/chat` — looks up the user's twin and delegates to `twin.chat()`. Validates attachment caps. | `POST /api/v1/llm/chat` |
| `attachment_distiller.py` | Thin shim over `nexus_core.distiller`. Server-side `record_distilled_event` was removed (Phase B); summaries ride back inline in the chat response. | – |
| `files.py` | Per-user file picker + upload | `POST /api/v1/files/upload` |
| `chain_proxy.py` | ERC-8004 reads (`/me`, `/agent/{id}`); the legacy `/register-agent` endpoint is deprecated — twin auto-bootstraps on first chat (S6). | `/api/v1/chain/me`, `/api/v1/chain/agent/{id}` |
| `sync_anchor.py` | Read-only legacy view: `enqueue_anchor` + `list_anchors_for_user`. The Phase A retry daemon was deleted in Phase B. | – |
| `twin_manager.py` | Per-user `DigitalTwin` lifecycle: lazy create, idle eviction, `bootstrap_chain_identity`, `_ChainActivityLogHandler` (Bug 3 — capture SDK chain activity into `twin_chain_events` so the desktop sidebar can show anchor successes / failures). | – |
| `twin_event_log.py` | Read-only views over each user's twin EventLog SQLite. Used by `agent_state` to serve `/agent/{messages,memories,timeline}` without instantiating a twin. | – |
| `agent_state.py` | The read API surface | `/api/v1/agent/{state,timeline,memories,messages}`, `/api/v1/sync/anchors` |
| `user_profile.py` | Profile management | `/api/v1/profile/*` |

## Test

`tests/test_server_regression.py` — 64 cases covering auth flow,
twin path, attachments, chain proxy, anchor reads, agent_state,
files upload. Each test runs against a fresh SQLite DB + twin
EventLog dir (see `tests/conftest.py`).

```bash
pytest tests/                       # 64 cases, ~3s
pytest tests/ -k attachments
pytest --cov=nexus_server tests/
```

## Storage

```
./nexus_server.db                                       # auth + users
~/.nexus_server/twins/{user_id}/event_log/{agent_id}.db # per-user twin EventLog
~/.nexus_server/twins/{user_id}/state/                  # CuratedMemory + ABC contract state
```

The on-chain anchoring (BSC `IdentityRegistry.updateStateRoot`)
is owned by the twin's `ChainBackend` (driven by the SDK), not by
the server. The server only sees chain *activity* via the log
handler that mirrors SDK log records into `twin_chain_events` for
UI display.

## Test isolation

`tests/conftest.py` pins the SQLite DB to `tempdir/rune_test.db`
and the twin event-log dir to `tempdir/rune_test_twins`, wiping
both before/after each test. `NEXUS_USE_TWIN=0` is set globally so
existing `/llm/chat` tests that mock `llm_gateway.call_llm` keep
working — twin-path tests opt in by setting
`twin_manager._test_override`.

## What changed (vs. older docs)

| Was | Is now | Phase |
| --- | --- | --- |
| `bnbchain_agent` package | `nexus_core` | D |
| `rune_twin` package | `nexus` | D |
| `rune_server` package | `nexus_server` | D |
| `sync_hub.py` router (`/sync/push` /pull/) | tombstone — desktop is thin client | B |
| `sync_events` mirror table | dropped — twin EventLog is authoritative | B |
| Anchor retry daemon | deleted — `ChainBackend` owns retry | B |
| `nexus.{tools,skills,mcp}` shim packages | tombstones; import `nexus_core.*` | E |
| Logger namespace `rune.*` | `nexus_core.*` | F |
| Object-storage bucket `rune-agent-{token_id}` (data plane since removed) | `nexus-agent-{token_id}` | F |

See root [`HISTORY.md`](../../HISTORY.md) for the full chronology.
