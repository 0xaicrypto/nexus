"""
SkillEvaluator — Offline evaluation pipeline for skill evolution.

Three-step GEPA-inspired pipeline:
  Step 1: LLM-as-Judge — score skill contributions on 5 dimensions
  Step 2: Auto-evolution — rewrite underperforming skills using failure history
  Step 3: Benchmark gating — new version must outperform old before replacement

Design principles:
  - All evaluation is async/non-blocking — never delays user chat
  - Evaluation LLM can be the same model as generation (different prompt)
  - Scores are persisted in skill entries for trend analysis
  - Evolution is conservative — requires N consecutive low scores to trigger
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .memory_evolver import _robust_json_parse

logger = logging.getLogger(__name__)


# ── Evaluation Prompts ─────────────────────────────────────────

EVALUATION_PROMPT = """You are evaluating the quality of an AI assistant's response.

The assistant used a specific skill to help answer the user's question.
Score each dimension from 0 to 10.

User query: {query}

AI response (first 500 chars): {response}

Skill used: {skill_name}
Skill description: {skill_description}
Skill procedure (summary): {skill_procedure_summary}

Score these 5 dimensions:
1. relevance: How well does the response address the user's actual question?
2. completeness: Does the response cover all important aspects?
3. accuracy: Is the information correct and reliable?
4. actionability: Can the user directly act on this response?
5. skill_contribution: Did the skill genuinely improve the response quality?

Return ONLY valid JSON:
{{"relevance": N, "completeness": N, "accuracy": N, "actionability": N, "skill_contribution": N}}

No markdown fences, no explanation."""


EVOLUTION_PROMPT = """You are improving an underperforming AI skill.

Skill name: {skill_name}
Current description: {description}
Current procedure:
{procedure}

Recent evaluation scores (0-10 scale):
{evaluation_history}

Recent failure cases:
{failure_cases}

The skill has scored below {threshold}/10 on average recently.
Rewrite the procedure to address the identified weaknesses.

Rules:
- Keep the same skill name and general purpose
- Fix specific issues revealed by low scores
- Add guardrails for common failure modes
- Procedure should be 3-10 steps in markdown format
- Include a "## Pitfalls" section for known failure modes

Return ONLY valid JSON:
{{
  "description": "Improved one-line description (max 100 chars)",
  "procedure": "## Improved Procedure\\n1. Step one\\n...",
  "changes_made": "Brief summary of what changed and why (1-2 sentences)"
}}

No markdown fences."""


BENCHMARK_PROMPT = """You are comparing two versions of an AI skill.

Test query: {query}

=== Version A (current) ===
Description: {desc_a}
Procedure: {proc_a}

=== Version B (candidate) ===
Description: {desc_b}
Procedure: {proc_b}

For this specific test query, which version would produce a BETTER response?
Score each version on the same 5 dimensions (0-10):

Return ONLY valid JSON:
{{
  "version_a": {{"relevance": N, "completeness": N, "accuracy": N, "actionability": N, "skill_contribution": N}},
  "version_b": {{"relevance": N, "completeness": N, "accuracy": N, "actionability": N, "skill_contribution": N}},
  "winner": "a" or "b",
  "reasoning": "Brief explanation (1 sentence)"
}}

No markdown fences."""


# ── Evaluation Dimensions ──────────────────────────────────────

EVAL_DIMENSIONS = ["relevance", "completeness", "accuracy", "actionability", "skill_contribution"]


class SkillEvaluator:
    """
    Offline evaluation pipeline for skill evolution.

    Wired into EvolutionEngine — runs asynchronously after each
    conversation turn, never blocking user interaction.

    Three capabilities:
      1. evaluate_usage()       — LLM-as-Judge scoring
      2. check_and_evolve()     — auto-trigger skill rewrite
      3. benchmark_evolution()  — A/B gate before applying changes
    """

    # ── Configuration ──────────────────────────────────────────

    # Minimum evaluations before evolution can trigger
    MIN_EVALS_FOR_EVOLUTION = 5

    # Average score threshold (0-10) — below this triggers evolution
    EVOLUTION_THRESHOLD = 5.0

    # Number of benchmark queries to test against
    BENCHMARK_QUERY_COUNT = 3

    # Minimum win rate in benchmark to accept new version
    BENCHMARK_WIN_THRESHOLD = 0.6  # Must win 60%+ of test queries

    # Max evaluation history per skill (ring buffer)
    MAX_EVAL_HISTORY = 20

    def __init__(self, llm_fn: Any):
        self.llm_fn = llm_fn
        # Pending evaluations queue: processed in batch
        self._pending_evals: list[dict] = []

    # ═══════════════════════════════════════════════════════════
    # Step 1: LLM-as-Judge Evaluation
    # ═══════════════════════════════════════════════════════════

    async def evaluate_usage(
        self,
        query: str,
        response: str,
        skill_name: str,
        skill_data: dict,
    ) -> Optional[dict]:
        """Score a skill's contribution to a response.

        Called asynchronously after a conversation turn.
        Returns evaluation scores or None if evaluation fails.
        """
        description = skill_data.get("description", "")
        procedure = skill_data.get("procedure", "")
        # Truncate procedure for the evaluation prompt
        proc_summary = procedure[:300] + "..." if len(procedure) > 300 else procedure

        prompt = EVALUATION_PROMPT.format(
            query=query[:500],
            response=response[:500],
            skill_name=skill_name,
            skill_description=description,
            skill_procedure_summary=proc_summary,
        )

        try:
            raw = await self.llm_fn(prompt)
            scores = _robust_json_parse(raw)
        except Exception as e:
            logger.debug("Skill evaluation failed for %s: %s", skill_name, e)
            return None

        if not isinstance(scores, dict):
            return None

        # Validate and clamp scores
        evaluation = {"timestamp": time.time(), "query_preview": query[:100]}
        for dim in EVAL_DIMENSIONS:
            val = scores.get(dim, 5)
            try:
                val = float(val)
            except (TypeError, ValueError):
                val = 5.0
            evaluation[dim] = max(0.0, min(10.0, val))

        # Compute overall score (weighted average)
        evaluation["overall"] = sum(evaluation[d] for d in EVAL_DIMENSIONS) / len(EVAL_DIMENSIONS)

        return evaluation

    def record_evaluation(self, skill_data: dict, evaluation: dict) -> None:
        """Store an evaluation result in the skill's history.

        Maintains a ring buffer of MAX_EVAL_HISTORY evaluations.
        """
        if "evaluations" not in skill_data:
            skill_data["evaluations"] = []

        skill_data["evaluations"].append(evaluation)

        # Ring buffer — keep only recent evaluations
        if len(skill_data["evaluations"]) > self.MAX_EVAL_HISTORY:
            skill_data["evaluations"] = skill_data["evaluations"][-self.MAX_EVAL_HISTORY:]

    def get_avg_score(self, skill_data: dict, dimension: str = "overall", last_n: int = 0) -> float:
        """Get average score for a dimension over recent evaluations.

        Args:
            skill_data: The skill dict
            dimension: Which dimension to average ("overall" or specific)
            last_n: How many recent evals to consider (0 = all)
        """
        evals = skill_data.get("evaluations", [])
        if not evals:
            return 10.0  # No data = assume good (don't trigger evolution)

        if last_n > 0:
            evals = evals[-last_n:]

        scores = [e.get(dimension, 5.0) for e in evals]
        return sum(scores) / len(scores)

    def needs_evolution(self, skill_data: dict) -> bool:
        """Check if a skill needs evolution based on recent scores.

        Requires MIN_EVALS_FOR_EVOLUTION evaluations before considering.
        """
        evals = skill_data.get("evaluations", [])
        if len(evals) < self.MIN_EVALS_FOR_EVOLUTION:
            return False

        avg = self.get_avg_score(skill_data, "overall", last_n=self.MIN_EVALS_FOR_EVOLUTION)
        return avg < self.EVOLUTION_THRESHOLD

    def get_weak_dimensions(self, skill_data: dict, last_n: int = 5) -> list[str]:
        """Find which dimensions are dragging the score down."""
        weak = []
        for dim in EVAL_DIMENSIONS:
            avg = self.get_avg_score(skill_data, dim, last_n)
            if avg < self.EVOLUTION_THRESHOLD:
                weak.append(dim)
        return weak

    # ═══════════════════════════════════════════════════════════
    # Step 2: Auto-Evolution (GEPA Propose)
    # ═══════════════════════════════════════════════════════════

    async def propose_evolution(
        self,
        skill_name: str,
        skill_data: dict,
    ) -> Optional[dict]:
        """Generate an improved version of an underperforming skill.

        Uses evaluation history and failure cases to guide improvement.
        Returns proposed {description, procedure, changes_made} or None.
        """
        evals = skill_data.get("evaluations", [])
        if not evals:
            return None

        # Format evaluation history for the prompt
        eval_lines = []
        for e in evals[-self.MIN_EVALS_FOR_EVOLUTION:]:
            scores_str = ", ".join(f"{d}={e.get(d, '?')}" for d in EVAL_DIMENSIONS)
            preview = e.get("query_preview", "")
            eval_lines.append(f"  [{scores_str}] query: {preview}")

        # Extract failure cases (low-scoring evaluations)
        failures = [e for e in evals if e.get("overall", 10) < self.EVOLUTION_THRESHOLD]
        failure_lines = []
        for f in failures[-5:]:
            failure_lines.append(
                f"  Query: {f.get('query_preview', '?')} | "
                f"Overall: {f.get('overall', '?'):.1f} | "
                f"Weak: {', '.join(d for d in EVAL_DIMENSIONS if f.get(d, 10) < 5)}"
            )

        prompt = EVOLUTION_PROMPT.format(
            skill_name=skill_name,
            description=skill_data.get("description", ""),
            procedure=skill_data.get("procedure", skill_data.get("best_strategy", "")),
            evaluation_history="\n".join(eval_lines) or "No detailed history",
            failure_cases="\n".join(failure_lines) or "No specific failures recorded",
            threshold=self.EVOLUTION_THRESHOLD,
        )

        try:
            raw = await self.llm_fn(prompt)
            proposal = _robust_json_parse(raw)
        except Exception as e:
            logger.warning("Skill evolution proposal failed for %s: %s", skill_name, e)
            return None

        if not isinstance(proposal, dict) or "procedure" not in proposal:
            return None

        proposal["skill_name"] = skill_name
        proposal["proposed_at"] = time.time()
        proposal["based_on_evals"] = len(evals)

        logger.info(
            "Evolution proposed for [%s]: %s",
            skill_name,
            proposal.get("changes_made", "")[:80],
        )
        return proposal

    # ═══════════════════════════════════════════════════════════
    # Step 3: Benchmark Gating (GEPA Apply with safety gate)
    # ═══════════════════════════════════════════════════════════

    async def benchmark_evolution(
        self,
        skill_name: str,
        current_data: dict,
        proposed: dict,
        test_queries: Optional[list[str]] = None,
    ) -> dict:
        """A/B test the proposed skill evolution against current version.

        Runs both versions through test queries and compares LLM-as-Judge scores.
        Returns {accepted: bool, win_rate: float, details: [...]}.
        """
        # Generate test queries from evaluation history if not provided
        if not test_queries:
            test_queries = self._generate_test_queries(current_data)

        if not test_queries:
            # No test queries available — accept if proposal exists
            logger.warning("No test queries for benchmark of %s — accepting by default", skill_name)
            return {"accepted": True, "win_rate": 1.0, "details": [], "reason": "no_test_queries"}

        current_desc = current_data.get("description", "")
        current_proc = current_data.get("procedure", current_data.get("best_strategy", ""))
        proposed_desc = proposed.get("description", current_desc)
        proposed_proc = proposed.get("procedure", "")

        wins_b = 0
        details = []

        for query in test_queries[:self.BENCHMARK_QUERY_COUNT]:
            prompt = BENCHMARK_PROMPT.format(
                query=query,
                desc_a=current_desc,
                proc_a=current_proc[:500],
                desc_b=proposed_desc,
                proc_b=proposed_proc[:500],
            )

            try:
                raw = await self.llm_fn(prompt)
                result = _robust_json_parse(raw)
            except Exception as e:
                logger.debug("Benchmark query failed: %s", e)
                details.append({"query": query, "error": str(e)})
                continue

            if not isinstance(result, dict):
                continue

            winner = result.get("winner", "a")
            if winner == "b":
                wins_b += 1

            details.append({
                "query": query[:100],
                "winner": winner,
                "reasoning": result.get("reasoning", ""),
                "score_a": sum(
                    result.get("version_a", {}).get(d, 5) for d in EVAL_DIMENSIONS
                ) / len(EVAL_DIMENSIONS),
                "score_b": sum(
                    result.get("version_b", {}).get(d, 5) for d in EVAL_DIMENSIONS
                ) / len(EVAL_DIMENSIONS),
            })

        total_valid = len([d for d in details if "error" not in d])
        win_rate = wins_b / total_valid if total_valid > 0 else 0.0
        accepted = win_rate >= self.BENCHMARK_WIN_THRESHOLD

        logger.info(
            "Benchmark for [%s]: %s (win_rate=%.0f%%, threshold=%.0f%%)",
            skill_name,
            "ACCEPTED" if accepted else "REJECTED",
            win_rate * 100,
            self.BENCHMARK_WIN_THRESHOLD * 100,
        )

        return {
            "accepted": accepted,
            "win_rate": win_rate,
            "total_queries": total_valid,
            "wins_new": wins_b,
            "details": details,
        }

    def _generate_test_queries(self, skill_data: dict) -> list[str]:
        """Extract test queries from evaluation history.

        Uses query_preview from past evaluations as benchmark inputs.
        Prioritizes diverse queries (dedup by first 50 chars).
        """
        evals = skill_data.get("evaluations", [])
        seen = set()
        queries = []
        for e in reversed(evals):  # Most recent first
            preview = e.get("query_preview", "")
            if not preview:
                continue
            key = preview[:50]
            if key not in seen:
                seen.add(key)
                queries.append(preview)
            if len(queries) >= self.BENCHMARK_QUERY_COUNT * 2:
                break
        return queries

    # ═══════════════════════════════════════════════════════════
    # Full Pipeline: Evaluate → Evolve → Benchmark → Apply
    # ═══════════════════════════════════════════════════════════

    async def run_evolution_pipeline(
        self,
        skill_name: str,
        skill_data: dict,
    ) -> Optional[dict]:
        """Run the full GEPA pipeline for a single skill.

        Only runs if the skill needs evolution (low recent scores).
        Returns the applied evolution result, or None if not needed/rejected.

        Caller (EvolutionEngine) is responsible for:
          1. Calling this method periodically
          2. Applying the returned changes to the skill cache
          3. Saving the updated skills
        """
        if not self.needs_evolution(skill_data):
            return None

        weak_dims = self.get_weak_dimensions(skill_data)
        logger.info(
            "Skill [%s] needs evolution (avg=%.1f, weak=%s)",
            skill_name,
            self.get_avg_score(skill_data, "overall", last_n=self.MIN_EVALS_FOR_EVOLUTION),
            weak_dims,
        )

        # Step 2: Propose evolution
        proposed = await self.propose_evolution(skill_name, skill_data)
        if not proposed:
            logger.warning("Evolution proposal failed for [%s]", skill_name)
            return None

        # Step 3: Benchmark gate
        benchmark = await self.benchmark_evolution(skill_name, skill_data, proposed)

        if not benchmark["accepted"]:
            logger.info(
                "Evolution REJECTED for [%s] (win_rate=%.0f%%)",
                skill_name, benchmark["win_rate"] * 100,
            )
            # Record the rejection in evaluation history
            return {
                "action": "rejected",
                "skill_name": skill_name,
                "win_rate": benchmark["win_rate"],
                "proposed": proposed,
                "benchmark": benchmark,
            }

        # ── Apply the evolution ──
        logger.info(
            "Evolution ACCEPTED for [%s] (win_rate=%.0f%%): %s",
            skill_name,
            benchmark["win_rate"] * 100,
            proposed.get("changes_made", "")[:80],
        )

        return {
            "action": "accepted",
            "skill_name": skill_name,
            "win_rate": benchmark["win_rate"],
            "proposed": proposed,
            "benchmark": benchmark,
            "old_version": skill_data.get("version", 1),
            "changes_made": proposed.get("changes_made", ""),
        }

    def apply_evolution(self, skill_data: dict, evolution_result: dict) -> None:
        """Apply an accepted evolution to a skill entry.

        Called by SkillEvolver after run_evolution_pipeline returns accepted.
        """
        proposed = evolution_result.get("proposed", {})

        # Bump version
        skill_data["version"] = skill_data.get("version", 1) + 1

        # Update description and procedure
        new_desc = proposed.get("description", "")
        if new_desc:
            skill_data["description"] = new_desc[:200]

        new_proc = proposed.get("procedure", "")
        if new_proc:
            # Archive old procedure in lessons
            old_proc = skill_data.get("procedure", "")
            if old_proc:
                skill_data.setdefault("lessons", []).append({
                    "lesson": f"Evolved from v{skill_data['version']-1}: {proposed.get('changes_made', '')}",
                    "outcome": "evolution",
                    "source": "evaluator",
                    "timestamp": time.time(),
                    "old_procedure_preview": old_proc[:200],
                })
                # Keep lessons bounded
                skill_data["lessons"] = skill_data["lessons"][-10:]

            skill_data["procedure"] = new_proc

        # Record evolution metadata
        skill_data["last_evolved"] = time.time()
        skill_data["evolution_count"] = skill_data.get("evolution_count", 0) + 1

        # Clear evaluation history to give the new version a fresh start
        skill_data["evaluations"] = []

        logger.info(
            "Applied evolution to [%s] → v%d",
            skill_data.get("name", "?"),
            skill_data["version"],
        )
