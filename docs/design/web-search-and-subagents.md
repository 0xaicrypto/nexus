# Web Search + Subagent Orchestration — Design Proposal (Draft)

Status: **Proposed** (2026-06-14)
Owner: TBD
Related: `docs/design/scheduled-tasks-and-calendar.md`,
         `docs/design/nexus-architecture.md`,
         `packages/server/nexus_server/retrieval_tiers.py`

This proposes two related capabilities:

1. **Web search via chat** — the medic can ask "what does NCCN say
   about 8 mm pulmonary nodule follow-up in a former smoker?" and
   get a cited, web-grounded answer.

2. **Parallel subagents** — Claude-style orchestrator pattern where
   a single medic question fans out to multiple specialist subagents
   running concurrently (one searches NCCN, one searches RadioPaedia,
   one queries Layer 1 patient memory, etc.), then a synthesizer
   composes the final answer.

These two design together because web search is the first concrete
*specialist* that subagents would dispatch to — the orchestrator
pattern needs a non-trivial first parallel branch to justify itself.

---

## Part 1 — Web Search

### What v1 had

v1 ran a `web_search` tool through the LLM function-calling pathway
(`packages/server/nexus_server/tools_*.py`). When the medic asked
something the LLM judged needed external info, it called `web_search`
(server-side via SerpAPI / Brave / Tavily — operator-configurable),
got back ranked snippets, and grounded its reply.

v2 chat (`chat_router_v2` + `retrieval_tiers`) is SSE-streaming Gemini
with NO function-calling. So we need to add a search-grounding
mechanism that fits the tier pipeline.

### Recommendation: add a 4th retrieval tier

Today's tiers:
- **T1** cached views (sub-50ms)
- **T2** templated SQL projections (sub-200ms)
- **T3** LLM synthesis over patient context (~3s)

Add:
- **T4 — Web-grounded LLM synthesis** (~5–10s)
  - Pre-step: query intent classifier decides T1 / T2 / T3 / T4
  - When T4 fires: send the question to a search provider, fetch
    top-N results (snippet + URL), inject them as additional
    context into the same Gemini call alongside PATIENT CONTEXT,
    and produce citations carrying both `[Nxx]` patient nodes
    AND `[Wxx]` web sources

### Tier classifier

The existing `_classify_tier` in `retrieval_tiers.py` decides T1/T2/T3
today based on keyword heuristics. Extend with a T4 branch:

```python
# Trigger T4 when the question is generic / external-knowledge:
WEB_INTENT_PATTERNS = [
    r"\b(NCCN|ACR|ESMO|UpToDate|guideline|recommend|standard of care)\b",
    r"\b(literature|paper|study|trial|meta-analysis|systematic review)\b",
    r"\b(latest|2024|2025|recent|new|emerging)\b",
    r"\b(what does .* say about|consensus on|evidence for)\b",
    r"\b(指南|文献|最新|共识|证据)\b",
]
PATIENT_INTENT_PATTERNS = [
    r"\b(this patient|him|her|J\.D\.|MRN-\d|患者|这个病人)\b",
    # If the question is clearly patient-specific, prefer T3 even
    # if it mentions a guideline term ("what does NCCN say about
    # OUR patient's nodule" → T3 with NCCN context implicit).
]
```

Mixed-intent questions ("does this patient's nodule fit NCCN
short-interval criteria?") go T4 — Web context + Patient context
both injected into the same LLM call.

### Search providers

| Provider                      | Pros                                                                                 | Cons                                                          | Recommendation                                                |
| ----------------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------- | ------------------------------------------------------------- |
| **Tavily**                    | Built for AI agents; returns clean snippet+URL JSON; `include_raw_content=true`      | Pay-per-search; rate-limited                                  | **Pick this** for v1 — least friction                          |
| Brave Search API              | Cheaper than Tavily; clean API                                                       | Returns raw search results (need own snippet extraction)      | Alternative if Tavily cost is an issue                        |
| Gemini's grounding-with-search | Native to the LLM; no separate API                                                   | Limited to Gemini's search corpus; less control over citations | Worth piloting in parallel — best UX if quality is acceptable |
| SerpAPI                       | Google results verbatim                                                              | Expensive; clinical answers buried in commercial noise         | Skip                                                          |

API key lives in `$RUNE_HOME/.env` (`TAVILY_API_KEY` or
`BRAVE_API_KEY`), settable through Settings · LLM (extend
`ALLOWED_KEYS`).

### Per-domain allow-list (clinical safety)

Web search for clinical queries needs strict source filtering.
Add a per-medic allow-list to `_t4_search`:

```python
DEFAULT_CLINICAL_DOMAINS = [
    "uptodate.com",      "nccn.org",          "acr.org",
    "esmo.org",          "ahrq.gov",          "nih.gov",
    "ncbi.nlm.nih.gov",  "pubmed.gov",        "radiopaedia.org",
    "radiologyassistant.nl",
    "thieme-connect.com", "rsna.org",
    "cnki.net",          "csco.org.cn",       # Chinese clinical sources
]
```

Settings · LLM gains a "Clinical sources" subtab where the medic can
add / remove domains. Tavily's `include_domains` parameter accepts
this list directly — out-of-list results are filtered at the search
provider, not post-hoc.

### Citations

Existing `[Nxx]` citation chip (where Nxx = node_id) extends to:

- `[Nxx]` — patient graph node (today)
- `[Wxx]` — web source (NEW). On click, ContextRail opens a panel
  with the URL preview, source domain badge ("NCCN.org"), the
  excerpted snippet the LLM grounded on, and an external-link button

Storage: web citations are NOT persisted to `clinical_graph_nodes`
(those represent patient-specific memory). They land as transient
`web_citations` rows scoped to the chat turn, queryable for
ContextRail drill-in but garbage-collected after N days.

### Streaming UX

When T4 fires, the SSE stream emits:

```
tier_classified         (tier="T4_web")
web_search_started      (query="NCCN pulmonary nodule follow-up", provider="tavily")
web_search_results      (results=[{url, title, snippet}, ...])  ← stream as they arrive
reasoning_chunk         ... (the LLM now has both PATIENT_CONTEXT and WEB_CONTEXT)
final_answer_chunk      ...
citations               (refs=[{kind:"node", id:N42}, {kind:"web", w_id:7, url:"...", title:"..."}])
turn_complete
```

UI renders a transient card "🔎 Searching NCCN, RadioPaedia,
UpToDate…" while results land, then the answer streams with
inline `[N42]` and `[W7]` chips.

---

## Part 2 — Parallel Subagents (Claude-style)

### Why subagents

A single Gemini call sees one massive concatenated prompt:
PATIENT CONTEXT + WEB CONTEXT + system prompt + question. Three
problems:

1. **Cross-attention dilution** — patient details and guideline
   text fight for the same attention window. The model often
   picks one and shortchanges the other.
2. **Source-aware reasoning** — "what NCCN says" requires a search
   pass with NCCN-specific filtering; "what this patient's prior
   biopsy showed" is a Layer 1 lookup. Forcing one model to do
   both means doing both poorly.
3. **Latency** — sequential retrieval forces you to wait for the
   slowest branch before LLM synthesis can start.

Claude solves this with **orchestrator + subagent pattern**: the
orchestrator decides which specialists to invoke in parallel,
each subagent has narrow scope + its own context, results
fan back in for synthesis.

### Architecture

```
                  ┌──────────────────────────┐
                  │   Orchestrator (Gemini)  │
                  │   Decomposes the query   │
                  │   into 1-N sub-questions │
                  └──────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼      (parallel asyncio.gather)
       ┌────────────┐  ┌────────────┐  ┌────────────┐
       │ subagent A │  │ subagent B │  │ subagent C │
       │ "patient   │  │ "NCCN      │  │ "imaging   │
       │  history"  │  │  search"   │  │  findings" │
       └────────────┘  └────────────┘  └────────────┘
       Tools: T1+T2     Tools: T4       Tools: dicom_router
              │               │               │
              └───────────────┼───────────────┘
                              ▼
                  ┌──────────────────────────┐
                  │  Synthesizer (Gemini)    │
                  │  Composes final answer   │
                  │  from subagent outputs   │
                  └──────────────────────────┘
                              │
                              ▼
                        SSE stream to UI
```

### When to use subagents vs single LLM

Heuristic:

- **Single LLM (today's T3)** — fast clinical Q with clear scope:
  "what does this patient have?", "summarise the CT findings"
- **Subagents (T5_orchestrator)** — multi-source synthesis:
  "compare this patient's nodule trajectory against NCCN
  short-interval criteria and the radiopaedia size-doubling-time
  framework"

Trigger on query length + intent count:
- Question is ≥2 sentences OR has ≥2 distinct "compare X against Y"
  clauses → orchestrator
- Question mentions ≥2 of {patient, guideline, imaging, literature}
  → orchestrator

### Subagent contract

Each subagent is a typed Python coroutine:

```python
@dataclass
class SubagentInput:
    sub_question: str
    patient_hash: Optional[str]
    user_id: str
    parent_turn_id: str   # for tracing all sub-results back to one turn

@dataclass
class SubagentOutput:
    sub_question: str
    sources: list[Source]      # heterogeneous: NodeRef / WebRef / DicomRef
    summary: str               # 2-3 sentence subagent answer
    cost_tokens: int
    elapsed_ms: int
    error: Optional[str] = None

Subagent = Callable[[SubagentInput], Awaitable[SubagentOutput]]
```

**Phase-1 subagent kinds**:

| Subagent              | Tools / scope                                                 | Use case                            |
| --------------------- | ------------------------------------------------------------- | ----------------------------------- |
| `patient_history`     | T1/T2 patient projection + EventLog query                     | Past findings, meds, comparisons    |
| `web_clinical`        | T4 search restricted to clinical domain allow-list            | Guidelines, papers                  |
| `imaging_lookup`      | DICOM `_render` + `study_summary`                             | "Show me the LUL window from study X" |
| `practitioner_facts`  | Layer 2 active facts                                          | "Account for my preferred workflow" |

Phase-2 additions: `lab_trend`, `medication_check`, `cohort_query`.

### Orchestrator decomposition

The orchestrator LLM gets a system prompt like:

```
You decompose clinical questions into independent sub-questions.

Available subagents (call each as a JSON object):
- patient_history (this patient's records)
- web_clinical (guidelines + papers, allow-listed sources)
- imaging_lookup (this patient's DICOM)
- practitioner_facts (the medic's confirmed patterns)

Rules:
- Each sub-question must be independently answerable
- 1-4 subagents max per turn (cost budget)
- Choose the MINIMUM set that covers the question
- Return STRICT JSON:
  { "subagents": [
      { "kind": "patient_history", "sub_question": "..." },
      { "kind": "web_clinical",    "sub_question": "..." }
    ],
    "synthesis_instructions": "Compare X against Y, citing both"
  }
```

Example:

Input: "should I biopsy J.D.'s 8mm RUL nodule? he's a former smoker
with a prior 6mm nodule from 2023 — what does NCCN say?"

Orchestrator output:
```json
{
  "subagents": [
    { "kind": "patient_history",
      "sub_question": "J.D. nodule history, smoking status, prior biopsies" },
    { "kind": "imaging_lookup",
      "sub_question": "current 8mm RUL nodule features + 2023 6mm nodule" },
    { "kind": "web_clinical",
      "sub_question": "NCCN pulmonary nodule biopsy criteria for former smokers" }
  ],
  "synthesis_instructions":
    "Compare current nodule + trajectory against NCCN criteria; cite both."
}
```

### Synthesizer

After `asyncio.gather` on all subagents, the synthesizer receives:
- the original question
- orchestrator's synthesis_instructions
- each subagent's `{summary, sources}`
- the patient context shared header (Nxx prefixes)

System prompt:

```
You compose a final answer from subagent outputs.
Cite EVERY claim with [Nxx] (patient nodes) or [Wxx] (web sources)
inline. Be honest about disagreements between sources. Never make up
citations.
```

Streaming: synthesizer's tokens stream straight through the SSE
pipeline to the UI, same as today's T3.

### Cost / latency model

- **T3 (single LLM)**: ~3s, 1 Gemini call, ~5k token context
- **T4 (web-grounded)**: ~6s, 1 Gemini call + 1 search call, ~8k context
- **T5 (orchestrator)**:
  - Orchestrator: ~2s, 1 Gemini call (~1k tokens)
  - Subagents in parallel: ~3-5s (slowest branch), 2-4 Gemini calls
  - Synthesizer: ~3s, 1 Gemini call (~6k tokens, results compressed)
  - **Total wall**: ~8-10s (vs 12-15s sequential)
  - **Total tokens**: ~3-4× T3 (paid for in cross-attention quality)

### Tracing + audit

Each orchestrator turn writes:

- `ORCHESTRATOR_PLAN` event (carries the decomposition JSON +
  `parent_turn_id`)
- `SUBAGENT_INVOKED` per branch (`kind`, `sub_question`, `started_at`)
- `SUBAGENT_COMPLETED` per branch (`sources`, `summary`, `elapsed_ms`,
  `cost_tokens`, `error?`)
- `ORCHESTRATOR_SYNTHESIS_STARTED`
- existing `ASSISTANT_RESPONSE` at the end

Reasoning Panel UI (already shows T3 reasoning steps) gets extended
to render the orchestrator plan + subagent timing as a tree:

```
🧠 Orchestrator plan
  ├── patient_history (0.8s) ✓ 4 nodes
  ├── imaging_lookup (1.2s)  ✓ 2 series referenced
  └── web_clinical (4.1s)    ✓ 3 sources (NCCN, RSNA, RadioPaedia)
🪡 Synthesis (2.8s)
```

---

## Phasing

### Phase 1 — Web search MVP (~3 days)

- Add Tavily provider in `nexus_server/web_search.py`
- Extend tier classifier with T4 branch (regex prefilter only,
  no LLM orchestrator yet)
- New SSE events: `web_search_started`, `web_search_results`
- `[Wxx]` citation chip in UI + ContextRail web-source panel
- `TAVILY_API_KEY` in settings_router ALLOWED_KEYS
- Domain allow-list (config + UI in Settings · LLM)
- Tests: provider mock, citation extraction, domain filter

### Phase 2 — Subagent orchestrator (~5 days)

- Orchestrator + 3 starter subagents (`patient_history`,
  `web_clinical`, `imaging_lookup`)
- New tier T5 classifier branch
- `asyncio.gather` execution with per-subagent timeout
- Synthesizer prompt + streaming
- Reasoning Panel orchestrator tree view
- Event-log entries for full audit trail
- Tests: orchestrator JSON parse, subagent timeout, synthesis
  citations resolve, parallel cost model

### Phase 3 — Polish

- `practitioner_facts`, `lab_trend`, `cohort_query` subagents
- Per-medic subagent enable/disable
- Cost dashboard in Settings showing per-tier $$/turn
- Gemini's native grounding-with-search as a swap-in T4 provider

---

## Open discussion items

1. **Search cost** — Tavily is ~$0.005/search at ~5 results. At
   ~10% of turns going T4 and ~20% going T5 (with 1-2 web subagents
   each) that's ~$5/medic/month on search alone. Acceptable? Cap?

2. **Citation persistence** — web citations as transient (gc'd
   after 30 days) vs permanent (joins clinical_graph_nodes with
   node_type='web_source'). Recommend transient — different lifetime
   semantics from patient nodes.

3. **Orchestrator failure modes** — what if the orchestrator
   misclassifies and dispatches the wrong subagents? Mitigations:
   (a) confidence threshold on the orchestrator's plan;
   (b) fallback to T3 if confidence < 0.6;
   (c) "regenerate" button on a bad answer lets the medic re-route
   with a hint.

4. **Sequential vs DAG** — Phase 1 assumes pure-parallel subagents
   (asyncio.gather). Some questions need a DAG: imaging_lookup
   first, THEN web_clinical with imaging features in the query.
   Recommend Phase 1 = parallel only; Phase 2.5 adds DAG with
   explicit `depends_on` declarations.

5. **Streaming subagent thoughts** — should each subagent stream
   its own reasoning to the UI, or do we wait for completion and
   batch? Recommend batch for Phase 1 (simpler), stream in Phase 3
   (much better UX but harder ordering).

6. **Privacy** — when the web subagent searches, the search query
   must NOT leak patient identifiers. The orchestrator's
   `web_clinical` `sub_question` is generated server-side and must
   pass through a PHI scrubber (strip MRN-like tokens, initials,
   dates of birth) before hitting the external API.

7. **Offline / no-key fallback** — if `TAVILY_API_KEY` isn't set,
   T4 silently degrades to T3 with a one-line "(web search
   unavailable — set TAVILY_API_KEY)" tail. Don't fail loud.
