# nexus-server quickstart

> The bigger picture (how server / nexus / SDK fit together, DPM,
> ABC, identity flow) is in the root
> [`README.md`](../../README.md) and
> [`ARCHITECTURE.md`](../../ARCHITECTURE.md). This file is just
> "get the server running locally and verify it works".

## 1. Install

```bash
cd packages/server
pip install -e ".[dev]"
# Optional: openai + anthropic providers (openai also powers kimi/Moonshot AI)
pip install -e ".[llm-extra]"
```

## 2. Configure

Create `packages/server/.env`:

```env
SERVER_SECRET=local-jwt-secret-change-me
GEMINI_API_KEY=AIza...
DATABASE_URL=sqlite:///./nexus_server.db

# Custodial chain mode (optional)
SERVER_PRIVATE_KEY=0x...   # leave unset to disable chain writes
```

Network/contract addresses (BSC RPC, ERC-8004 contracts) are
loaded from `packages/sdk/.env` automatically.

## 3. Run

```bash
python -m nexus_server.main --reload
# or via the entry-point script:
nexus-server --reload
```

Server boots on `http://localhost:8001`. Health check:

```bash
curl http://localhost:8001/health
```

## 4. Verify

Run the regression suite:

```bash
pytest tests/                # 64 tests, ~3s
```

Quick smoke probe with curl (after registering a user and capturing
the JWT):

```bash
# Register (first user becomes admin) and capture the JWT
TOKEN=$(curl -s -X POST http://localhost:8001/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"dev","password":"Str0ng-Pass-123"}' | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["jwt_token"])')

# Chat → routes through DigitalTwin
curl -X POST http://localhost:8001/api/v1/llm/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hello"}]}'

# Read views
curl -H "Authorization: Bearer $TOKEN" http://localhost:8001/api/v1/agent/state
curl -H "Authorization: Bearer $TOKEN" http://localhost:8001/api/v1/agent/timeline
curl -H "Authorization: Bearer $TOKEN" http://localhost:8001/api/v1/chain/me
```

## 5. Inspect data

The auth / users SQLite is at `./nexus_server.db`:

```bash
sqlite3 nexus_server.db ".tables"
sqlite3 nexus_server.db "SELECT id, display_name, created_at FROM users;"
```

Per-user twin EventLog SQLite is at
`~/.nexus_server/twins/{user_id}/event_log/{agent_id}.db`
(override with `NEXUS_TWIN_BASE_DIR`):

```bash
sqlite3 ~/.nexus_server/twins/$UID/event_log/user-${UID:0:8}.db \
  "SELECT seq, event_type, ts FROM events ORDER BY seq DESC LIMIT 20;"
```

> **Note** — Phase B retired the legacy server-side `sync_events`
> mirror table; if you read older docs that reference it, they're
> describing pre-S5 architecture. The twin's own EventLog is the
> single source of truth now.

## 6. Common issues

* **`ImportError: cannot import name 'sync_hub'`** — that module
  is intentionally a tombstone (Phase B). Nothing should import
  it; if your code does, switch to the relevant replacement
  (`twin_event_log` for events, `twin_manager` for lifecycle,
  `agent_state` for read views).
* **`ImportError: nexus.skills was removed in Phase E`** —
  re-export shims under `nexus.{tools,skills,mcp}` are gone;
  import from `nexus_core.{tools,skills,mcp}` instead.
* **DB locked / "disk I/O error"** — usually a leftover
  `nexus_server.db` from a previous run; `rm nexus_server.db` and
  restart (or set `DATABASE_URL` to a fresh path).
* **Chain operations failing silently** — check `nexus_server.log`
  for `nexus_core.backend.chain`
  warnings; chain writes fall back gracefully if
  `SERVER_PRIVATE_KEY` isn't set.

## Where to go next

* [`README.md`](README.md) — full package reference (modules,
  layout, env vars).
* [`ARCHITECTURE.md`](ARCHITECTURE.md) — detailed component map.
* Root [`HISTORY.md`](../../HISTORY.md) — chronology of S1–S6,
  Round 2-A/B/C, Phase A–F.
