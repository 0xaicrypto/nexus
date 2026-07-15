"""
PersonaEvolver — Self-optimize the twin's system prompt and behavior.

Phase D
-------
Single source of truth: the typed ``PersonaStore``. All chat-time
reads go through it; rollback works by flipping the store's
pointer (the projection reads ``persona_store.current()`` next
turn and sees the rolled-back text). No legacy artifact write,
no in-memory cache, no ``apply_rollback`` — the absence of those
paths is what guarantees the typed store and the agent's actual
behaviour can never diverge.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from nexus_core import AgentRuntime
from nexus_core.evolution import EvolutionProposal
from nexus_core.memory import EventLog, PersonaStore, PersonaVersion

logger = logging.getLogger(__name__)

REFLECTION_PROMPT = """You are a digital twin's self-reflection engine.

Analyze the twin's current state and produce an evolved persona.

Current persona:
{current_persona}

Accumulated memories (sample):
{memories_sample}

Learned skills:
{skills_summary}

Recent conversation patterns:
{recent_patterns}

Evolution history (last 3 changes):
{evolution_history}

Instructions:
1. Identify what the twin has learned about its owner
2. Determine how the twin's communication style should adapt
3. Note any new capabilities or knowledge areas
4. Write an EVOLVED persona that incorporates these learnings

Rules:
- Keep the core identity stable — evolution should be incremental
- Make the persona more specific and personalized over time
- Add specific knowledge and preferences learned
- Adjust tone/style based on conversation patterns
- Never remove safety guardrails

Return a JSON object:
{{
  "evolved_persona": "The full updated persona text...",
  "changes_summary": "Brief description of what changed and why",
  "confidence": 0.0-1.0,
  "version_notes": "Short label for this evolution step"
}}

Return ONLY valid JSON, no markdown fences."""


class PersonaEvolver:
    """Evolves the twin's persona through self-reflection.

    Phase D: ``persona_store`` is required — there is no legacy
    fallback.
    """

    def __init__(
        self,
        rune: AgentRuntime,
        agent_id: str,
        llm_fn: Any,
        event_log: EventLog | None = None,
        persona_store: PersonaStore | None = None,
    ):
        if persona_store is None:
            # Phase D: typed store is the only path. When the caller
            # doesn't pass one (tests / standalone use), synthesise
            # a scratch store under tempdir. DigitalTwin always
            # wires the real, chain-mirrored one in production.
            import tempfile
            from pathlib import Path
            scratch = Path(tempfile.gettempdir()) / f"nexus-persona-scratch-{agent_id}"
            scratch.mkdir(parents=True, exist_ok=True)
            persona_store = PersonaStore(base_dir=scratch)
        self.rune = rune
        self.agent_id = agent_id
        self.llm_fn = llm_fn
        self.event_log = event_log
        self.persona_store = persona_store
        self._default_persona: str = ""
        self._evolution_history: list[dict] = []

    async def load_persona(self, default_persona: str) -> str:
        """Return the active persona text. Reads through the typed
        ``PersonaStore`` so a rollback is reflected immediately.

        ``default_persona`` is used only when the store has never
        been written to (fresh agent / fresh chain). It is *also*
        cached so subsequent calls return the same baseline if the
        store remains empty — matches the old behaviour.
        """
        self._default_persona = default_persona
        current = self.persona_store.current()
        if current is None or not (current.persona_text or "").strip():
            return default_persona
        return current.persona_text

    async def evolve(
        self,
        memories_sample: list[str],
        skills_summary: dict,
        recent_patterns: str = "",
    ) -> dict:
        history_text = ""
        for h in self._evolution_history[-3:]:
            history_text += f"- {h.get('typed_version', '?')}: {h.get('changes', 'N/A')}\n"
        if not history_text:
            history_text = "No previous evolutions."

        # Read current text from the typed store; fall back to
        # whatever default was last passed to load_persona().
        current_obj = self.persona_store.current()
        current_text = (current_obj.persona_text if current_obj else "") or self._default_persona
        prev_typed_version = self.persona_store.current_version() or "(none)"

        prompt = REFLECTION_PROMPT.format(
            current_persona=current_text,
            memories_sample=json.dumps(memories_sample[:15], ensure_ascii=False),
            skills_summary=json.dumps(skills_summary, ensure_ascii=False),
            recent_patterns=recent_patterns or "Not enough data yet.",
            evolution_history=history_text,
        )

        try:
            raw = await self.llm_fn(prompt)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(raw)
        except Exception as e:
            logger.warning(f"Persona evolution failed: {e}")
            return {"error": str(e)}

        evolved = result.get("evolved_persona", "")
        if not evolved or len(evolved) < 50:
            return {"skipped": True, "reason": "Output too short"}

        confidence = result.get("confidence", 0.5)
        if confidence < 0.3:
            return {"skipped": True, "reason": f"Low confidence: {confidence}"}

        # Phase O.2: emit evolution_proposal BEFORE the write. This is
        # PersonaEvolver — the AHE paper's highest-risk evolver — so a
        # complete pre-write declaration is non-negotiable for verdict-
        # driven rollback to be useful.
        edit_id = self._emit_proposal(
            prev_persona=current_text,
            evolved_persona=evolved,
            change_summary=result.get("changes_summary", ""),
            confidence=confidence,
            prev_typed_version=prev_typed_version,
        )

        # Single source of truth: the typed PersonaStore. ``propose_version``
        # writes a new immutable version and advances the active pointer;
        # the next call to ``load_persona`` will return ``evolved``.
        try:
            typed_version = self.persona_store.propose_version(
                PersonaVersion(
                    persona_text=evolved,
                    changes_summary=result.get("changes_summary", "")[:500],
                    confidence=max(0.0, min(1.0, float(confidence))),
                    version_notes=result.get("version_notes", "")[:200],
                    extra={
                        "evolution_edit_id": edit_id,
                    },
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("PersonaStore.propose_version failed: %s", e)
            return {"error": f"persona_store write failed: {e}"}

        self._evolution_history.append({
            "typed_version": typed_version,
            "changes": result.get("changes_summary", ""),
            "notes": result.get("version_notes", ""),
            "confidence": confidence,
            "timestamp": time.time(),
            "evolution_edit_id": edit_id,
        })

        logger.info(f"Persona evolved to {typed_version}")
        return {
            "typed_version": typed_version,
            "changes": result.get("changes_summary", ""),
            "notes": result.get("version_notes", ""),
            "confidence": confidence,
            "evolution_edit_id": edit_id,
        }

    # ── Phase O.2: emit evolution_proposal events ─────────────

    def _emit_proposal(
        self,
        *,
        prev_persona: str,
        evolved_persona: str,
        change_summary: str,
        confidence: float,
        prev_typed_version: str,
    ) -> str:
        """Emit an ``evolution_proposal`` event for the upcoming
        persona swap. Returns the new edit_id; ``""`` when the
        instrumentation isn't wired in.
        """
        if self.event_log is None:
            return ""

        edit_id = str(uuid.uuid4())
        proposal = EvolutionProposal(
            edit_id=edit_id,
            evolver="PersonaEvolver",
            target_namespace="memory.persona",
            target_version_pre=prev_typed_version,
            target_version_post=prev_typed_version,  # post-version unknown until commit
            change_summary=change_summary or "persona evolution",
            change_diff=[
                {
                    "op": "replace",
                    "field": "persona_text",
                    "prev_len": len(prev_persona),
                    "post_len": len(evolved_persona),
                },
            ],
            evidence_summary=f"reflection at {prev_typed_version}, confidence={confidence:.2f}",
            rollback_pointer=prev_typed_version,
            predicted_fixes=[],
            predicted_regressions=[],
            # Phase C: PersonaEvolver sits at the apex (L0) — every
            # downstream layer (facts/skills/knowledge) feeds into
            # it. lineage card uses these counts to render the AHE
            # pyramid: "caused by N facts + M skills + K articles".
            triggered_by={
                "trigger_reason": "reflection_cycle",
                "from_version": prev_typed_version,
                "confidence": confidence,
                "delta_chars": len(evolved_persona) - len(prev_persona),
            },
        )
        try:
            self.event_log.append(
                event_type="evolution_proposal",
                content=(
                    f"PersonaEvolver → memory.persona: "
                    f"{change_summary or 'reflection'} "
                    f"(confidence={confidence:.2f})"
                ),
                metadata=proposal.to_event_metadata(),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("emit evolution_proposal (persona) failed: %s", e)
            return ""
        return edit_id

    @property
    def current_persona(self) -> str:
        """Read-through to the typed store. Used by tests + UI."""
        current = self.persona_store.current()
        if current is None:
            return self._default_persona
        return current.persona_text or self._default_persona

    async def get_evolution_history(self) -> list[dict]:
        return list(self._evolution_history)

    async def get_stats(self) -> dict:
        return {
            "persona_version": self.persona_store.current_version(),
            "total_evolutions": len(self._evolution_history),
            "persona_length": len(self.current_persona),
        }

    # ── Phase C: Evolution Pressure dashboard ─────────────────────

    def pressure_state(
        self,
        cadence_days: float = 30.0,
        drift_threshold: float = 0.7,
        drift_score: float = 0.0,
    ) -> dict:
        """Per-evolver state for the Pressure Dashboard.

        Persona is the slowest-changing layer (BEP-Nexus AHE: highest
        risk per change). Two independent triggers — whichever wins
        decides the gauge:
          * **Time** — days_since_last_evolve / cadence_days
          * **Drift** — observed contract drift / threshold

        The reported ``accumulator`` is the MAX of the two ratios
        (clamped to [0, 1]) so a gauge near 100% means "next chat
        likely to trigger" regardless of whether time or drift was
        the dominant signal.
        """
        last_h = self._evolution_history[-1] if self._evolution_history else None
        last_ts = float(last_h.get("timestamp", 0.0)) if last_h else 0.0
        days_since = (time.time() - last_ts) / 86400.0 if last_ts else cadence_days
        time_ratio = min(1.0, days_since / max(0.1, cadence_days))
        drift_ratio = min(1.0, max(0.0, drift_score) / max(0.01, drift_threshold))
        accumulator = max(time_ratio, drift_ratio)
        if accumulator >= 1.0:
            status = "ready"
        elif accumulator >= 0.9:
            status = "warming"
        else:
            status = "warming"
        return {
            "evolver": "PersonaEvolver",
            "layer": "L0",
            "accumulator": accumulator,
            "threshold": 1.0,
            "unit": "ratio",
            "status": status,
            "fed_by": ["MemoryEvolver", "KnowledgeCompiler", "SkillEvolver"],
            "last_fired_at": last_ts or None,
            "details": {
                "persona_version": self.persona_store.current_version(),
                "total_evolutions": len(self._evolution_history),
                "days_since_last": days_since,
                "cadence_days": cadence_days,
                "drift_score": drift_score,
                "drift_threshold": drift_threshold,
                "dominant_signal": "drift" if drift_ratio > time_ratio else "time",
            },
        }
