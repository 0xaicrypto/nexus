# Heurion

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/)
[![Node](https://img.shields.io/badge/node-22+-green.svg)](https://nodejs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![TypeScript](https://img.shields.io/badge/TypeScript-3178C6.svg?logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![React](https://img.shields.io/badge/React-61DAFB.svg?logo=react&logoColor=black)](https://react.dev/)
[![Status](https://img.shields.io/badge/status-active-blue.svg)](ROADMAP.md)

> **An AI for research should accumulate, not reset.**
> *Runtime is temporary; identity is eternal.*

> **面向研究的 AI 应当积累，而非重置。**
> *运行时可逝；身份永恒。*

Heurion is a **research-first clinical AI workstation** for oncology
investigators, built on top of a persistent self-evolving agent
platform.

Heurion 是一个**以研究为先导的临床 AI 工作站**，面向肿瘤研究者，构建在持久化自我进化智能体平台之上。

> **Note:** Internal code packages still use the `nexus` prefix
> (`nexus_core`, `nexus`, `nexus_server`) for import stability. The
> public product, domain, and user-facing branding are **Heurion**.
>
> **说明：** 内部代码包仍使用 `nexus` 前缀（`nexus_core`、`nexus`、
> `nexus_server`）以保持导入稳定性。公开产品、域名和用户可见品牌为
> **Heurion**。

The product is organised around the trials a physician is running, not
around isolated patient chats:

产品围绕医生正在开展的试验组织，而非孤立的医患对话：

- The app's home is a **Research Workspace** — a list of studies, each
  with its own roster, eligibility inbox, visit schedule, safety stream,
  and a cross-patient Research Chat scoped to that protocol.
- 应用首页是**研究工作台**——研究列表，每项研究拥有独立的入组名单、受试者筛选收件箱、访视日程、安全性信息流，以及限定于该方案的患者间研究对话。
- Each patient is reachable as a drill-in *within* a study, with a
  per-patient Patient Chat for individual clinical decisions.
- 每位患者可在研究中逐层下钻查看，并配有面向个体临床决策的患者对话。
- Every enrollment, screening verdict, AE confirmation, and protocol
  deviation is an *event* on a per-patient append-only log; nothing is
  ever silently deleted or overwritten.
- 每次入组、筛选结论、不良事件确认和方案偏离，都是每位患者只追加日志中的*事件*；没有任何内容会被静默删除或覆盖。
- The agent's memory (Episodes / Facts / Skills / Persona / Knowledge)
  accumulates the physician's heuristics over time, with each edit
  graded by a Verdict Runner and reversible if it regresses.
- 智能体的记忆（片段 / 事实 / 技能 / 人格 / 知识）会随时间积累医生的启发式经验，每次编辑都由裁决运行器评分，若出现退化则可回滚。

The canonical product spec is
[`docs/design/RESEARCH_WORKSPACE_DESIGN.md`](docs/design/RESEARCH_WORKSPACE_DESIGN.md);
the high-fidelity UI mock lives at
[`docs/design/visual-mock/`](docs/design/visual-mock/).
The SaaS pivot decision is recorded in
[`docs/adr/ADR-003-web-ui-saas-pivot.md`](docs/adr/ADR-003-web-ui-saas-pivot.md).

产品规范见 [`docs/design/RESEARCH_WORKSPACE_DESIGN.md`](docs/design/RESEARCH_WORKSPACE_DESIGN.md)；
高保真 UI 原型见 [`docs/design/visual-mock/`](docs/design/visual-mock/)；
SaaS 转型决策见 [`docs/adr/ADR-003-web-ui-saas-pivot.md`](docs/adr/ADR-003-web-ui-saas-pivot.md)。

The rest of this README describes the **platform** that powers that
product — the four-layer architecture and the immortality / auditability
properties that make it possible.

本文档其余部分介绍支撑该产品的**平台**——四层架构以及使其成为可能的永生 / 可审计特性。

Models will be replaced. The agent isn't.

模型会被替换，智能体不会。

---

## Why "永生 agent" (the immortality property)

Most AI agents today are stateless: a model invocation produces tokens,
those tokens vanish, the next session starts from zero. A persona that
"remembers" you is just a system prompt; a "skill" the agent learned is
just a few lines of context the operator chose to keep.

A Heurion agent is the opposite. It has:

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
│  Web UI                 │   React + Vite + Tailwind + i18n
│   (packages/web)        │   browser-first SaaS; mobile-ready
├─────────────────────────┤
│  Desktop v2             │   Tauri 2.0 + React + TS (frozen)
│   (packages/desktop-v2) │
└────────────┬────────────┘
             │
┌────────────▼────────────┐
│  @heurion/sdk           │   Typed client library
│   (packages/sdk-client) │   Browser + Node.js + CLI
│   HeurionClient + 10    │   AsyncGenerator for SSE
│   typed modules         │
└────────────┬────────────┘
             │ HTTP + JWT, SSE
┌────────────▼────────────┐
│  Server (TypeScript)    │   Fastify + Prisma + SQLite
│   (packages/server-ts)  │   Auth/Patients/Research/Docs
│   Modular architecture  │   Chat SSE + Evolution + Memory
└────────────┬────────────┘
             │ gRPC / HTTP (optional)
┌────────────▼────────────┐
│  Python Worker          │   DICOM parsing + MONAI inference
│   (packages/server)     │   Event-sourcing + Clinical graph
│   + SDK (packages/sdk)  │   Vector search + OCR
└─────────────────────────┘
```

### 架构概览

Heurion 采用四层架构：

| 层 | 包 | 技术栈 | 职责 |
|---|---|---|---|
| **Web UI** | `packages/web` | React + Vite | 浏览器端界面，明暗主题，中英文 |
| **SDK** | `packages/sdk-client` | TypeScript | 10 个类型化模块，浏览器+CLI 共用 |
| **Server** | `packages/server-ts` | Fastify + Prisma | 认证/对话/研究/文档/技能/管理 |
| **Python Worker** | `packages/server` + `packages/sdk` | FastAPI + Python | DICOM/MONAI/事件溯源/向量搜索 |

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
their own edits will break. So Heurion only ever rolls back on
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
| `@heurion/sdk` (sdk-client) | HTTP, JWT, SSE, 10 typed modules | DOM, React, rendering |
| `server-ts` (TypeScript) | Fastify, Prisma, Auth, Business logic | DICOM, MONAI, medical imaging |
| `server` + `sdk` (Python) | pydicom, MONAI, event-sourcing | HTTP, JWT, multi-tenancy |
| `packages/web` | React views, i18n, dark mode, routing | persistence (server is source of truth) |

Imports flow strictly downward — SDK never imports framework, framework
never imports server, etc. This is the single most important property
of the architecture. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the full breakdown.

---

## Repository layout

```
packages/
  sdk/         nexus_core/         Infrastructure primitives (no agent concept)
  nexus/       nexus/              DigitalTwin + 4 evolvers + VerdictRunner
  server/      nexus_server/       FastAPI multi-tenant frontend + clinical stack
  web/         src/                Browser-first React SaaS UI
  desktop-v2/  src/ + src-tauri/   Tauri 2.0 + React + TS thin client (frozen)
  relay/       main.py             Stand-alone Python service (webhooks, email)

docs/
  BEP-nexus.md                     The chain-anchor protocol spec
  concepts/                        DPM, ABC, identity, modes, data-flow
  design/                          Falsifiable evolution, recursive projection
  adr/                             Architecture decision records
  how-to/                          Add a tool, add a contract rule

ARCHITECTURE.md                    How the layers fit together
HISTORY.md                         How we got here (Phases A–F, ongoing)
ROADMAP.md                         What's next
```

The legacy Avalonia desktop (`packages/desktop`) lives at git tag
`legacy/avalonia-final`; it was removed from `main` once desktop-v2
reached parity.

---

## Quickstart

### Web UI (SaaS / self-hosted)

```bash
cd packages/web
pnpm install
pnpm build        # produces dist/ served by the backend
```

The FastAPI server serves the built static files at `/`. For a full
self-hosted deployment, see [`scripts/deploy_setup.sh`](scripts/deploy_setup.sh).

### Desktop v2 (frozen, macOS)

**End user**: download the latest `.dmg` from Releases, drag `Heurion.app`
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
pytest packages/sdk/tests/
pytest packages/nexus/tests/
pytest packages/server/tests/
```

(The total runs to a couple of thousand tests; per-package counts shift
on every PR, so we no longer pin numbers in docs — check CI for the
current snapshot.)

---

## Where to read next

| You want to… | Read this |
|---|---|---|
| Understand the system end-to-end | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| See exactly what happens when a user sends a message | [`docs/concepts/data-flow.md`](docs/concepts/data-flow.md) |
| Understand the memory model | [`docs/concepts/dpm.md`](docs/concepts/dpm.md) |
| Understand the safety + drift model | [`docs/concepts/abc.md`](docs/concepts/abc.md) |
| Understand on-chain identity | [`docs/concepts/identity.md`](docs/concepts/identity.md) |
| Understand chain mode vs local mode | [`docs/concepts/modes.md`](docs/concepts/modes.md) |
| Read the on-chain protocol spec | [`docs/BEP-nexus.md`](docs/BEP-nexus.md) |
| Read the falsifiable-evolution design | [`docs/design/nexus-architecture.md`](docs/design/nexus-architecture.md) |
| Read the RLM-based projection design | [`docs/design/nexus-architecture.md`](docs/design/nexus-architecture.md) |
| Read the Web UI / SaaS pivot decision | [`docs/adr/ADR-003-web-ui-saas-pivot.md`](docs/adr/ADR-003-web-ui-saas-pivot.md) |
| Read the Web UI redesign proposal | [`docs/design/web-ui-redesign.md`](docs/design/web-ui-redesign.md) |
| Build & run the web UI locally | `cd packages/web && pnpm dev` |
| Build & run the desktop on macOS | `cd packages/desktop-v2 && ./scripts/build-macos.sh` (or `./scripts/dev-loop.sh` for hot-reload) |
| Add a new tool the agent can call | [`docs/how-to/add-a-tool.md`](docs/how-to/add-a-tool.md) |
| Add a new behaviour rule | [`docs/how-to/add-a-contract-rule.md`](docs/how-to/add-a-contract-rule.md) |

---

## Status

Test phase. APIs and on-chain schemas may still break; contracts are on
BSC testnet only. The core loop — chat, evolution, verdicts, rollback,
chain anchoring — is implemented end-to-end and covered by a thorough
test suite across SDK / framework / server (see CI for the live count).
The web UI is under active development as the new primary client; the
desktop-v2 Tauri client is frozen. See [`ROADMAP.md`](ROADMAP.md) for
what's next.

### 状态

测试阶段。API 和链上模式仍可能变动；合约目前仅在 BSC 测试网。核心
循环——对话、进化、裁决、回滚、链上锚定——已实现端到端，并通过 SDK /
框架 / 服务端全面测试覆盖。Web UI 正在积极开发为新主客户端；
desktop-v2 Tauri 客户端已冻结。后续计划见 [`ROADMAP.md`](ROADMAP.md)。

---

## References

- AHE: *Active Handover Evaluation for self-evolving agents* — arXiv:2604.25850
- RLM: *Recursive Language Models* — arXiv:2512.24601
- ABC: *Agent Behaviour Contract* — arXiv:2602.22302
- ERC-8004: BSC IdentityRegistry standard
- RFC 8785: JSON Canonicalization Scheme

> The arXiv IDs above are the design references the implementation is
> based on. Where Heurion deviates from a paper (e.g. the AHE
> "predictions are noise" finding driving our never-revert-on-prediction
> rule), the deviation is documented in the matching design doc under
> `docs/design/`.
