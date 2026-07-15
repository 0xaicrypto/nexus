"""evolve_skill tool — agent-callable wrapper around skill_evolution.

When the user says "the Content Studio writer keeps producing fluff
hooks — here are 3 examples of better hooks, can you make it better?",
the agent calls this tool with the examples. The tool:

  1. Loads the named skill from disk.
  2. Calls the validation-gated evolver.
  3. If a strictly-improving edit was found, persists the new
     SKILL.md AND a diff log.
  4. Returns a summary the agent can present to the user.

By design this is a long-running tool (one evolve cycle does
N rollouts × M judge calls). The agent should tell the user
"this will take a minute or two" before calling.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from nexus_core.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


def _user_skills_dir() -> Path:
    """Match the SkillManager + starter_packs convention — cwd
    relative ``.nexus/skills``. The desktop server runs with cwd =
    $RUNE_HOME so this resolves to $RUNE_HOME/.nexus/skills."""
    return Path.cwd() / ".nexus" / "skills"


def _resolve_skill_path(skill_name: str) -> tuple[Optional[Path], Optional[Path], str]:
    """Return (skill_file_path, skill_dir_path, layout_tag) or
    (None, None, "") if not installed. layout_tag is "folder" or
    "flat" so we know which file to rewrite on accept."""
    base = _user_skills_dir()
    folder = base / skill_name
    if folder.is_dir() and (folder / "SKILL.md").exists():
        return folder / "SKILL.md", folder, "folder"
    flat = base / f"{skill_name}.md"
    if flat.exists():
        # Flat skills get an evolution sidecar directory created on
        # first evolve so .rejected.jsonl + .history have somewhere
        # to live without polluting the .md.
        side = base / f"{skill_name}.evolution"
        return flat, side, "flat"
    return None, None, ""


class EvolveSkillTool(BaseTool):
    """Improve an installed skill's SKILL.md against held-out examples."""

    @property
    def name(self) -> str:
        return "evolve_skill"

    @property
    def description(self) -> str:
        return (
            "Improve an installed skill by running it against held-out "
            "examples + scoring with a judge LLM + proposing bounded "
            "edits + accepting ONLY those edits that strictly beat the "
            "current score on the held-out set.\n"
            "\n"
            "Inspired by SkillOpt (Microsoft, 2026). The strict-improve "
            "gate means evolving a skill CAN'T MAKE IT WORSE — at worst, "
            "it stays unchanged.\n"
            "\n"
            "Use this when:\n"
            "  - User points out a recurring failure mode in a workflow's "
            "output and provides examples of better behaviour\n"
            "  - User says 'this skill keeps doing X wrong'\n"
            "  - You want to harden a verifier or scanner after seeing it "
            "miss specific cases\n"
            "\n"
            "Cost: 1 evolve run = ~(N_examples × 2) LLM calls (rollout + "
            "judge) per round, ≤ 3 rounds. Takes 30-120 seconds. Warn the "
            "user before calling.\n"
            "\n"
            "Durable rules (wrapped in <!-- nexus:durable --> in the SKILL.md) "
            "are NEVER modified — authors protect critical invariants "
            "this way. Failed proposals go into .rejected.jsonl so the "
            "optimizer doesn't repropose dead ends."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": (
                        "Installed skill name, kebab-case (e.g. "
                        "'content-writer', 'sca-vulnerability-scanner'). "
                        "Must match an existing .nexus/skills/<name>/ "
                        "or .nexus/skills/<name>.md."
                    ),
                },
                "examples": {
                    "type": "array",
                    "description": (
                        "Held-out examples to score against. Each item: "
                        "{input: str (what to feed the skill), rubric: "
                        "str (what 'good' looks like), "
                        "expected_output_summary: str (optional reference)}. "
                        "Recommend 3-8 examples; more = better signal but "
                        "longer evolve run."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "input":   {"type": "string"},
                            "rubric":  {"type": "string"},
                            "expected_output_summary": {"type": "string"},
                        },
                        "required": ["input"],
                    },
                },
                "max_iterations": {
                    "type": "integer",
                    "description": (
                        "How many propose→score→gate cycles to run "
                        "(default 3). Each cycle that accepts an edit "
                        "monotonically improves the score; loop stops "
                        "early if no proposal beats current."
                    ),
                },
            },
            "required": ["skill_name", "examples"],
        }

    async def execute(
        self,
        skill_name: str = "",
        examples: Optional[list] = None,
        max_iterations: int = 3,
        **kwargs,
    ) -> ToolResult:
        if not skill_name.strip():
            return ToolResult(success=False, error="`skill_name` is required.")
        if not examples or not isinstance(examples, list):
            return ToolResult(
                success=False,
                error="`examples` must be a non-empty list of "
                      "{input, rubric, ...}.",
            )

        skill_file, skill_dir, layout = _resolve_skill_path(skill_name)
        if skill_file is None:
            return ToolResult(
                success=False,
                error=(
                    f"Skill {skill_name!r} not installed. Check "
                    f".nexus/skills/{skill_name}/SKILL.md or "
                    f".nexus/skills/{skill_name}.md exists."
                ),
            )

        # Load the skill body. For folder layout, take the SKILL.md
        # text as the body; for flat layout, take the whole file.
        raw_text = skill_file.read_text(encoding="utf-8")
        from nexus_core.skills.manager import _parse_frontmatter
        frontmatter, body = _parse_frontmatter(raw_text)

        # Resolve per-skill models from frontmatter.
        from nexus_core.skills.manager import (
            _extract_model,
            _extract_optimizer_model,
        )
        target_model    = _extract_model(frontmatter)
        optimizer_model = _extract_optimizer_model(frontmatter) or target_model
        # Judge model defaults to the optimizer (strong evaluator).
        judge_model     = optimizer_model

        # Build TaskExample list.
        from nexus_server.skill_evolution import (
            TaskExample,
            evolve_skill_loop,
        )
        ex_list = []
        for raw_ex in examples:
            if not isinstance(raw_ex, dict):
                continue
            inp = str(raw_ex.get("input", "")).strip()
            if not inp:
                continue
            ex_list.append(TaskExample(
                input=inp,
                rubric=str(raw_ex.get("rubric", "")).strip(),
                expected_output_summary=str(
                    raw_ex.get("expected_output_summary", "")
                ).strip(),
            ))
        if not ex_list:
            return ToolResult(
                success=False,
                error="No valid examples after parsing (each needs at "
                      "minimum an `input` string).",
            )

        try:
            max_iterations = max(1, min(int(max_iterations or 3), 5))
        except (TypeError, ValueError):
            max_iterations = 3

        logger.info(
            "evolve_skill: %s, %d examples, target=%s, optimizer=%s",
            skill_name, len(ex_list), target_model, optimizer_model,
        )

        try:
            summary = await evolve_skill_loop(
                skill_body=body, skill_dir=skill_dir,
                examples=ex_list,
                target_model=target_model,
                optimizer_model=optimizer_model,
                judge_model=judge_model,
                max_iterations=max_iterations,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("evolve_skill_loop crashed for %s", skill_name)
            return ToolResult(
                success=False,
                error=f"Evolution loop crashed: {e}. SKILL.md unchanged.",
            )

        delta = summary["after_score"] - summary["before_score"]

        # If we got improvement, persist + record a history line.
        if summary["accepted_edits"]:
            new_full = _reassemble_skill_md(
                frontmatter_text=raw_text.split("---", 2)[1] if raw_text.startswith("---") else "",
                new_body=summary["final_body"],
            )
            # Backup + write.
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            if skill_dir is not None:
                skill_dir.mkdir(parents=True, exist_ok=True)
                hist = skill_dir / ".history"
                hist.mkdir(exist_ok=True)
                (hist / f"SKILL.{ts}.md").write_text(raw_text, encoding="utf-8")
                # Append an evolution log.
                with (skill_dir / "evolution.log").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "ts":             ts,
                        "before_score":   summary["before_score"],
                        "after_score":    summary["after_score"],
                        "delta":          delta,
                        "accepted_edits": summary["accepted_edits"],
                    }) + "\n")
            skill_file.write_text(new_full, encoding="utf-8")

            return ToolResult(output=(
                f"✓ Evolved '{skill_name}' over {summary['iterations']} "
                f"accepted edit(s). Held-out score: "
                f"{summary['before_score']:.1f} → {summary['after_score']:.1f} "
                f"(+{delta:.1f}). Rejected proposals saved to "
                f".rejected.jsonl. Edits applied:\n"
                + "\n".join(
                    f"  {e['iteration']}. [{e['kind']}] {e['rationale']} "
                    f"(+{e['delta']:.1f})"
                    for e in summary["accepted_edits"]
                )
            ))

        # No accepted edits — return the rejection reasons so the
        # user knows the skill is already at a local optimum given
        # these examples.
        reasons = summary.get("rejected_reasons", [])
        reasons_text = (
            "Rejection reasons:\n" + "\n".join(f"  - {r}" for r in reasons[:5])
            if reasons else "Optimizer proposed no edits worth trying."
        )
        return ToolResult(output=(
            f"No improving edit found for '{skill_name}'. Held-out "
            f"score stays at {summary['before_score']:.1f}/100. "
            f"SKILL.md UNCHANGED — the strict-improve gate refused "
            f"every proposal. {reasons_text}\n\n"
            "Suggestions: try different examples (more diverse / harder), "
            "raise the rubric bar, or accept that the skill is already "
            "well-tuned for this case."
        ))


def _reassemble_skill_md(*, frontmatter_text: str, new_body: str) -> str:
    """Rebuild a complete SKILL.md from its frontmatter chunk + new body.
    ``frontmatter_text`` is the raw YAML between the ``---`` fences
    (no fences themselves). If absent, the rebuilt file has no
    frontmatter."""
    if not frontmatter_text.strip():
        return new_body.rstrip() + "\n"
    return (
        "---\n"
        + frontmatter_text.strip("\n")
        + "\n---\n\n"
        + new_body.lstrip("\n").rstrip()
        + "\n"
    )


def register_evolve_tools(twin, user_id: str) -> None:
    """Register the evolve_skill tool onto the given twin."""
    twin.register_tool(EvolveSkillTool())
    logger.info("evolve_skill tool registered for user %s", user_id)
