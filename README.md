# Nexus

> **Runtime is temporary; identity is eternal.**

Nexus is a platform for **persistent, self-evolving AI agents** anchored on
BNB Chain. Each user owns one *Digital Twin* — a private agent that
accumulates memories, learns skills, and rewrites its own persona over
time, while every change is auditable on-chain.

Models will be replaced. The agent isn't.

---

## Why "永生 agent" (the immortality property)

Most AI agents today are stateless: a model invocation produces tokens,
those tokens vanish, the next session starts from zero. A persona that
"remembers" you is just a system prompt; a "skill" the agent learned is
just a few lines of context the operator chose to keep.

A Nexus agent is the opposite. It has:

1. **An identity that outlives any single LLM.** Each agent is registered
   on BSC under [ERC-8004](docs/concepts/identity.md). The token id is
   the permanent handle. Swap Gemini for Claude tomorrow and the agent's
   memories, persona, skills, and social impressions all carry over.

2. **A memory that is a chained log, not a session buffer.** Every event
   ever observed is appended to a SQLite-backed `EventLog`, and a
   SHA-256 root over a deterministic manifest of recent events is
   anchored to BSC after each compaction. You can replay the log,
   recompute the root, and prove the agent didn't lie about its own
   history.

3. **A self-evolution loop that is *falsifiable*.** Every persona /
   memory / skill / knowledge edit emits an `evolution_proposal` event
   *before* it lands in the store. After an observation window, a
   `VerdictRunner` scores the edit against what actually happened —
   contract violations, drift score, observed regressions — and writes
   back a verdict (`kept` / `kept_with_warning` / `reverted`). A
   reverted verdict triggers an automatic rollback of the namespace
   store. The agent gets to grow, but every step of growth is on the
   record and can be undone.

4. **A wallet of typed memory namespaces.** Memory is split into five
   independently versioned stores — Episodes, Facts, Skills, Persona,
   Knowledge — each with `propose / commit / rollback` semantics. New
   facts don't blur into a single soup of notes; they live where the
   verdict scorer can grade them and where the user can inspect, approve,
   or roll them back from the desktop UI.

These four properties together are what we mean by an *immortal agent*:
its identity is a chain primitive, its growth is durably stored, and
its evolution is something you can argue with rather than just hope is
going well.

---

## Five-minute tour

```
┌─────────────────────────┐
│  Desktop (Avalonia C#)  │   thin client — JWT only
└────────────┬────────────┘
             │ HTTPS
┌────────────▼────────────┐
│  Server (FastAPI)       │   passkey auth, multi-tenant
│   one DigitalTwin per   │   /chat /messages /timeline
│   logged-in user        │   /memory/namespaces
│                         │   /evolution/verdicts
└────────────┬────────────┘
             │
┌────────────▼────────────┐
│  Nexus framework        │   9-step chat loop
│   DigitalTwin           │   ProjectionMemory (DPM)
│   EvolutionEngine       │   4 evolvers + VerdictRunner
└────────────┬────────────┘
             │
┌────────────▼────────────┐
│  nexus_core SDK         │   AgentRuntime facade
│   EventLog + 5 stores   │   ContractEngine + DriftScore
│   ChainBackend          │   BSCClient
└────────────┬────────────┘
             │
             ▼
          BSC RPC
         (anchor)
```

A user installs the desktop, signs in with a passkey, and starts chatting.
On the first message:

1. **Server** lazily creates a `DigitalTwin` for that user and bootstraps
   on-chain identity: mints an ERC-8004 token and sets
   `activeRuntime` on the AgentStateExtension contract to this server's
   wallet.
2. **Twin** runs its [9-step chat flow](docs/concepts/data-flow.md):
   ABC pre-check → append user message to EventLog → project relevant
   memory → call LLM with tools → ABC post-check → DriftScore update →
   append assistant response → fire background self-evolution.
3. **SDK** persists the new EventLog rows to its durable store and,
   after every compaction, anchors the new state root on BSC. Reads are
   served from the local SQLite mirror so chat latency is unaffected.
4. **Desktop** receives the response over HTTP and re-fetches its
   panels in the background — Brain (learning + chain status), Pressure
   Dashboard (which evolver is about to fire), Evolution timeline,
   Activity stream.

The "self-evolving" part is real and observable in two places:

- **Brain panel** answers *"is my agent learning, and is what it
  learned safely on chain?"* — namespace counts + 7-day timeline +
  data-flow pipeline + just-learned feed + chain-health card, with
  every item tagged ● local · ● persisted · ● anchored
  on BSC.
- **Evolution panel** shows the falsifiable loop: every persona /
  memory / skill edit recorded as a proposal, then graded as a
  verdict, and (when something regresses) auto-reverted with full
  traceability.

---

## The four mechanisms in one paragraph each

### Deterministic Projection Memory (DPM)

The agent's working memory is the **projection** of an append-only
event log, not a separate store. Two projections coexist on the same
log so that performance and auditability don't fight:

- **Chat projection** is *stochastic* and uses a Recursive Language
  Model (RLM, [arXiv:2512.24601]) — for short logs a single LLM call
  picks the relevant slice; for long logs a root LLM treats the log as
  a REPL variable and writes Python that recursively calls smaller
  sub-LLMs over chunks. This optimises for *recall quality* during a
  conversation.
- **Anchor projection** is *deterministic*: a chunked manifest with
  RFC 8785 JCS canonicalisation, hashed with SHA-256. This optimises
  for *verifiability* at chain-anchor time.

The two projections never share state. The chat projection can hallucinate
a detail; the anchor projection cannot, because its inputs are bytes and
its outputs are commitments. See [`docs/concepts/dpm.md`](docs/concepts/dpm.md)
and [`docs/design/nexus-architecture.md`](docs/design/nexus-architecture.md).

### Five-namespace typed memory (Phase J)

Per [BEP-Nexus §3.3](docs/BEP-nexus.md), the curated memory layer is
*not* a single flat store. It's five independently versioned namespaces:

| Namespace | Holds | Granularity | Versioning |
|---|---|---|---|
| **Episodes** | session-level autobiographical summaries | per session | working file + commit |
| **Facts** | atomic, citable claims (preference / fact / constraint / goal / context, importance 1-5, optional TTL) | per fact | working file + commit |
| **Skills** | learned strategies per `task_kind` (success / failure counts) | per skill | working file + commit |
| **Persona** | the agent's identity / system prompt | per version | every update *is* a new version (no working file) |
| **Knowledge** | distilled long-form articles | per article | working file + commit |

All five sit on the same `VersionedStore` primitive — immutable
`v{N}.json` snapshots plus a movable `_current.json` pointer.
Rollback flips the pointer; older versions are never destroyed. Phase
O verdicts use exactly this primitive when they need to undo a bad
edit.

### Falsifiable self-evolution (Phase O, inspired by AHE [arXiv:2604.25850])

The empirical lesson from the AHE paper is that *predicted* regressions
are essentially noise — agents are bad at forecasting which task kinds
their own edits will break. So Nexus only ever rolls back on
**observed** regressions, never predicted ones. The contract is:

```
proposal      ──►  evolver writes to namespace store
   │                emits evolution_proposal event with
   │                predicted_fixes + predicted_regressions
   │                (predictions are advisory, not binding)
   │
   │  observation window (default: 100 events)
   ▼
verdict       ──►  VerdictRunner scans the EventLog window:
                     - observed contract violations  → regressions
                     - drift_delta vs intervention θ → severity gate
                     - calls SDK score_verdict()    → kept / warning / reverted
                   writes back evolution_verdict
                   if reverted: store.rollback(rollback_pointer)
                   + emits evolution_revert
```

The user can also *manually* approve or revert any pending edit from
the desktop UI; both produce verdict / revert events that look
identical to the auto-grader's, so the timeline reads uniformly. See
[`docs/design/nexus-architecture.md`](docs/design/nexus-architecture.md).

### On-chain identity + verifiable growth (BEP-Nexus)

Each agent's on-chain footprint:

- **ERC-8004 NFT** on BSC `IdentityRegistry`. The `tokenId` is the
  permanent agent id. Transferring the NFT transfers the agent.
- **A durable per-agent object store** (S3-compatible mirror planned)
  holding the EventLog snapshot, namespace store snapshots, and
  per-version manifests.
- **`AgentStateExtension` contract** stores the latest state-root
  pointer for each agent and tracks `activeRuntime` (which
  server's wallet is currently authorised to write). NFT transfer
  resets `activeRuntime` to the new owner — no stale runtime can keep
  writing.
- **`TaskStateManager` contract** is the on-chain TaskStore for
  agent-to-agent task delegation (A2A protocol).

State-root computation, manifest schema, and the seven test vectors
that pin the canonical encoding live in
[`docs/BEP-nexus.md`](docs/BEP-nexus.md).

---

## Why split it this way?

| Layer | Knows about | Doesn't know about |
|---|---|---|
| `nexus_core` (SDK) | BSC web3, append-only logs, contract spec parsing, LLM provider abstraction | agents, users, HTTP, JWT |
| `nexus` (framework) | DigitalTwin lifecycle, 9-step chat flow, evolution scheduling, projection mode | HTTP, JWT, multi-tenancy |
| `nexus_server` | FastAPI routes, WebAuthn passkeys, one twin per user, view-shape APIs | how chat works inside a turn (delegated to `twin.chat()`) |
| `RuneDesktop.*` | Avalonia views, view models, JWT lifetime | persistence (server is the source of truth) |

Imports flow strictly downward — SDK never imports framework, framework
never imports server, etc. This is the single most important property
of the architecture. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the full breakdown.

---

## Repository layout

```
packages/
  sdk/      nexus_core/        Infrastructure primitives (no agent concept)
  nexus/    nexus/             DigitalTwin + 4 evolvers + VerdictRunner
  server/   nexus_server/      FastAPI multi-tenant frontend
  desktop/  RuneDesktop.*/     Avalonia C# thin client

docs/
  BEP-nexus.md                 The chain-anchor protocol spec
  concepts/                    DPM, ABC, identity, modes, data-flow
  design/                      Falsifiable evolution, recursive projection
  how-to/                      Add a tool, add a contract rule

ARCHITECTURE.md                How the layers fit together
HISTORY.md                     How we got here (Phases A–P)
ROADMAP.md                     What's next
```

---

## Quickstart

**End user**: download the latest `.dmg` from Releases, drag `Nexus.app`
to `/Applications`, launch. That's it — the installer ships with all
Python deps, the Tauri shell, default LLM keys, and the schema-migration
runner. First launch creates the database; subsequent launches apply
any pending migrations automatically.

**Developer building from source on macOS**:

```bash
cd packages/desktop-v2
./scripts/build-macos.sh
```

That single command bootstraps every prerequisite (Xcode CLT, Homebrew,
Python 3.12, pnpm, Rust), installs all monorepo packages editable, runs
PyInstaller + Tauri, auto-installs the resulting `.app` into
`/Applications/`, and relaunches. See
[`ENGINEERING_STANDARDS.md`](ENGINEERING_STANDARDS.md) §1 — there are no
"step 2: manually run X" instructions by design.

For a fully on-chain setup (BSC testnet), see
[`docs/concepts/modes.md`](docs/concepts/modes.md).

The legacy `demo/` folder has been retired — the per-package test suites
are the canonical reference for how each layer is meant to be used:

```bash
pytest packages/sdk/tests/        # 334 tests (+3 skipped)
pytest packages/nexus/tests/      # 249 tests
pytest packages/server/tests/     # 122 tests
```

---

## Where to read next

| You want to… | Read this |
|---|---|
| Understand the system end-to-end | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| See exactly what happens when a user sends a message | [`docs/concepts/data-flow.md`](docs/concepts/data-flow.md) |
| Understand the memory model | [`docs/concepts/dpm.md`](docs/concepts/dpm.md) |
| Understand the safety + drift model | [`docs/concepts/abc.md`](docs/concepts/abc.md) |
| Understand on-chain identity | [`docs/concepts/identity.md`](docs/concepts/identity.md) |
| Understand chain mode vs local mode | [`docs/concepts/modes.md`](docs/concepts/modes.md) |
| Read the on-chain protocol spec | [`docs/BEP-nexus.md`](docs/BEP-nexus.md) |
| Read the falsifiable-evolution design | [`docs/design/nexus-architecture.md`](docs/design/nexus-architecture.md) |
| Read the RLM-based projection design | [`docs/design/nexus-architecture.md`](docs/design/nexus-architecture.md) |
| Build & run everything locally | `cd packages/desktop-v2 && ./scripts/build-macos.sh` (or `./scripts/dev-loop.sh` for hot-reload) |
| Add a new tool the agent can call | [`docs/how-to/add-a-tool.md`](docs/how-to/add-a-tool.md) |
| Add a new behaviour rule | [`docs/how-to/add-a-contract-rule.md`](docs/how-to/add-a-contract-rule.md) |

---

## Status

Test phase. APIs and on-chain schemas may still break; contracts are on
BSC testnet only. The core loop — chat, evolution, verdicts, rollback,
chain anchoring — is implemented end-to-end and covered by 705 tests
across SDK / framework / server. See [`ROADMAP.md`](ROADMAP.md) for
what's next.

---

## References

- AHE: *Active Handover Evaluation for self-evolving agents* — arXiv:2604.25850
- RLM: *Recursive Language Models* — arXiv:2512.24601
- ABC: *Agent Behaviour Contract* — arXiv:2602.22302
- ERC-8004: BSC IdentityRegistry standard
- RFC 8785: JSON Canonicalization Scheme

> The arXiv IDs above are the design references the implementation is
> based on. Where Nexus deviates from a paper (e.g. the AHE
> "predictions are noise" finding driving our never-revert-on-prediction
> rule), the deviation is documented in the matching design doc under
> `docs/design/`.
