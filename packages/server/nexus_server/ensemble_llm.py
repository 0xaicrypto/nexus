"""#170 — Multi-model ensemble for high-stakes clinical decisions.

When a workflow step / skill is marked ``critical=true``, instead of
asking a single Gemini call, we ask Gemini + Claude + GPT in
parallel and either:

  • return the consensus when N-1 of N models agree (high
    confidence — no second opinion needed), OR
  • escalate to the medic when models disagree beyond a threshold
    (low confidence — show the disagreement transparently rather
    than pick one model's answer arbitrarily).

This is the smallest meaningful multi-model swarm: it doesn't try
to do agent-to-agent debate (#170 Phase 2) — just diversity-of-
samples to catch single-model hallucinations in cancer / RT /
diagnosis decisions where one wrong number can lead to wrong
treatment.

API keys via env:
  ANTHROPIC_API_KEY for Claude
  OPENAI_API_KEY    for GPT
  GEMINI_API_KEY    for Gemini (already present)

When a provider's key is missing, that provider is skipped silently
(but logged) — ensemble degrades gracefully to fewer voters.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── Provider abstractions ──────────────────────────────────────────


@dataclass
class ProviderResult:
    """One model's response in an ensemble call."""
    provider: str      # "gemini" / "claude" / "openai"
    model:    str      # specific model name
    text:     str
    error:    str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.text.strip())


async def _call_gemini(
    system_prompt: str, user_prompt: str, *, model: str = "gemini-2.5-flash",
) -> ProviderResult:
    """Reuse the existing nexus_core Gemini path — never duplicates
    the gateway logic, just adds a thin wrapper for ensemble use."""
    try:
        from nexus_core.llm.client import LLMClient
        client = LLMClient(provider="gemini", model=model)
        text = await client.chat(
            system_prompt=system_prompt,
            user_message=user_prompt,
        )
        return ProviderResult(provider="gemini", model=model, text=text or "")
    except Exception as e:  # noqa: BLE001
        logger.warning("ensemble gemini call failed: %s", e)
        return ProviderResult(
            provider="gemini", model=model, text="",
            error=f"{type(e).__name__}: {e}",
        )


async def _call_claude(
    system_prompt: str, user_prompt: str, *,
    model: str = "claude-sonnet-4-5-20250929",
) -> ProviderResult:
    """Anthropic Claude via the official anthropic-python SDK.
    Imports lazily so the dep stays optional when ensemble isn't used.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return ProviderResult(
            provider="claude", model=model, text="",
            error="ANTHROPIC_API_KEY not set",
        )
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        resp = await client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        # Concat all text content blocks (Claude can stream multi-block).
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        )
        return ProviderResult(provider="claude", model=model, text=text)
    except Exception as e:  # noqa: BLE001
        logger.warning("ensemble claude call failed: %s", e)
        return ProviderResult(
            provider="claude", model=model, text="",
            error=f"{type(e).__name__}: {e}",
        )


async def _call_openai(
    system_prompt: str, user_prompt: str, *, model: str = "gpt-4o-mini",
) -> ProviderResult:
    """OpenAI GPT via the official openai-python SDK."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return ProviderResult(
            provider="openai", model=model, text="",
            error="OPENAI_API_KEY not set",
        )
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key)
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=4096,
        )
        text = resp.choices[0].message.content or ""
        return ProviderResult(provider="openai", model=model, text=text)
    except Exception as e:  # noqa: BLE001
        logger.warning("ensemble openai call failed: %s", e)
        return ProviderResult(
            provider="openai", model=model, text="",
            error=f"{type(e).__name__}: {e}",
        )


# ── Ensemble orchestration ─────────────────────────────────────────


@dataclass
class EnsembleResult:
    """Output of one ensemble call. The caller decides whether to
    return ``consensus_text`` to the user, surface the disagreement,
    or escalate."""
    providers:    list[ProviderResult]
    consensus:    bool         # True when N-1 of N agreed
    consensus_text: str        # populated only when ``consensus`` is True
    disagreement_summary: str  # one-line summary when ``consensus`` is False

    @property
    def successful(self) -> list[ProviderResult]:
        return [r for r in self.providers if r.ok]


async def ensemble_call(
    system_prompt: str, user_prompt: str, *,
    require_consensus: bool = True,
) -> EnsembleResult:
    """Fan out to Gemini + Claude + OpenAI concurrently and
    summarise the (dis)agreement.

    Consensus heuristic v1: when ``require_consensus``, declare
    consensus only when at least 2 of the 3 successful responses
    are highly textually similar (Jaccard ≥ 0.4 over shingles
    works well for clinical bullet-point answers). When fewer than
    2 successful responses come back, the caller falls back to
    "best available" and flags it as low-confidence.

    Future: replace Jaccard with semantic similarity via the
    embed_text helper already used in the vector index. For now
    Jaccard is enough to catch "all 3 said the same number" vs
    "models gave 3 different numbers".
    """
    gemini_task = asyncio.create_task(_call_gemini(system_prompt, user_prompt))
    claude_task = asyncio.create_task(_call_claude(system_prompt, user_prompt))
    openai_task = asyncio.create_task(_call_openai(system_prompt, user_prompt))
    results = await asyncio.gather(
        gemini_task, claude_task, openai_task, return_exceptions=False,
    )
    successful = [r for r in results if r.ok]

    if not successful:
        return EnsembleResult(
            providers=results,
            consensus=False,
            consensus_text="",
            disagreement_summary=(
                "All three model providers failed; cannot produce a "
                "consensus answer. Check API keys + network."
            ),
        )

    if not require_consensus:
        return EnsembleResult(
            providers=results,
            consensus=True,
            consensus_text=successful[0].text,
            disagreement_summary="",
        )

    if len(successful) == 1:
        return EnsembleResult(
            providers=results,
            consensus=False,
            consensus_text="",
            disagreement_summary=(
                f"Only one provider responded ({successful[0].provider}); "
                "cannot establish consensus. Treat as low-confidence."
            ),
        )

    # Pairwise Jaccard over word-trigrams to spot near-duplicates.
    def shingles(text: str) -> set[str]:
        words = text.lower().split()
        return {
            " ".join(words[i:i + 3])
            for i in range(max(0, len(words) - 2))
        }

    by_provider = {r.provider: shingles(r.text) for r in successful}
    providers = list(by_provider.keys())
    pair_scores: list[tuple[str, str, float]] = []
    for i in range(len(providers)):
        for j in range(i + 1, len(providers)):
            a, b = by_provider[providers[i]], by_provider[providers[j]]
            if not a or not b:
                score = 0.0
            else:
                score = len(a & b) / max(1, len(a | b))
            pair_scores.append((providers[i], providers[j], score))

    high_pairs = [p for p in pair_scores if p[2] >= 0.4]
    if high_pairs:
        # Consensus — return the response that participates in the
        # most high-agreement pairs (i.e. the "median" model).
        votes: dict[str, int] = {}
        for a, b, _ in high_pairs:
            votes[a] = votes.get(a, 0) + 1
            votes[b] = votes.get(b, 0) + 1
        winner = max(votes, key=votes.get)
        winning_text = next(r.text for r in successful if r.provider == winner)
        return EnsembleResult(
            providers=results,
            consensus=True,
            consensus_text=winning_text,
            disagreement_summary="",
        )

    summary = (
        f"Ensemble disagreement: {len(successful)} providers "
        "produced substantively different answers (Jaccard < 0.4 "
        "between all pairs). Escalating to medic for review."
    )
    return EnsembleResult(
        providers=results,
        consensus=False,
        consensus_text="",
        disagreement_summary=summary,
    )
