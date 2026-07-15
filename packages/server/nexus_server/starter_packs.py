"""Bundled starter pack registry.

A "starter pack" is a workflow + the skills it references, shipped
together so a user can one-click install instead of curating skills
+ workflow rows by hand.

Layout on disk:

    nexus_server/starter_packs/
        content-studio/
            workflow.json
            skills/
                content-strategist.md
                content-researcher.md
                ...
        research-brief/
            ...

The packs themselves are versioned with the server bundle. Phase 3
(marketplace) replaces this with a remote registry + signed packs;
Phase 2 v1 keeps it server-local for the simplest possible UX:
ship the bundle, click Install, go.

Public API
==========
* :func:`list_packs` — returns a list of pack metadata (id, name,
  description, step count, tier hint).
* :func:`install_pack` — for a given (user_id, pack_id), copies the
  pack's skill files into the user's skills dir AND creates the
  workflow row in the database. Idempotent: re-installing replaces
  the workflow definition and re-writes the skill files.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from nexus_server import workflows

logger = logging.getLogger(__name__)


# Location of the bundled pack assets on disk. Resolved relative to
# THIS file so the bundle layout doesn't depend on the server's cwd
# (which is $RUNE_HOME at runtime, not the package root).
PACKS_ROOT = Path(__file__).parent / "starter_packs"


@dataclass
class StarterPack:
    """One installable pack's metadata. Wire shape exposed by the
    GET /packs endpoint."""
    id: str                # folder name, also the canonical handle
    name: str              # display name
    description: str
    step_count: int
    audience: str          # e.g. "solo creators", "engineers"
    tier: str              # "free" | "pro" | "pro_plus" | "radiology_pro"
    available: bool = True # False = visible but install endpoint refuses
    coming_soon_note: str = ""


# Hand-curated catalog. Order matters — this is the order shown in
# the empty state. Only the FIRST one (Content Studio) is fully
# bundled in Phase 2 v1; the others have entries here so the UI can
# show them with "Coming soon" badges and the discovery story still
# works.
PACK_CATALOG: list[StarterPack] = [
    StarterPack(
        id="content-studio",
        name="Content Studio",
        description=(
            "Strategist → Researcher → Writer → Editor → Publisher. "
            "End-to-end content production from topic to publish-ready."
        ),
        step_count=5,
        audience="solo creators",
        tier="free",
    ),
    StarterPack(
        id="research-brief",
        name="Research Brief",
        description=(
            "Scoper → Researcher → Fact-checker → Synthesizer → Publisher. "
            "Decision-grade briefs with tiered citations and explicit caveats."
        ),
        step_count=5,
        audience="analysts",
        tier="free",
    ),
    StarterPack(
        id="code-review",
        name="Code Review",
        description=(
            "Context → Bugs → Security → Style/Tests → Summarize. "
            "Iterative review with a Judge that loops until all "
            "CRITICAL and HIGH findings are patched or acknowledged."
        ),
        step_count=5,
        audience="engineers",
        tier="free",
    ),
    StarterPack(
        id="paper-polish",
        name="Paper Polish",
        description=(
            "Inspector → Diagnoser → Patcher → Formatter, with a "
            "VTO-style visual loop. Layout / citation / structure "
            "defects, with content-protection protocols. Degrades "
            "cleanly to text-only review when LaTeX/Poppler aren't "
            "available."
        ),
        step_count=4,
        audience="researchers",
        tier="free",
    ),
    # #129 — Medical Imaging Pack. Router + 5 specialist readers
    # (chest CT, head CT, X-ray, dermatology, pathology) + summarizer.
    # The workflow defaults to chest-CT (the most common case); other
    # modalities are still callable via delegate() in chat. Designed
    # as the primary evolution target for the expert-correction loop
    # (#130 / #131) — clinical principles in each reader's
    # nexus:durable region stay locked while the protocols evolve.
    StarterPack(
        id="medical-imaging",
        name="Medical Imaging",
        description=(
            "Router → specialist reader → summarizer. Structured "
            "medical-image reading with systematic protocols (chest "
            "CT default; head CT / X-ray / dermatology / pathology "
            "available via delegate). Decision support only — every "
            "output explicitly recommends professional review. "
            "Learns from your corrections."
        ),
        step_count=3,
        audience="clinicians",
        tier="free",
    ),
    # Crypto-focused starter packs (trader-briefing, smart-contract-audit)
    # were removed by user request — the product focus is shifting toward
    # knowledge / medical / general productivity verticals. The pack
    # directories under starter_packs/ were deleted alongside this
    # registry change. If we re-add crypto verticals later, recreate
    # the StarterPack entries here AND the pack directories together
    # so the registry doesn't lie about what's actually shippable.
    StarterPack(
        id="radiology-pro",
        name="Radiology Pro",
        description=(
            "Findings → Differential → Recommendation → Audit. "
            "Chain-anchored audit trail for AI-assisted radiology reports."
        ),
        step_count=4,
        audience="radiologists",
        tier="radiology_pro",
        available=False,
        coming_soon_note="Co-designing with a radiologist before launch",
    ),
]


def list_packs() -> list[StarterPack]:
    """Return the full pack catalog (both available + coming-soon)."""
    return list(PACK_CATALOG)


def get_pack(pack_id: str) -> Optional[StarterPack]:
    return next((p for p in PACK_CATALOG if p.id == pack_id), None)


# ─────────────────────────────────────────────────────────────────────
# Installation
# ─────────────────────────────────────────────────────────────────────


def install_pack(user_id: str, pack_id: str) -> workflows.Workflow:
    """Install a starter pack for ``user_id``.

    Behaviour:
      1. Looks up the pack in :data:`PACK_CATALOG`. 404s if unknown
         (raises :class:`KeyError`).
      2. Refuses if the pack is not yet ``available`` (raises
         :class:`PermissionError`). The catalog ships entries for
         coming-soon packs so the UI can render them; this guard
         keeps the install path honest.
      3. Copies the pack's skill markdown files into the user's
         skills directory (overwrite-on-install — fresh pack version
         each time).
      4. Creates (or replaces) a workflow row referencing those
         skills. Same-name workflows for this user are first
         deleted (cascading their runs) so a re-install doesn't pile
         up duplicates.

    Returns the freshly-created :class:`workflows.Workflow`.
    """
    pack = get_pack(pack_id)
    if pack is None:
        raise KeyError(f"Unknown starter pack: {pack_id}")
    if not pack.available:
        raise PermissionError(
            f"Pack '{pack_id}' isn't ready to install yet: "
            f"{pack.coming_soon_note or 'coming soon'}"
        )

    pack_dir = PACKS_ROOT / pack_id
    if not pack_dir.exists():
        raise FileNotFoundError(f"Pack assets missing on disk: {pack_dir}")

    # ─── v2.1: shared pack context ───────────────────────────────────
    # taxonomy.yaml gives every sub-agent the SAME vocabulary for
    # talking about defects / findings / categories. Lifted from
    # PaperFit's VTO taxonomy idea.
    # protocols.md is the pack's hard rules ("never silently delete a
    # figure", "never patch around access control"). These appear at
    # the bottom of every skill so the agent can't claim it didn't
    # know.
    shared_context = _build_shared_context(pack_dir)

    # ─── Copy skill files into the user's skills dir ────────────────
    skills_target = _user_skills_dir()
    skills_target.mkdir(parents=True, exist_ok=True)

    pack_skills = pack_dir / "skills"
    if pack_skills.exists():
        for skill_file in pack_skills.glob("*.md"):
            dest = skills_target / skill_file.name
            # Concatenate the skill body + shared context. Skill stays
            # the primary instructions; taxonomy/protocols come at the
            # end so the agent's last-priority anchor reinforces them.
            body = skill_file.read_text(encoding="utf-8")
            if shared_context:
                body = body.rstrip() + "\n\n" + shared_context + "\n"
            dest.write_text(body, encoding="utf-8")
            logger.info("Installed skill: %s → %s", skill_file.name, dest)

    # ─── Load workflow definition ───────────────────────────────────
    wf_path = pack_dir / "workflow.json"
    if not wf_path.exists():
        raise FileNotFoundError(f"Pack workflow.json missing: {wf_path}")
    wf_data = json.loads(wf_path.read_text(encoding="utf-8"))

    # ─── Replace any prior install of the same pack for this user ──-
    # Same-name match is the conservative join key; the metadata
    # ``source: starter-pack:<id>`` tag is also stamped so future
    # marketplaces can dedupe more precisely.
    for existing in workflows.list_workflows(user_id):
        if existing.name == wf_data["name"]:
            workflows.delete_workflow(user_id, existing.id)
            logger.info(
                "Replaced prior install of '%s' (workflow %s) for user %s",
                wf_data["name"], existing.id, user_id,
            )

    definition = workflows.WorkflowDefinition(**wf_data["definition"])
    new_wf = workflows.create_workflow(
        user_id=user_id,
        name=wf_data["name"],
        description=wf_data.get("description", ""),
        definition=definition,
    )
    logger.info(
        "Installed pack '%s' for user %s as workflow %s",
        pack_id, user_id, new_wf.id,
    )
    return new_wf


def _user_skills_dir() -> Path:
    """Where to drop installed skill files. The SkillManager default
    is ``.nexus/skills`` relative to cwd; the desktop bundle runs the
    server with cwd = $RUNE_HOME, so that resolves to
    ``$RUNE_HOME/.nexus/skills``. We mirror that here so the runtime
    resolver finds what we install.

    If the server is running with a different cwd (tests, dev runs),
    this still works — we install relative to the same Path the
    SkillManager will scan.
    """
    return Path.cwd() / ".nexus" / "skills"


def _build_shared_context(pack_dir: Path) -> str:
    """Assemble the pack's shared context block — taxonomy + protocols.

    Both files are optional. We dump their contents verbatim inside
    fenced sections so the LLM sees them but they don't get confused
    with the skill's own markdown headings.

    Layout the agent ends up seeing (appended after the skill body):

        ## Shared taxonomy (this pack)
        ```yaml
        <verbatim taxonomy.yaml>
        ```

        ## Shared protocols (this pack — hard rules)
        <verbatim protocols.md>

    Returns an empty string if neither file exists, in which case the
    install path skips concatenation entirely.
    """
    parts: list[str] = []

    tax_path = pack_dir / "taxonomy.yaml"
    if tax_path.exists():
        try:
            tax = tax_path.read_text(encoding="utf-8").strip()
        except OSError as e:  # noqa: BLE001
            logger.warning("Failed to read taxonomy.yaml for %s: %s", pack_dir.name, e)
            tax = ""
        if tax:
            parts.append(
                "## Shared taxonomy (this pack)\n\n"
                "Every sub-agent in this pack uses these category labels "
                "when describing issues or findings. Use them verbatim — "
                "consistency across steps is what makes the handoff work.\n"
                "\n```yaml\n" + tax + "\n```"
            )

    prot_path = pack_dir / "protocols.md"
    if prot_path.exists():
        try:
            prot = prot_path.read_text(encoding="utf-8").strip()
        except OSError as e:  # noqa: BLE001
            logger.warning("Failed to read protocols.md for %s: %s", pack_dir.name, e)
            prot = ""
        if prot:
            parts.append(
                "## Shared protocols (this pack — hard rules)\n\n"
                "These rules are non-negotiable for every step in this "
                "workflow. Violating them = invalid output, gatekeeper "
                "will reject.\n\n" + prot
            )

    return "\n\n".join(parts)
