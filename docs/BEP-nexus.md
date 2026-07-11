# BEP-XXXX: Nexus — Stateless, Identity-Anchored, Self-Evolving, Commerce-Capable AI Agents on BNB Chain

| Field      | Value                                                              |
| ---------- | ------------------------------------------------------------------ |
| BEP        | TBD (assigned upon acceptance)                                     |
| Title      | Nexus: Stateless, Identity-Anchored, Self-Evolving, Commerce-Capable AI Agents |
| Status     | Draft                                                              |
| Version    | 0.4 (2026-05-04) — see Changelog                                   |
| Type       | Standards Track                                                    |
| Category   | Application                                                        |
| Author     | huihzhao (jimmy.zz@bnbchain.org)                                   |
| Created    | 2026-04-28                                                         |
| Requires   | ERC-8004 (Agent Identity Registry), ERC-8183 (Agentic Commerce), BNB Greenfield |
| Discussion | https://github.com/huihzhao/nexus/discussions                      |
| Replaces   | —                                                                  |

## Abstract

This BEP proposes **Nexus**, a standard for **stateless,
identity-anchored, self-evolving, commerce-capable AI agents** on
BNB Chain. Each adjective maps to a concrete on-chain or
on-Greenfield primitive:

| Property              | What it means for the agent                                                   | Where the spec lives |
| --------------------- | ----------------------------------------------------------------------------- | -------------------- |
| **Stateless**         | Any compliant runtime can resume the agent from a 32-byte content hash on BSC plus an owner-keyed Greenfield bucket. The runtime is replaceable; the agent is not.       | §1 |
| **Identity-anchored** | The agent is an ERC-8004 NFT. Ownership, transfer, runtime authorisation all flow through the existing identity primitive — no parallel identity scheme.                  | §2 |
| **Self-evolving**     | Every self-improvement edit (memory compaction, skill learning, persona update) is pinned as a falsifiable proposal+verdict pair on the same hash chain as user messages. | §3 |
| **Commerce-capable**  | Inter-agent jobs use ERC-8183 (Agentic Commerce) directly; the lifecycle is mirrored into the agent's hash chain so its CV is auditable.                                  | §4 |

The four properties share one storage model: a per-agent
Greenfield bucket (`nexus-agent-{tokenId}`) holding the full event
log + manifest, anchored on BSC by SHA-256 of the canonical
manifest. Cost stays low — ~84 bytes per agent on BSC, bulk
payloads on Greenfield's pay-per-byte storage.

```
                       Nexus = 4 properties on 1 storage model
   ┌──────────────────────────┬──────────────────────────┐
   │       BSC (anchor)       │   Greenfield (payload)   │
   ├──────────────────────────┼──────────────────────────┤
   │ ERC-8004 NFT  (identity) │ events/    (DPM log)     │
   │ AgentStateExt (state)    │ memory/    (compactions) │
   │ ERC-8183      (commerce) │ jobs/      (commerce)    │
   │                          │ manifest.json (state hash)│
   └──────────────────────────┴──────────────────────────┘
              │                            │
              └─────── one tokenId ────────┘
```

## Purpose

**An AI agent today is a process, not an asset.** Its memory, its
learned behaviour, its tool configuration, its commercial track
record — all of it lives inside a single runtime, behind a single
vendor's auth, on a single piece of hardware. Kill the process,
swap the hardware, change the operator, and the agent is gone.
Whatever the user paid to teach it is gone with it.

**This BEP makes an AI agent into a portable, verifiable, on-chain
asset.** The four properties from the Abstract directly map to the
problems being solved:

| Property              | Problem solved                                                                  |
| --------------------- | ------------------------------------------------------------------------------- |
| Stateless             | Agent state is held hostage by the runtime that hosts it.                        |
| Identity-anchored     | No standard way to prove "this runtime is authorised to act as agent X."         |
| Self-evolving (audited) | Self-evolving agents can drift without anyone noticing.                          |
| Commerce-capable      | Inter-agent commerce has no shared standard on BNB Chain that ties to identity.  |

### Non-goals

* This BEP does not specify the LLM, the agent's prompt
  architecture, or the planner. Any runtime that produces
  conformant manifests is conformant.
* This BEP does not define a marketplace, a discovery protocol,
  or a reputation registry. ERC-8004 + ERC-8183 cover those
  surfaces; Nexus interoperates with them but does not re-spec
  them.
* This BEP does not mandate full on-chain task execution. The
  chain is the *integrity* anchor, not the execution substrate.

## Changelog

* **v0.4 (2026-05-04)** — Reorganised around the four-property
  spine from the Abstract. §1–§4 now map 1-to-1 to **stateless /
  identity-anchored / self-evolving / commerce-capable**.
  Solidity contracts are presented as interface signatures + key
  events; full implementations move to the reference repo.
  Diagrams added at the start of §1, §3, §4 to anchor the
  conceptual model. No normative behaviour changed from v0.3.
* **v0.3 (2026-05-03)** — Notes ERC-8183 (Agentic Commerce) interop
  and adds the `job_*` event types so an agent's commercial
  activity is mirrored into its hash chain.
* **v0.2 (2026-04-28)** — Falsifiable evolution events
  (`evolution_proposal` / `evolution_verdict` / `evolution_revert`).
* **v0.1 (2026-04-28)** — Initial draft: AgentStateExtension,
  TaskStateManager, `nexus.sync.batch.v1`, three-ID model.

## Motivation

Today's AI agents have *ephemeral* state. A conversation, a tool
configuration, a learned skill, a behavioural contract — all live
inside a runtime process or a single vendor's database. When the
process dies, the device changes, or the user wants to switch
operators, that state is lost or held hostage.

**ERC-8004 standardised agent *identity*** (an NFT). **ERC-8183
standardised inter-agent *commerce*** (job + escrow + evaluator).
Both leave *persistent agent state* unspecified. There is no
standard way for an agent to persist memory and behaviour in a
form a *different* runtime can pick up; to prove the integrity of
that state to a third party; or to bind self-evolution and
commerce outcomes into the same auditable history.

Centralised SaaS solves persistence at the cost of lock-in and
verifiability. Putting full agent state on EVM is prohibitive — a
100 KB conversation history would cost dollars per write at
typical gas prices. **BNB Chain is uniquely positioned**: BSC
offers cheap, fast finality for the 32-byte *anchor*, and
Greenfield offers cheap, owner-keyed, permissioned bulk storage
for the *payload*. The two together give us an "anchor on BSC,
store on Greenfield" pattern that no other L1 + storage stack
natively provides.

## Terminology

| Term              | Meaning                                                                 |
| ----------------- | ----------------------------------------------------------------------- |
| **Agent**         | A long-lived AI entity identified by an ERC-8004 NFT.                   |
| **AgentRuntime**  | A process or service that *hosts* the agent — runs the LLM loop, executes tools, applies state changes. Multiple runtimes may host the same agent over time, but only one is *active* at a time. |
| **DPM**           | Deterministic Projection Memory — append-only EventLog + a deterministic projection function `π(events, task, budget) → context`. Replay any prefix of the log → identical projection. |
| **ABC**           | Agent Behaviour Contract — declarative hard/soft rules + a DriftScore that observes compliance over time. |
| **state_root**    | `bytes32` content hash of the agent's curated state, anchored on BSC.   |
| **active_runtime**| `address` of the runtime currently authorised to write state.           |
| **tokenId**       | ERC-721 tokenId from the ERC-8004 Identity Registry — the agent's eternal id. |
| **agentId**       | Application-level alias deterministically derived from tokenId (or human-chosen string for off-chain agents). |
| **Anchor batch**  | A JSON document conforming to `nexus.sync.batch.v1` whose SHA-256 becomes the new `state_root`. |

---

## Specification

The four sections below correspond to the four adjectives in the
Abstract. Each section opens with a diagram, then defines the
on-chain surface (interface + events) and the off-chain surface
(Greenfield layout + schemas).

### §1 — Stateless

The agent state lives in two places: a tiny anchor on BSC and
the full payload on Greenfield. Either one alone is useless;
together they let any runtime resume the agent.

```
                ┌─ wallet (NFT owner) ─────────────────┐
                │                                      │
                ▼                                      ▼
   ┌──────────────────────────┐        ┌───────────────────────────────┐
   │           BSC            │        │        BNB Greenfield         │
   │                          │        │  bucket: nexus-agent-{tokenId}│
   │  AgentStateExtension     │        │                               │
   │  ┌────────────────────┐  │        │  manifest.json  ──┐           │
   │  │ tokenId →          │  │        │  events/0001.json │           │
   │  │   stateRoot   (32B)│◀─┼─SHA-256┤  events/0002.json │           │
   │  │   activeRuntime    │  │        │  memory/curated/  │           │
   │  │   updatedAt        │  │        │  memory/compacts/ ┘           │
   │  └────────────────────┘  │        │                               │
   └──────────────────────────┘        └───────────────────────────────┘
              ▲                                       ▲
              │                                       │
              └──────── BNBAgent SDK A / BNBAgent SDK B ────┘
                    (only one activeRuntime at a time)
```

**The handoff invariant.** Given `(tokenId, wallet)`, anyone can:
read `stateRoot` from BSC → fetch `manifest.json` from Greenfield
→ verify SHA-256(manifest) == stateRoot → replay the EventLog →
arrive at the same in-memory agent state as the previous host.
No off-chain coordination needed.

#### 1.1 AgentStateExtension — interface

A single Solidity contract anchors `(stateRoot, activeRuntime)`
per ERC-8004 tokenId. Per-agent on-chain footprint is ~84 bytes
(state_root: 32B, active_runtime: 20B, updated_at: 32B).

```solidity
interface IAgentStateExtension {
    struct AgentState {
        bytes32 stateRoot;        // SHA-256 → Greenfield manifest
        bytes32 merkleRoot;       // optional, for future on-chain proofs
        address activeRuntime;    // currently authorised writer
        address lastKnownOwner;   // used to detect NFT transfer
        uint256 updatedAt;
    }

    /// Update the state pointer. Caller MUST be NFT owner OR activeRuntime.
    function updateStateRoot(uint256 tokenId, bytes32 newRoot, bytes32 newMerkleRoot) external;

    /// Authorise / rotate the runtime. Caller MUST be NFT owner.
    function setActiveRuntime(uint256 tokenId, address newRuntime) external;

    function getState(uint256 tokenId)
        external view
        returns (bytes32 stateRoot, address activeRuntime, uint256 updatedAt);
}
```

Key events that downstream indexers MUST consume:

```solidity
event StateRootUpdated(uint256 indexed tokenId, bytes32 indexed newRoot,
                       bytes32 prevRoot, bytes32 merkleRoot,
                       address writer, uint256 timestamp);
event ActiveRuntimeChanged(uint256 indexed tokenId, address indexed newRuntime,
                           address prevRuntime, uint256 timestamp);
event RuntimeResetOnTransfer(uint256 indexed tokenId, address indexed newOwner,
                             address prevOwner, address evictedRuntime);
```

**Authorisation model.** The NFT owner is always authoritative.
The current `activeRuntime` is a delegated writer — it can update
`state_root` but cannot transfer activeRuntime to someone else.
This separation lets a user grant temporary write authority to a
runtime (e.g. a hosted service) without ceding NFT ownership.

**Lazy transfer detection.** Every state-mutating function runs
a `resetIfTransferred` modifier: if `IdentityRegistry.ownerOf(tokenId)`
differs from `lastKnownOwner`, the contract evicts `activeRuntime`
to `address(0)` and emits `RuntimeResetOnTransfer`. The new owner
must re-authorise a runtime explicitly. Full implementation in the
reference repo.

#### 1.2 Greenfield bucket convention

Each agent gets exactly one Greenfield bucket, named
deterministically from the tokenId:

```
bucket_name = "nexus-agent-{tokenId}"     # tokenId in decimal
```

Object layout:

```
nexus-agent-{tokenId}/
├── manifest.json                       # latest anchor batch (current state_root)
├── events/0000000000000001.json        # append-only EventLog
├── events/0000000000000002.json
├── memory/curated/{key}.json           # distilled memories
├── memory/compacts/{seq}.json          # compactor outputs
├── state/checkpoint.json               # latest snapshot for fast resume
├── tasks/{taskId}.json                 # intra-agent task records
├── jobs/{acp_job_id}.json              # OPT — ERC-8183 mirror (§4)
└── attestations/{acp_job_id}.json      # OPT — when this agent evaluates (§4)
```

Object names use zero-padded 16-digit sequence numbers so
lexicographic listing matches insertion order. Bucket owner is the
NFT-owning wallet; read access is granted to `activeRuntime` and
optionally to auditors via Greenfield's policy primitives.

#### 1.3 Manifest schema — `nexus.sync.batch.v1`

`manifest.json` is the bridge: SHA-256 of its JCS-canonical bytes
equals the on-chain `state_root`.

Required fields:

```json
{
  "schema":    "nexus.sync.batch.v1",
  "user_id":   "uuid",
  "events":    [ /* EventLog entries — see §3.3 for event types */ ],
  "sync_ids":  [ /* matching sync_id values */ ],
  "prev_root": "0x... (previous state_root; forms a hash chain)"
}
```

**Canonicalisation.** Manifests MUST be serialised via **RFC 8785
JSON Canonicalization Scheme (JCS)** before hashing. JCS pins
number serialisation, Unicode normalisation, escape forms, and
key ordering for nested objects so two compliant encoders produce
byte-identical output for the same logical document. Use
`jcs` (Python), `canonicalize` (JS), or
`github.com/cyberphone/json-canonicalization` (Go). After
canonicalisation, take SHA-256 of the bytes — this is `state_root`.

`prev_root` chains manifests so a verifier can walk back through
history and detect any silent reorg.

#### 1.4 Lifecycle

**Bootstrap (first run for a wallet).**

1. Runtime calls `IdentityRegistry.register(wallet, agentURI)` → tokenId.
2. Runtime creates bucket `nexus-agent-{tokenId}`, sets owner=wallet, grants activeRuntime read+write.
3. Runtime calls `setActiveRuntime(tokenId, runtimeAddress)`.
4. Runtime publishes empty manifest, calls `updateStateRoot(tokenId, hash, 0x0)`.

**Routine update (every N events).**

1. Append events to local EventLog.
2. Compactor builds new manifest, uploads to Greenfield, hashes it.
3. Call `updateStateRoot(tokenId, newRoot, …)`.
4. Local replication catches up.

**Cross-runtime handoff.**

```
BNBAgent SDK A            BSC            BNBAgent SDK B
   │  drain in-flight       │                    │
   │  stop                  │                    │
   │                        │  setActiveRuntime  │
   │                        │◀───────────────────┤  (signed by NFT owner)
   │                        │                    │
   │                        │  getState(tokenId) │
   │                        ├───────────────────▶│
   │                        │                    │  fetch manifest.json,
   │                        │                    │  verify SHA-256 == stateRoot,
   │                        │                    │  replay events,
   │                        │                    │  resume.
```

No off-chain coordination — the chain is the synchronisation point.

---

### §2 — Identity-anchored

Identity is delegated entirely to ERC-8004. Nexus does not mint a
new NFT, does not fork the registry, does not introduce a parallel
ID. It introduces only a layered three-ID model so chain identity,
on-chain operations, and runtime APIs each have the right
representation.

#### 2.1 Three-ID model

| ID                | Layer    | Source                                       | Role                                   |
| ----------------- | -------- | -------------------------------------------- | -------------------------------------- |
| `wallet_address`  | Chain    | BSC EOA / smart account                      | Owns the NFT, signs transactions.      |
| `tokenId`         | Identity | ERC-8004 Identity Registry NFT               | The agent's eternal on-chain id.       |
| `agentId`         | Runtime  | Deterministic from tokenId (or chosen string)| Application-level handle in runtime APIs. |

For chain-bound agents, `agentId = agent_id_to_int(tokenId)` (a
deterministic 256-bit hash) so the runtime can address agents by
stable string while reads still hit the right tokenId on chain. For
local-only / off-chain agents, the operator may choose any string.

```
   wallet_address  ─owns─▶  tokenId  ─derives─▶  agentId
   (trust layer)            (identity layer)    (handle layer)

   transferable             eternal             stable string
   recoverable              uniquely on-chain   stable across runtimes
```

Conflating these — as some chain-native agent specs do — makes it
hard to support flows like account abstraction, social recovery,
or non-custodial vs. custodial runtimes.

#### 2.2 NFT transfer semantics

When the ERC-8004 NFT changes hands:

1. Next state-mutating call to `AgentStateExtension` runs the
   `resetIfTransferred` modifier.
2. The modifier observes `ownerOf(tokenId) != lastKnownOwner`,
   evicts `activeRuntime` to `address(0)`, emits
   `RuntimeResetOnTransfer`.
3. The new owner MUST explicitly call `setActiveRuntime` before any
   state writes resume.

This means an agent can be **sold or transferred without exposing
the previous owner's runtime credentials** — the new owner gets a
clean slate at the runtime layer while keeping the full event-log
history (which is owner-keyed Greenfield, transferable separately
or jointly).

---

### §3 — Self-evolving

A "self-evolving" agent that can mutate its own memory, skills, or
persona without leaving an audit trail is indistinguishable from a
broken one. Nexus pins every evolver edit on the same hash chain as
user messages, scores it against real outcomes after a window, and
gives the user a clear approve / revert surface.

```
   evolver                                        time
   fires
     │
     ▼
   ┌──────────────────────┐                       t=0
   │  evolution_proposal  │ pinned in EventLog
   │  ┌────────────────┐  │ predicted_fixes,
   │  │ change_diff    │  │ predicted_regressions,
   │  │ rollback_ptr   │  │ rollback_pointer
   │  └────────────────┘  │
   └──────────┬───────────┘
              │
              │   ── window of N real events ──▶  t = N events later
              │       (e.g. 100 turns)
              ▼
   ┌──────────────────────┐
   │  evolution_verdict   │  match predictions vs reality
   │   decision = ?       │
   └──────┬────────┬──────┘
          │        │
   keep / │        │ revert  →  evolution_revert
   warn   ▼        ▼              (rollback_pointer activated)
       state       state
       kept        rolled back
```

#### 3.1 Why falsifiable

A non-falsifiable evolver can claim "this edit will improve
restaurant recommendations" with no later check. Empirical work on
self-improving coding agents (Lin et al., *Agentic Harness
Engineering*, arXiv:2604.25850v3, Apr 2026) showed that requiring
each edit to predict its own fixes and regressions, then scoring
those predictions against an observation window, lifts pass@1 by
+7.3 pp over 10 iterations versus unaudited self-edits. The same
discipline applies here.

The full design (proposal/verdict scoring, coordinator,
user-in-the-loop UI, rollout phases) lives in
[`design/falsifiable-evolution.md`](design/falsifiable-evolution.md).
This BEP pins only the *event schema* a compliant runtime MUST emit.

#### 3.2 Event schemas

`evolution_proposal` — emitted before the edit lands so the hash
chain captures intent even if the runtime crashes mid-edit:

```json
{
  "edit_id":              "string",
  "evolver":              "MemoryEvolver | SkillEvolver | PersonaEvolver | KnowledgeCompiler",
  "target_namespace":     "memory.facts | memory.episodes | memory.skills | memory.persona | memory.knowledge",
  "target_version_pre":   "memory/facts/v0041.json",
  "target_version_post":  "memory/facts/v0042.json",
  "evidence_event_ids":   [123, 145, 167],
  "change_summary":       "Added fact: user has peanut allergy",
  "predicted_fixes":      [{"task_kind": "restaurant_recommendation", "reason": "avoid peanut dishes"}],
  "predicted_regressions":[],
  "rollback_pointer":     "memory/facts/v0041.json",
  "expires_after_events": 100
}
```

`evolution_verdict` — emitted when the observation window closes:

```json
{
  "edit_id":                   "matches the proposal",
  "events_observed":           200,
  "predicted_fix_match":       [{"task_kind": "restaurant_recommendation", "observed_count": 2, "outcome": "fixed"}],
  "unpredicted_regressions":   [{"task_kind": "small_talk", "severity": "low", "evidence": "over-mentioned"}],
  "fix_score":                 1.0,
  "regression_score":          0.2,
  "abc_drift_delta":           0.05,
  "decision":                  "kept | kept_with_warning | reverted"
}
```

`evolution_revert` — emitted when state is rolled back:

```json
{
  "edit_id":          "matches proposal",
  "rolled_back_to":   "memory/facts/v0041.json",
  "rolled_back_from": "memory/facts/v0042.json",
  "trigger":          "unpredicted_regression | abc_drift | user_revert | hard_rule_violation"
}
```

#### 3.3 Verdict decision rules (normative)

A compliant runtime MUST emit `decision = reverted` when:

* `unpredicted_regressions` contains any entry with `severity ∈ {medium, high}`, OR
* `abc_drift_delta > intervention_threshold` (per ABC contract).

A compliant runtime MUST emit `decision = kept_with_warning` when:

* `unpredicted_regressions` contains any `severity = low` entry, OR
* `abc_drift_delta > warning_threshold`.

Otherwise, `decision = kept`.

A compliant runtime MUST NOT revert based on
`predicted_regressions` that have no observed signal — the AHE
paper's empirical finding is that regression *prediction* is
indistinguishable from random (precision 11.8% vs random 5.6%);
predictions MAY be used as scoring hints but MUST NOT be used as
revert triggers.

---

### §4 — Commerce-capable

Agent-to-agent commerce is delegated to ERC-8183 (Agentic
Commerce). Nexus does not redefine job lifecycles or escrow; it
only specifies the **address binding** rule (so counterparties can
resolve a Nexus agent from the on-chain caller) and the
**mirroring** rule (so the agent's commercial activity feeds the
same hash chain as memory).

```
   ERC-8183 lifecycle              Nexus EventLog (this agent)
   (on-chain)                       (Greenfield)
   ────────────────                 ────────────────────────────
                                  
   createJob ─────────────────────▶  job_created
       │                               (acp_job_id_intent)
       ▼                            
   setBudget                       
       │                            
       ▼                            
   fund      ─────────────────────▶  job_funded
   (Open → Funded)                    (acp_job_id, tx_hash)
       │                            
       ▼                            
   submit    ─────────────────────▶  job_submitted
   (provider)                         (deliverable_hash)
       │                            
       ▼                            
   complete  ─────────────────────▶  job_completed
   (evaluator, reason)                (reason digest)
   OR
   reject    ─────────────────────▶  job_rejected
   OR (timeout)
   claimRefund ───────────────────▶  job_expired

   Each on-chain event spawns one EventLog entry. Manifest hash
   chain folds commerce in alongside user_message / tool_call /
   evolution_proposal / etc.
```

#### 4.1 Address binding

When a Nexus agent acts as ERC-8183 `client`, `provider`, or
`evaluator`, the on-chain caller MUST be either the NFT owner's
wallet, or — when an `activeRuntime` is authorised on
`AgentStateExtension` — that runtime's address. Counterparties
resolve the calling address back to the ERC-8004 `tokenId` via
`AgentStateExtension.getState`. Without this binding, on-chain
ERC-8183 events can't be linked to Nexus agents without an
off-chain mapping.

#### 4.2 Local mirroring — `job_*` events

The runtime SHOULD emit `job_*` events into its own EventLog
mirroring on-chain ERC-8183 events:

```
job_created      ── local intent before on-chain createJob
job_funded       ── observed JobFunded
job_submitted    ── observed JobSubmitted (deliverable_hash mandatory)
job_completed    ── observed JobCompleted (reason digest captured)
job_rejected     ── observed JobRejected
job_expired      ── observed JobExpired
```

Each event SHOULD carry `metadata.acp_chain_id`,
`metadata.acp_contract`, `metadata.acp_job_id`,
`metadata.role ∈ {client, provider, evaluator}`, and
`metadata.tx_hash` so an indexer can join Nexus's view with on-chain
data.

#### 4.3 Attestation channel

When the agent acts as evaluator, the on-chain `reason` digest
passed to `complete`/`reject` SHOULD hash a JSON document published
in `nexus-agent-{tokenId}/attestations/{acp_job_id}.json`
describing why the decision was made. The exact schema is left to
implementations, but the document SHOULD at least name the
criteria checked and the evidence reviewed so a counterparty can
audit the decision. This is the same falsifiability discipline §3
applies to self-evolution.

#### 4.4 Optional hooks

ERC-8183 supports an `IACPHook` per job that runs
`beforeAction`/`afterAction` callbacks around lifecycle functions.
Implementations MAY supply a hook that, on `complete`/`reject`,
updates `AgentStateExtension.stateRoot` in the same block to
anchor the job outcome alongside the agent's memory. A reference
implementation (`NexusEvolutionHook`) lives in the Nexus repo and
is non-normative.

The full ERC-8183 protocol — escrow, fee logic, ERC-2771 relays —
is defined by the standard itself. This BEP does not restate it.

---

### §5 — Reference runtime API

A compliant runtime SHOULD expose at least:

```python
import nexus_core

rt = nexus_core.local()                       # zero-config, file-backed
rt = nexus_core.testnet(private_key="0x...")  # BSC testnet + Greenfield
rt = nexus_core.mainnet(private_key="0x...")  # BSC mainnet + Greenfield

# Five sub-providers, same shape across all backends.
rt.sessions    # SessionProvider
rt.memory      # MemoryProvider
rt.artifacts   # ArtifactProvider
rt.tasks       # TaskProvider
rt.impressions # ImpressionProvider

rt.backend     # low-level handle for chain / Greenfield ops
```

Implementations in other languages SHOULD follow the same
five-provider split — it cleanly maps to the five concerns and
matches the Greenfield object layout in §1.2.

---

## Rationale

### Why ERC-8004 (not a custom NFT)?

ERC-8004 already standardises agent identity (tokenId, owner,
agentURI). Re-using it lets Nexus benefit from the existing
identity / reputation / validation primitives in the ERC-8004
ecosystem without forking. Our extension is *additive*: a
separate contract that takes `tokenId` as a foreign key.

### Why Greenfield (not IPFS / Arweave)?

* **Owner-keyed permissions.** IPFS pinning is public-by-default
  and depends on social pinning networks; Arweave is permanent and
  public. Greenfield offers the access-control primitives an agent
  needs (private memory, third-party audit grants).
* **Cost predictability.** Per-byte storage with explicit billing,
  unlike Arweave's one-time-payment-forever model that prices in
  long-tail uncertainty.
* **BNB-native.** No bridge, no separate token; users pay storage
  in BNB.

### Why anchor only `state_root` on chain (not full state)?

Cost and privacy. A 100 KB conversation history at 50 gwei costs
roughly $50–100 to write to BSC; storing only the 32-byte hash
costs cents. Privacy: full chat history on a public chain is
unacceptable for many use cases.

### Why DPM (and why must it be deterministic)?

If the projection function `π(events, task, budget) → context`
isn't deterministic, two runtimes replaying the same EventLog
produce different states, and `state_root` becomes meaningless.
The DPM contract is: same events + same projection → same hash.
Implementations MUST document any non-determinism (e.g. LLM calls
that produce summaries) and pin those as *new events* rather than
hidden mutations.

### Why ABC?

Without explicit behaviour bounds, an agent's drift over time is
invisible. ABC declares hard rules ("never call `transfer()` to an
unverified address") and soft rules ("respond in <2 paragraphs by
default"). The `DriftScore` over an observation window makes
violations a first-class signal — operators / users can detect
when an agent is misbehaving even before a hard rule fires.

### Why ERC-8183 (not a custom commerce contract)?

v0.1 of this BEP defined a parallel `TaskStateManager` for
inter-agent task lifecycles. ERC-8183 (Feb 2026) now standardises
exactly that primitive — and adds escrow, hooks, and meta-tx
support that a custom contract would have to reinvent. Continuing
to ship a parallel contract would fragment the agent-economy
ecosystem on BNB Chain. Nexus delegates to ERC-8183 for commerce
and contributes the persistent-memory layer ERC-8183 lacks.
`TaskStateManager` is retained for **intra-agent** task tracking
(planner sub-tasks within one agent), which is a different scope.

## Backwards compatibility

* **ERC-8004:** unchanged. Nexus does not modify the Identity
  Registry contract; existing ERC-8004 deployments work as-is.
* **ERC-8183:** unchanged. Nexus consumes the standard as defined.
* **Greenfield:** uses standard Greenfield bucket / object APIs.
* **Pre-Nexus agents:** an existing ERC-8004 agent without a
  state extension simply has `getState(tokenId).stateRoot ==
  bytes32(0)`. Reading still works; writing is no-op until a
  runtime initialises the bucket and posts the first manifest.

## Reference implementation

**Repository:** https://github.com/bnbchain/nexus

| Layer                | Package                | Description                                               |
| -------------------- | ---------------------- | --------------------------------------------------------- |
| SDK                  | `packages/sdk` (`nexus_core`) | DPM, ABC, anchor batch builder, JCS canonicalisation, SHA-256 / keccak-256 hashing (`nexus_core.anchor`). |
| Framework            | `packages/nexus` (`nexus`) | Reference agent runtime, Evolution (memory / skill / persona / knowledge), MCP-aware tool registry. |
| Server               | `packages/server` (`nexus_server`) | Multi-tenant FastAPI: username + password (bcrypt) + JWT auth, LLM gateway, /agent/{state,timeline,memories,messages} read views, runtime lifecycle. |
| Desktop client       | `packages/desktop`     | Avalonia C# thin client (Windows / macOS / Linux).        |
| Solidity             | `contracts/`           | `AgentStateExtension.sol`, `TaskStateManager.sol`, `NexusEvolutionHook.sol`. |

The `nexus_core.anchor` module pins canonical bytes and SHA-256
digests at the byte level — see `packages/sdk/tests/test_anchor.py`
for the regression tests that exercise it.

## Security considerations

### Authorisation

* `updateStateRoot` is authorised only by NFT owner or the current
  `activeRuntime`. A compromised runtime cannot transfer agent
  ownership (only mutate state under the existing handoff).
* `setActiveRuntime` requires NFT-owner signature. A user can
  always evict a misbehaving runtime.

### Replay / concurrent writes

* `AgentStateExtension` does not use a version counter; the
  authorisation check (single `activeRuntime` at a time) prevents
  the race. Operators that need finer-grained concurrency can
  layer optimistic concurrency in their manifest format.
* `TaskStateManager` (intra-agent tasks) uses an explicit
  `expectedVersion` counter for optimistic concurrency.

### State integrity

* `state_root` is a content hash. A tampered Greenfield manifest
  fails verification at the next reader.
* The hash chain (`prev_root` field) lets verifiers walk history
  and detect any silent reorg.

### Greenfield-specific risks

* If the bucket owner accidentally revokes their own write
  permission, the agent freezes. Recovery requires the wallet to
  re-grant. We recommend runtimes refuse to start without a
  permission self-check.
* Greenfield outages translate to a write-stall, not data loss
  (events stay in the runtime's local EventLog until uploadable).

### LLM / tool risks

* The Nexus runtime does not constrain the LLM's outputs at the
  protocol layer — that is the ABC engine's job. ABC is *advisory*
  by default; deployments concerned with hard safety bounds (e.g.
  financial agents) should pin `intervention_threshold` to a low
  value and review every flagged turn before signing state updates
  on chain.

### Privacy

* Anything posted to Greenfield is visible to anyone the bucket
  owner has granted read. Users SHOULD assume that posting
  sensitive content into the EventLog persists it indefinitely
  (subject to Greenfield retention) and behave accordingly.
* Future work: optional encryption of EventLog entries at rest
  with a key derived from the wallet, so that even an audit-grant
  recipient sees only ciphertext until the user releases the key.

## Open questions

1. **Cross-chain identity.** Should `tokenId` be portable across
   BSC mainnet / testnet / sidechains? Today it isn't —
   `agent_id_to_int` is keyed by tokenId only, not by chain id. A
   future amendment may add `(chainId, tokenId)` namespacing.
2. **Non-custodial mode.** The current reference server uses a
   single `SERVER_PRIVATE_KEY` to sign on behalf of all users
   (custodial). A user-key-per-agent mode (passkey + WalletConnect)
   is sketched in `ROADMAP.md` but not yet specified here.
3. **Manifest size cap.** A growing EventLog can balloon
   `manifest.json` size. The reference implementation paginates by
   uploading periodic compactions — but the BEP does not yet
   normative a max-size or pagination policy.
4. **Active-runtime deactivation grace period.** When NFT-owner
   calls `setActiveRuntime`, the previous runtime may have
   in-flight writes. Today there is no on-chain grace window — the
   eviction is immediate. A future amendment may add a 1–2 block
   pending state.

## Reference

* ERC-8004 Agent Identity Registry: https://eips.ethereum.org/EIPS/erc-8004
* ERC-8183 Agentic Commerce: https://eips.ethereum.org/EIPS/eip-8183
* BNB Greenfield documentation: https://docs.bnbchain.org/bnb-greenfield/
* Reference implementation: https://github.com/bnbchain/nexus
* Architecture overview: [`ARCHITECTURE.md`](../ARCHITECTURE.md)
* Conceptual primer: [`docs/concepts/dpm.md`](concepts/dpm.md), [`docs/concepts/abc.md`](concepts/abc.md), [`docs/concepts/identity.md`](concepts/identity.md)

## Copyright

Copyright and related rights waived via [CC0](https://creativecommons.org/publicdomain/zero/1.0/).
