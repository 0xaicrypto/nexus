# Roadmap

What's next, in approximate order. Items in **Now** are actively being
worked on; **Next** is queued; **Later** is shape-known but not
prioritised.

## Now

### Reorg Phase A — docs (this commit)

`README.md`, `ARCHITECTURE.md`, `HISTORY.md`, `docs/concepts/*.md`,
`docs/how-to/*.md`. Zero code change. Done.

### Reorg Phase B — delete dead code

- `git rm -rf packages/desktop/RuneDesktop.Sync/`
- Delete `nexus_server/sync_hub.py` + `/sync/push` `/sync/pull`
  endpoints (desktop is thin client now, no callers).
- Delete `nexus_server/sync_anchor.py` retry daemon (already opt-in,
  no production users).
- Delete the `sync_events` table + `_build_on_event` mirror writes
  (S5 made `/agent/*` endpoints read from twin's own EventLog; the
  mirror is no longer consulted on the read path).
- Delete `nexus_server/memory_service.py` placeholder.

### Reorg Phase C — server internal grouping

Reorganise `nexus_server/*.py` flat layout into domain folders:
`auth/`, `twins/`, `chat/`, `chain/`, `views/`. Each with a one-page
README. Tests split by domain too.

## Next

### Phase D — Python module rename

Code rename to match doc names:

- `bnbchain_agent` → `nexus_core`
- `rune_twin` → `nexus`
- `rune_server` → `nexus_server`

Plus pyproject.toml package names, .env / conftest env vars, all
import strings. Single mechanical sweep + full test pass.

### Phase E — SDK internal grouping (in progress)

Done:
- Dropped `nexus/{tools,skills,mcp}/` re-export wrappers. The
  submodules are tombstones that raise ImportError pointing at
  ``nexus_core.*``; ``nexus.tools`` survives only to host
  :class:`ExtendedToolRegistry`, the genuinely Nexus-specific bit.

Still to do (heavier refactor, deferred):
- Submodule reorg of `nexus_core/`: split monolithic ``chain.py`` /
  ``state.py`` into ``chain/``,
  ``memory/``, ``contracts/``, ``runtime/``, ``llm/``, ``tools/``,
  ``distill/``. README per submodule. Holds until the class-rename
  decisions in Phase F land — splitting + renaming together is
  cheaper than two passes.

### Phase F — public class renames + namespace migration

Done:
- Logger namespace `rune.*` → `nexus_core.*`. All ``getLogger("rune.X")``
  call sites were updated.
- Object-storage bucket prefix `rune-agent-{token_id}` →
  `nexus-agent-{token_id}` (historical; the decentralised
  object-storage data plane has since been removed entirely).
  The legacy shared default
  ``rune-agent-state`` was also renamed in keystore.py / state.py /
  cli.py / web_demo.py / scripts; pre-existing testnet buckets
  abandoned (test phase, no real user data).

Class renames are now done in Phase H — see below.

### Phase G — env var + cache + chain-schema rename (done)

- Env vars `RUNE_*` → `NEXUS_*`. Migrated everywhere:
  `NEXUS_NETWORK`, `NEXUS_TESTNET_RPC` / `NEXUS_MAINNET_RPC`,
  `NEXUS_TESTNET_AGENT_STATE_ADDRESS` /
  `NEXUS_TESTNET_TASK_MANAGER_ADDRESS` /
  `NEXUS_TESTNET_IDENTITY_REGISTRY` (and mainnet equivalents),
  `NEXUS_PRIVATE_KEY`,
  `NEXUS_USE_TWIN`, `NEXUS_TWIN_BASE_DIR`,
  `NEXUS_TWIN_IDLE_SECONDS`, `NEXUS_DISABLE_TWIN_REAPER`,
  `NEXUS_CACHE_DIR`, `NEXUS_MAX_ATTACHMENT_BYTES`,
  `NEXUS_MAX_INLINE_TEXT_BYTES`. Test fixtures
  ``TEST_RUNE_*`` → ``TEST_NEXUS_*``. Twin manager's dynamic
  ``getattr(config, f"RUNE_{net_prefix}_…")`` lookups also updated.
- Cache directory ``.rune_twin_demo`` → ``.nexus_demo``. Pre-existing
  local caches abandoned per user instruction.
- Chain anchor schema id ``"rune.sync.batch.v1"`` →
  ``"nexus.sync.batch.v1"``. Pre-existing testnet anchors
  abandoned per user instruction (we can re-anchor from scratch).

### Phase H — public class renames (done)

The static-factory class ``Rune`` was retired in favour of
module-level functions. The 80% surface is now::

    import nexus_core
    rt = nexus_core.local()                       # was Rune.local()
    rt = nexus_core.testnet(private_key="0x...")  # was Rune.testnet(...)
    rt = nexus_core.mainnet(private_key="0x...")  # was Rune.mainnet(...)
    rt = nexus_core.builder().mock_backend().build()

Class rename map:

| Was | Is now |
| --- | --- |
| ``Rune`` (static-factory class) | dropped — use top-level functions |
| ``RuneBuilder`` | ``Builder`` |
| ``RuneProvider`` (the 5-provider facade) | ``AgentRuntime`` |
| ``RuneSessionProvider`` / ``RuneMemoryProvider`` / ``RuneArtifactProvider`` / ``RuneTaskProvider`` / ``RuneImpressionProvider`` | drop ``Rune`` prefix — ``SessionProvider`` etc. |
| ``RuneChainClient`` (BSC web3 wrapper) | ``BSCClient`` |
| ``RuneKeystore`` | ``Keystore`` |
| A2A's ``AgentRuntime`` (separate concept — A2A process container) | ``A2ARuntime`` (matches existing ``A2AAgentConfig`` convention; resolves the naming clash with the new SDK ``AgentRuntime``) |

Pre-existing class names had no production users, so the rename
is a clean break — no compatibility aliases.

### Phase I–N — BEP v0.2 design (in flight)

Detailed design lives in [`docs/design/nexus-architecture.md`](docs/design/nexus-architecture.md).
Summary:

- **I.** Monolith decomposition (`twin.py`, `state.py`,
  `chain.py`, `twin_manager.py`, `llm_gateway.py`
  → per-responsibility submodules with re-export shims).
- **J.** 5-namespace curated memory taxonomy (`facts/`, `episodes/`,
  `skills/`, `persona/`, `knowledge/`) + persona versioning
  generalised to all namespaces.
- **K.** Manifest schema v2 — chunked + Merkle, retention policy.
- **L.** Multi-writer `AgentStateExtension` v2 (writers set, reader
  grants, version counter, generation-counter eviction on transfer).
- **M.** Non-custodial mode Phase P (passkey-derived wallet).
- **N.** Non-custodial mode Phase A (smart-account / EIP-7702).

### Phase O — Falsifiable Evolution (next)

Detailed design lives in
[`docs/design/nexus-architecture.md`](docs/design/nexus-architecture.md).

Make every Nexus self-evolution edit a falsifiable, on-chain
contract: each `MemoryEvolver` / `SkillEvolver` / `PersonaEvolver` /
`KnowledgeCompiler` write declares **predicted fixes** and
**predicted regressions**; the next compaction round verifies, and
failed predictions auto-rollback. Inspired by the AHE paper
(*Agentic Harness Engineering*, Lin et al., arXiv:2604.25850v3,
Apr 2026), which proved this pattern lifts coding-agent pass@1 by
+7.3 pp over 10 iterations.

5 new event types in `nexus.sync.batch.v1` (folded into the same
schema since v0.2 hasn't shipped yet — no version bump):

| event_type | Purpose |
| --- | --- |
| `evolution_proposal` | Self-evolution edit + predicted fixes / regressions |
| `evolution_verdict` | Post-window evaluation against observed task-level deltas |
| `evolution_revert` | Storage pointer rollback when verdict fails |
| `evolution_user_approve` | User manually approves a `kept_with_warning` verdict |
| `evolution_user_revert` | User manually reverts an edit regardless of verdict |

7 sub-phases (~5 weeks total):

- **O.1** schema + per-namespace versioning (3 days)
- **O.2** evolver instrumentation — emit `evolution_proposal` (1 wk)
- **O.3** middleware as first-class file-level component (4 days)
- **O.4** verdict scorer + auto-rollback + LLM-classified
  `task_kind` (1 wk)
- **O.5** coordinator — round-robin priority across evolvers (3 days)
- **O.6** UI surface — Evolution timeline + manual approve/revert
  (1 wk)
- **O.7** external audit hooks — verdict sampling + audit grant tier
  (3 days)

**Ordering rationale.** Phase O ships *before* Phase M
(non-custodial). O is server-side and has zero browser-support
dependency; M waits on Safari 18+ PRF passkey support. More
importantly, agents created during the M-deferred period still get
falsifiable evolution from day one — agents created during an
O-deferred period would have a permanently-unverifiable evolution
history because the proposal/verdict pairs can't be retrofitted.

Conservative defaults baked in per AHE empirical findings:

- `PersonaEvolver` interval ≥ 30 days (was effectively weekly) —
  paper measured prose-level edits at −2.3 pp.
- Coordinator caps at 1 evolver writing per compaction round —
  paper measured stacking edits as sub-additive.
- Verdict scorer **only reverts on observed regressions**, never on
  predicted-but-unobserved ones — paper measured regression
  prediction precision indistinguishable from random (11.8% vs
  random 5.6%).

### Phase P — Recursive Projection (RLM-style chat context)

Detailed design lives in
[`docs/design/nexus-architecture.md`](docs/design/nexus-architecture.md).

Replace the single-call ``π(events, task, budget)`` chat
projection with a Recursive Language Model: load the EventLog as a
REPL variable, let the root LLM write code to slice / sub-LM-call
/ stitch. Inspired by Zhang, Kraska & Khattab,
*Recursive Language Models* (arXiv:2512.24601, Dec 2025), which
proved this pattern handles inputs ~2 orders of magnitude beyond
the base model's context window at the same or lower cost per
query.

The RLM **primitive itself is already shipped** as
``nexus_core.rlm`` (350 LOC + 22 conformance tests) in the
current branch. Phase P is about **integrating** it into Nexus
runtime consumers.

**Critical design decision: split DPM.**

| Path | Today | Phase P |
| --- | --- | --- |
| Chat projection | single-call ``π``, lossy compactor summary | RLM, runtime navigation, no compactor tax |
| Chain anchor | single-call ``π`` (same as chat) | **unchanged** — still the deterministic chunked manifest from BEP v0.2 §3 |

Chat answers and chain anchors describe the same EventLog
differently — and that's fine. Chain anchor stays deterministic
(stochastic RLM trajectories don't hash). Chat quality goes up
without dragging audit-ability down.

Sub-phases (~3 weeks total):

- **P.1** ``DigitalTwin.project_for_chat()`` using ``RLMRunner``;
  feature-flagged side-by-side dogfooding (1 wk)
- **P.2** Phase O.4 verdict scorer built on ``RLMRunner`` for long
  observation windows (3 days, after Phase O.4)
- **P.3** Attachment-by-reference — drop upfront distillation,
  use RLM at chat time (1 wk, deferrable, gated on cost analysis)
- **P.4** Operator monitoring — RLM iterations / sub-calls /
  truncated runs metrics + alerts (3 days)

**Risks** (all with mitigations in design doc): cost variance
from long-tail runs (capped via ``RLMConfig`` budgets + per-day
ceiling); quality regression on short queries (fast-path: skip
RLM if EventLog < threshold); sub-LM hallucination (caught by
ABC contract on final output).

**Ordering.** P.1 can run in parallel with Phase O.1–O.3 (no
contention). P.2 depends on Phase O.4 landing. P.3 + P.4 are
post-mainnet polish.

## Later

### Planning support — the missing capability

Today the agent reacts. It doesn't plan. Add:

- `nexus_core.planning` — `Plan` / `PlanStep` data model,
  `EventLogPlanStore` (DPM-aligned: plans are events).
- `nexus.planning` — `Planner` (LLM decompose / re-plan) +
  `PlanExecutor` (run steps via tools, persist progress).
- `twin.chat` integration: detect planning intent → decompose →
  return "I've broken this into N steps" + run in background.
- `/agent/plans` server endpoint + desktop Plans panel.

Full design: see [planning thread] (TBD — write up once Phase A–F
land).

### Non-custodial chain mode

Currently chain mode is custodial: server signs with
`SERVER_PRIVATE_KEY`. A future non-custodial mode would have users
sign in with their own wallet (MetaMask / WalletConnect) and pass
their address through.

Required changes:

- Auth flow: passkey → wallet signature
- ChainBackend: `private_key` becomes per-twin (from session) instead
  of server-wide
- Cost model: each twin pays its own gas

Out of scope until basic product is shipping.

### OpenAPI-driven view types

Server's Pydantic view types (`ChatMessageView`, `MemoryEntry`,
`AgentStateSnapshot`, `FileUploadResponse`, …) are duplicated in the
desktop's C# code. Hand-maintained. When server adds a field, desktop
silently doesn't see it.

Generate C# DTO from server OpenAPI schema (via
`datamodel-code-generator` or similar). Keeps types in lockstep, no
silent drift.

### Test taxonomy cleanup

`test_server_regression.py` is 65 tests in one file (>2000 lines).
Split by domain matching the Phase C server reorg:
`test_auth.py`, `test_twins.py`, `test_chat.py`, `test_chain.py`,
`test_views.py`. Plus `tests/integration/` for end-to-end SDK + Nexus
+ Server.

### Repo rename

`rune-protocol` → `nexus` (or `nexus-protocol`). Wait until product
strategy commits to dropping the "Rune" brand externally. Today the
brand is internal-facing only.

## Done (selected)

See [`HISTORY.md`](HISTORY.md) for the full chronology. Highlights:

- **S1–S6** — server-side cleanup. Each step retired a piece of the
  server's parallel intelligence layer in favour of routing through
  Nexus's `DigitalTwin`. The result: server is a pure HTTP frontend,
  Nexus is the single agent runtime.
- **Round 2-A/B/C** — desktop became a thin client. Deleted
  `LocalEventLog`, `RuneEngine`, JWT decoder for user-id scoping, the
  per-user data directory, the `_build_system_prompt` /
  `_build_context_messages` logic. `MainViewModel` is ~140 lines
  total now.
- **Bug 1/2/3** — post-S6 stability fixes around bucket auto-create,
  duplicate ERC-8004 registration, and UI visibility into chain
  failures.
- **Distiller move to SDK** — `attachment_distiller`'s reusable
  pipeline lives in `nexus_core.distiller`; server keeps a thin shim
  for the `record_distilled_event` persistence half.
