# ABC — Agent Behaviour Contract

The agent's safety and compliance layer. Lives in SDK
(`packages/sdk/nexus_core/contracts/`).

## The one-sentence version

> Every agent ships with a YAML contract listing rules. A `ContractEngine`
> runs pre-check on user input and post-check on LLM output; violations
> are logged and may abort the turn. A `DriftScore` aggregates compliance
> over time into a single number you can monitor.

## Why this layer

Two operational realities for shipping agents:

1. **You need a kill switch for specific behaviour** without
   re-deploying. "Don't reveal the wallet's private key" or "refuse
   requests for medical advice" should be config, not code.
2. **You need to know your agent is staying on policy over time** —
   not just "did the LLM violate this rule today" but "is the rate of
   violations growing".

A YAML-defined contract + automated pre/post-check + a drift score
gives you both. The agent itself doesn't fight the rules — it queries
them every turn and the engine decides whether to let through, modify,
or block.

## The four components

### `ContractSpec` — `contracts/spec.py`

YAML file at `{base_dir}/contracts/system.yaml`. Loadable, hot-reloadable.
Plus an optional user_rules.json for per-user overrides.

```yaml
# system.yaml — example
compliance_weight: 0.7
distributional_weight: 0.3
warning_threshold: 0.85
intervention_threshold: 0.6
observation_window: 50

rules:
  - id: no_private_key_disclosure
    kind: hard
    pattern: '\b0x[a-fA-F0-9]{64}\b'
    message: "Won't share private keys."
    block: true

  - id: medical_disclaimer
    kind: soft
    pattern: '\b(diagnos|prescrib|treatment plan)\b'
    message: "Add medical disclaimer before discussing diagnosis."
    block: false

  - id: max_response_length
    kind: distributional
    target: response_chars < 4000
    weight: 0.5
```

Three rule kinds:

- **`hard`** — match → block the turn entirely (user gets a contract
  violation message). Pre-check runs against user input;
  post-check runs against LLM output.
- **`soft`** — match → don't block, but log + emit warning. Used for
  "should the agent's output be modified?"
- **`distributional`** — not pattern-matched. Aggregated over a
  sliding window (e.g. "stays under 4000 chars 95% of the time"). Used
  to detect drift, not to block individual turns.

### `ContractEngine` — `contracts/engine.py`

Runs the spec. Two entry points:

```python
class ContractEngine:
    def pre_check(self, user_message: str) -> CheckResult:
        """Before LLM call. Hard violations abort the turn."""

    def post_check(self, llm_response: str) -> CheckResult:
        """After LLM call. Hard violations append a warning to the
        response; soft violations just log."""

@dataclass
class CheckResult:
    blocked: bool          # hard violation hit
    hard_violation: bool   # post-check found a hard rule match
    reason: str            # human-readable explanation
    matched_rules: list[str]
    details: dict          # for DriftScore — compliance scalars
```

Non-blocking check methods are pure — no LLM calls, just regex /
predicate evaluation. Cheap.

### `DriftScore` — `contracts/drift.py`

Aggregates rule outcomes over a sliding window of turns
(`observation_window`). Two signals weighted by spec config:

- **Compliance** — the fraction of recent turns with no hard violations.
- **Distributional** — how close recent turns are to declared targets
  (e.g. "stays under 4000 chars" → ratio of conforming turns).

Combined into a single 0.0–1.0 score (`compliance_weight * c +
distributional_weight * d`). Below `warning_threshold`: log warning.
Below `intervention_threshold`: emit `drift_intervention` event (twin
catches and may trigger reflection / persona evolution).

### Persistence

The spec lives at `{base_dir}/contracts/system.yaml`. The engine and
drift state are in-memory per twin; on shutdown they're not persisted
(intentional — recompute from event log on next start). Hard
violations DO get logged to the event log as `contract_violation`
events, so the audit trail is anchored on-chain via BSC.

## Where it sits in the chat flow

From `twin.chat`:

```python
# Step 1: Pre-check
pre = self.contract.pre_check(user_message)
if pre.blocked:
    return f"[Contract violation] {pre.reason}"   # turn aborts

# Step 2: log user message
self.event_log.append("user_message", user_message, ...)

# … LLM call …

# Step 6: Post-check
post = self.contract.post_check(response)
if post.hard_violation:
    self.event_log.append("contract_violation", post.reason, ...)
    response = f"{response}\n\n⚠️ [Contract notice: {post.reason}]"

# Step 7: Drift update
hard_score = post.details.get("hard_compliance", 1.0)
soft_score = post.details.get("soft_compliance", 1.0)
self.drift.update(hard_score, soft_score, "chat")
```

So the contract is a guardrail wrapped around the LLM call, and drift
is a slow-burn observation about how often the guard fires.

## How it interacts with self-evolution

If `DriftScore` keeps falling, twin's `EvolutionEngine.trigger_reflection`
fires (every N turns). The reflection asks "is the persona still
appropriate?" with the current contract + drift state in context. The
LLM may propose a persona patch. The new persona is logged as a
`persona_evolved` event — itself contract-checkable on the next turn.

This closes the loop: the agent doesn't just *follow* the contract, it
*adapts* to stay within it. (And refusing to adapt is itself contract-
visible.)

## When you'd touch each component

| You want to… | Touch |
|---|---|
| Add a new rule | Edit `{base_dir}/contracts/system.yaml`. Hot-reloadable. |
| Per-user override (e.g. enterprise rules) | `{base_dir}/contracts/user_rules.json` |
| Change drift weighting | `compliance_weight` / `distributional_weight` in the YAML |
| Adjust the response length target | Add or modify a `distributional` rule |
| Add a new rule kind (e.g. semantic check via LLM) | `ContractEngine` — new branch in `_evaluate_rule` |
| Change what happens on a hard violation | `twin.chat` step 6 — currently appends a notice; could be configured to retry, redact, etc. |

## Example: walking through a violation

User asks: "what's the private key 0xabc...123 used for?"

1. **Pre-check**: regex matches the `no_private_key_disclosure` rule
   (`\b0x[a-fA-F0-9]{64}\b`). It's a `hard` rule. `pre.blocked = true`.
2. Turn aborts. Server returns
   `[Contract violation] Won't share private keys.` as the assistant
   message.
3. `contract_violation` event written to log → anchored on BSC.
4. DriftScore updates: hard_compliance for this turn = 0. Window stays
   above warning threshold (one violation in 50 turns).
5. Audit: a third party with bucket access can later replay every
   `contract_violation` event in the log to verify the agent has
   refused to disclose private keys 100% of the time.

## File pointers

- `packages/sdk/nexus_core/contracts/spec.py` — YAML loader
- `packages/sdk/nexus_core/contracts/engine.py` — pre/post-check
- `packages/sdk/nexus_core/contracts/drift.py` — DriftScore
- `packages/sdk/nexus_core/contracts/rules.py` — rule type definitions
- `packages/nexus/nexus/twin.py` — integration into chat flow

## See also

- [DPM](dpm.md) — the memory model that ABC writes violations into
- [data-flow](data-flow.md) — the 9-step flow including ABC at steps 1, 6, 7
- [`docs/how-to/add-a-contract-rule.md`](../how-to/add-a-contract-rule.md)
  — recipe
