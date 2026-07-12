"""SkillManager — install, load, and manage external skills.

Two on-disk layouts supported (loader auto-detects):

  Layout A — Folder per skill ("Binance Skills Hub" style, Nexus legacy):
    {skills_dir}/<skill-name>/SKILL.md      # YAML frontmatter + body
    {skills_dir}/<skill-name>/references/   # optional
    {skills_dir}/<skill-name>/.local.md     # user overrides, not distributed

  Layout B — Flat ``.claude/agents/`` style (Claude Code ecosystem):
    {skills_dir}/<skill-name>.md            # single file, frontmatter + body

Layout B is the Claude Code convention. Supporting it gives Nexus drop-in
compatibility with the existing agent ecosystem on GitHub (anthropic-
experimental/agent-cookbook and friends) — copy a ``.claude/agents/
researcher.md`` into Nexus's skills dir, it loads.

Frontmatter is also Claude-Code-compatible. Both ``name`` (Claude Code,
preferred) and ``title`` (Nexus legacy) are accepted as the display
name; both top-level and nested-under-``metadata`` ``version``/``author``
work. New fields ``model`` (per-skill model pin) and ``tools`` (tool
allow-list) are read for Phase 1 workflow support.

Skills are installed to a local directory and loaded into the LLM system
prompt.

GitHub mirror (``NEXUS_GITHUB_MIRROR``)
=======================================
Every marketplace fetch (search listings, SKILL.md metadata, install
downloads) goes to ``api.github.com`` / ``raw.githubusercontent.com``,
which are unreachable from mainland China (GFW). Set the
``NEXUS_GITHUB_MIRROR`` env var to a ghproxy-style mirror base (e.g.
``https://ghproxy.net/``) and :func:`_mirror` rewrites each GitHub URL
by prefixing it — ``https://ghproxy.net/https://raw.githubusercontent.
com/...`` (the ghproxy convention). Unset = URLs pass through
unchanged. 国内网络访问 GitHub 需设置镜像。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote as urllib_quote

logger = logging.getLogger(__name__)


# ── GitHub mirror support (NEXUS_GITHUB_MIRROR) ────────────────────
# Hosts the skills marketplace talks to. Only URLs on these hosts are
# rewritten — a mirror must never see non-GitHub traffic.
_GITHUB_HOSTS = frozenset({
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "gist.githubusercontent.com",
})


def _mirror(url: str) -> str:
    """Rewrite a GitHub URL through the configured mirror, if any.

    When the ``NEXUS_GITHUB_MIRROR`` env var is set (e.g.
    ``https://ghproxy.net/``) and ``url`` points at a GitHub host,
    returns ``mirror.rstrip('/') + '/' + url`` — the ghproxy
    convention of prefixing the FULL original URL. Otherwise returns
    ``url`` unchanged.

    NOTE: fetch sites use :func:`_fetch_bytes`, which tries the DIRECT
    URL first and only falls back to the mirrored URL when the direct
    attempt fails — a configured mirror never slows down networks that
    can reach GitHub. 国内网络直连失败时自动回退镜像。
    """
    mirror = os.environ.get("NEXUS_GITHUB_MIRROR", "").strip()
    if not mirror:
        return url
    try:
        host = url.split("//", 1)[1].split("/", 1)[0].lower()
    except IndexError:
        return url
    if host not in _GITHUB_HOSTS:
        return url
    return mirror.rstrip("/") + "/" + url


def _fetch_bytes(url: str, timeout: float = 10.0) -> bytes:
    """HTTP GET with direct-first / mirror-fallback semantics.

    1. Try the original URL directly.
    2. On ANY failure, if NEXUS_GITHUB_MIRROR is configured and the
       URL is a GitHub host (``_mirror`` returns a different URL),
       retry once through the mirror.
    3. Otherwise re-raise the original error.
    """
    import urllib.request

    def _get(u: str) -> bytes:
        req = urllib.request.Request(
            u, headers={"User-Agent": "rune-nexus/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    try:
        return _get(url)
    except Exception:
        mirrored = _mirror(url)
        if mirrored == url:
            raise
        logger.info("direct GitHub fetch failed, retrying via mirror")
        return _get(mirrored)


# Process-level latch so we attempt the Node bootstrap at most once
# per session. Repeated calls just await the cached result — even when
# it failed, we don't retry within the same process (the user would
# need to install Node manually + restart twin).
_node_bootstrap_state: dict[str, Any] = {
    "checked": False,
    "available": False,
    "method": "",  # "preinstalled" | "brew" | "failed"
    "error": "",
}
_node_bootstrap_lock = asyncio.Lock()


async def _ensure_node_available() -> bool:
    """Make sure ``npx`` is on PATH; try to install Node if it's not.

    Returns True if npx is callable after this function returns. The
    LobeHub paths call this as a preflight so the agent can self-heal
    from a missing Node install instead of repeatedly bouncing back
    "I need npx" errors at the user.

    Auto-install logic (best-effort, OS-aware):
      * macOS  — try ``brew install node`` if Homebrew is on PATH.
      * Linux  — leave it. Distro variance (apt/yum/pacman) plus sudo
        prompts make automatic install too risky to attempt headlessly.
        We surface a clean error instead.
      * Windows — same as Linux: surface a clean error.

    Idempotent within a process: caches the outcome in
    :data:`_node_bootstrap_state`. Subsequent calls return the cached
    answer instantly.
    """
    async with _node_bootstrap_lock:
        if _node_bootstrap_state["checked"]:
            return bool(_node_bootstrap_state["available"])

        # Fast path: already installed.
        if shutil.which("npx") is not None:
            _node_bootstrap_state.update(
                checked=True, available=True, method="preinstalled",
            )
            return True

        sysname = platform.system()
        if sysname == "Darwin":
            brew = shutil.which("brew")
            if brew is None:
                _node_bootstrap_state.update(
                    checked=True, available=False, method="failed",
                    error="Homebrew not installed — can't auto-install Node. "
                          "Install Homebrew (https://brew.sh) or Node directly "
                          "(https://nodejs.org/).",
                )
                logger.warning("Node bootstrap: brew missing on macOS")
                return False

            logger.info("Auto-installing Node.js via Homebrew (one-time)…")
            try:
                # ``brew install node`` is interactive on first prompt
                # but with no tty it just proceeds. Cap at 5 minutes
                # — typical install is 30-90s, slow networks may push
                # past that and we'd rather surface a timeout than
                # appear hung.
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [brew, "install", "node"],
                    capture_output=True, text=True, timeout=300,
                )
                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout or "")[:300]
                    _node_bootstrap_state.update(
                        checked=True, available=False, method="failed",
                        error=f"brew install node failed: {err}",
                    )
                    logger.warning(
                        "Node bootstrap via brew failed: %s", err,
                    )
                    return False
            except subprocess.TimeoutExpired:
                _node_bootstrap_state.update(
                    checked=True, available=False, method="failed",
                    error="brew install node timed out (5min). Network may be slow — retry manually.",
                )
                return False
            except Exception as e:
                _node_bootstrap_state.update(
                    checked=True, available=False, method="failed",
                    error=f"brew install node raised: {e}",
                )
                return False

            # Re-check PATH after install. Homebrew sometimes installs
            # to /opt/homebrew/bin which the parent process's PATH
            # already includes, but not always — the next subprocess
            # spawn will pick it up either way.
            if shutil.which("npx") is None:
                _node_bootstrap_state.update(
                    checked=True, available=False, method="failed",
                    error="brew finished but npx still not on PATH — restart the server to pick it up.",
                )
                return False

            _node_bootstrap_state.update(
                checked=True, available=True, method="brew",
            )
            logger.info("Node.js installed successfully via Homebrew")
            return True

        # Non-mac: don't try to apt/yum/choco — too varied + needs
        # sudo. Surface a helpful message and let the LLM relay it.
        _node_bootstrap_state.update(
            checked=True, available=False, method="failed",
            error=(
                f"Node.js (npx) not found on this {sysname} system. "
                "Install it from https://nodejs.org/ then retry — "
                "auto-install on this OS is not supported."
            ),
        )
        return False


@dataclass
class InstalledSkill:
    """Metadata for an installed skill.

    ``name`` is the kebab-case identifier (Claude Code convention),
    used as the addressable handle in workflows + tool invocation.
    ``title`` is the human-readable display name; falls back to
    ``name`` when the frontmatter only carries the Claude Code shape.

    ``model`` and ``tools`` are new in Phase 0 (alignment with
    ``.claude/agents/``). They drive Phase 1 workflow features:
      * ``model`` — preferred model for steps using this skill
        (``"strong"``, ``"fast"``, ``"cheap"`` workflow-level tier
        OR an explicit model id like ``"claude-sonnet-4-6"``).
        ``None`` means "use whatever the workflow / user picked".
      * ``tools`` — allowlist of tool names this skill may invoke.
        Empty list = no restriction (use the agent's default tool set).
        Mirrors the ``tools:`` array in Claude Code's spec.

    ``layout`` records which on-disk shape this skill came from so
    the exporter can round-trip without surprises.
    """
    name: str
    title: str
    description: str
    version: str
    author: str
    path: Path                          # Local directory OR file (depends on layout)
    instructions: str                   # Body (after frontmatter)
    references: dict[str, str] = field(default_factory=dict)  # filename -> content
    metadata: dict[str, Any] = field(default_factory=dict)
    model: Optional[str] = None         # per-skill model pin (Claude Code compat)
    tools: list[str] = field(default_factory=list)  # tool allow-list
    layout: str = "folder"              # "folder" (Layout A) | "flat" (Layout B)
    # #104: agentskills.io marketplace spec compatibility — each skill
    # carries its own ``license`` (e.g. "MIT", "Apache-2.0") so a
    # marketplace can show + filter on licence before install. Empty
    # string when the SKILL.md doesn't declare one.
    license: str = ""
    # #119(C): per-skill optimizer model — used only during evolve
    # runs, not at inference. Lets a skill be authored with a cheap
    # target (e.g. gemini-2.5-flash) and a strong evolver (e.g.
    # claude-opus-4-6 or gemini-2.5-pro) for ~25-45% extra gain per
    # SkillOpt §4. None = fall back to ``model`` for both.
    optimizer_model: Optional[str] = None


class SkillManager:
    """Manages skill installation, loading, and prompt injection.

    Skills are stored in `{base_dir}/skills/{skill_name}/` and loaded on startup.
    The LLM sees skill instructions as part of its system prompt.
    """

    def __init__(self, base_dir: str | Path = ".nexus"):
        self._base_dir = Path(base_dir)
        self._skills_dir = self._base_dir / "skills"
        self._skills_dir.mkdir(parents=True, exist_ok=True)
        self._skills: dict[str, InstalledSkill] = {}
        # "org/repo" -> default branch name, resolved via the GitHub
        # repos API. Instance-level so tests with a monkeypatched HTTP
        # layer never see another instance's stale entries.
        self._default_branch_cache: dict[str, str] = {}

        # Auto-load existing installed skills
        self._load_all()

    def _load_all(self) -> None:
        """Load all skills from the skills directory.

        Scans for BOTH layouts:
          * Layout A — directories containing a ``SKILL.md``.
          * Layout B — top-level ``*.md`` files (``.claude/agents/`` style).

        On name collision (a folder skill and a flat skill share a name),
        the folder takes precedence — the flat file is logged + skipped.
        Folder skills can carry references / .local.md / multi-file
        state that the flat form can't represent, so they're the
        richer artifact.
        """
        if not self._skills_dir.exists():
            return
        # Pass 1 — folder skills (Layout A, richer).
        for entry in sorted(self._skills_dir.iterdir()):
            if entry.is_dir() and (entry / "SKILL.md").exists():
                try:
                    skill = self._load_skill_folder(entry)
                    self._skills[skill.name] = skill
                    logger.info("Loaded skill: %s (%s) [folder]", skill.name, skill.title)
                except Exception as e:
                    logger.warning("Failed to load folder skill %s: %s", entry, e)

        # Pass 2 — flat `.claude/agents/`-style skills.
        for entry in sorted(self._skills_dir.iterdir()):
            if (
                entry.is_file()
                and entry.suffix == ".md"
                and entry.name != "SKILL.md"  # never confuse with folder marker
                and not entry.name.startswith(".")
            ):
                try:
                    skill = self._load_skill_flat(entry)
                    if skill.name in self._skills:
                        logger.info(
                            "Flat skill %s shadowed by existing folder skill, skipped",
                            entry.name,
                        )
                        continue
                    self._skills[skill.name] = skill
                    logger.info("Loaded skill: %s (%s) [flat]", skill.name, skill.title)
                except Exception as e:
                    logger.warning("Failed to load flat skill %s: %s", entry, e)

    def _load_skill(self, skill_dir: Path) -> InstalledSkill:
        """Backwards-compat shim — delegates to :meth:`_load_skill_folder`.

        Old callers (this module's installers + external code) still
        invoke ``_load_skill`` with a directory. New code should use
        the explicit ``_load_skill_folder`` / ``_load_skill_flat``
        methods.
        """
        return self._load_skill_folder(skill_dir)

    def _load_skill_folder(self, skill_dir: Path) -> InstalledSkill:
        """Layout A loader — directory with ``SKILL.md`` + optional refs."""
        skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        frontmatter, body = _parse_frontmatter(skill_md)

        # Default identity from the directory name; frontmatter overrides.
        # ``name`` (Claude Code) takes priority over ``title`` (legacy).
        name = (
            str(frontmatter.get("name") or "").strip()
            or skill_dir.name
        )
        title = (
            str(frontmatter.get("title") or "").strip()
            or str(frontmatter.get("name") or "").strip()
            or skill_dir.name
        )
        description = str(frontmatter.get("description") or "")

        # Version / author — accept both top-level (Claude Code style)
        # and metadata.{...} (legacy Nexus). Top-level wins when both
        # are present.
        version, author = _extract_version_author(frontmatter)
        model = _extract_model(frontmatter)
        tools = _extract_tools(frontmatter)
        license_ = _extract_license(frontmatter)
        optimizer_model = _extract_optimizer_model(frontmatter)

        # Load reference files
        references: dict[str, str] = {}
        refs_dir = skill_dir / "references"
        if refs_dir.exists():
            for ref_file in refs_dir.glob("*.md"):
                references[ref_file.name] = ref_file.read_text(encoding="utf-8")

        # Load .local.md if exists (user-specific config)
        local_md = skill_dir / ".local.md"
        local_content = ""
        if local_md.exists():
            local_content = local_md.read_text(encoding="utf-8")

        instructions = body.strip()
        if local_content:
            instructions += f"\n\n## User Configuration\n{local_content}"

        return InstalledSkill(
            name=name,
            title=title,
            description=description,
            version=str(version),
            author=str(author),
            path=skill_dir,
            instructions=instructions,
            references=references,
            metadata=frontmatter,
            model=model,
            tools=tools,
            layout="folder",
            license=license_,
            optimizer_model=optimizer_model,
        )

    def _load_skill_flat(self, skill_file: Path) -> InstalledSkill:
        """Layout B loader — single ``.claude/agents/``-style markdown file.

        The file's stem (filename without ``.md``) is the default name,
        overridable by frontmatter ``name``. No references, no
        ``.local.md`` — flat layout is single-file by design.
        """
        text = skill_file.read_text(encoding="utf-8")
        frontmatter, body = _parse_frontmatter(text)

        name = (
            str(frontmatter.get("name") or "").strip()
            or skill_file.stem
        )
        title = (
            str(frontmatter.get("title") or "").strip()
            or name
        )
        description = str(frontmatter.get("description") or "")
        version, author = _extract_version_author(frontmatter)
        model = _extract_model(frontmatter)
        tools = _extract_tools(frontmatter)
        license_ = _extract_license(frontmatter)
        optimizer_model = _extract_optimizer_model(frontmatter)

        return InstalledSkill(
            name=name,
            title=title,
            description=description,
            version=str(version),
            author=str(author),
            path=skill_file,
            instructions=body.strip(),
            references={},
            metadata=frontmatter,
            model=model,
            tools=tools,
            layout="flat",
            license=license_,
            optimizer_model=optimizer_model,
        )

    async def install(self, source: str) -> InstalledSkill:
        """Install a skill from the Anthropic skills hub or a GitHub URL.

        Supported identifier shapes (in priority order):
          - Full GitHub tree URL:
              https://github.com/anthropics/skills/tree/main/document-skills/pdf
          - 'anthropic:<name>' shortcut → rewritten to the canonical
            anthropics/skills tree URL.
          - Bare skill name ('pdf', 'docx', ...) → assumed to be an
            Anthropic skill. The GitHub installer searches the repo
            for a directory matching the name.
          - GitHub-style path 'org/repo/...' → treated as a GitHub URL.

        Earlier revs also supported lobehub: and gemini: prefixes; both
        marketplaces were dropped (LobeHub requires creds; Gemini's repo
        layout drifted from the SKILL.md convention). The Anthropic
        path is the only fully-automatable, no-auth flow we ship.

        Args:
            source: GitHub URL, anthropic: shortcut, or bare skill name.

        Returns:
            The installed skill. For repo-ROOT URLs that resolve to
            multiple candidate skill directories this raises a
            descriptive error listing the choices — callers that want
            everything should use :meth:`install_pack` instead.
        """
        return await self._install_from_github(self._normalize_source(source))

    @staticmethod
    def _normalize_source(source: str) -> str:
        """Map every supported install identifier shape onto a full
        GitHub URL (shared by :meth:`install` + :meth:`install_pack`).

          - Full GitHub URL      → passed through unchanged.
          - 'anthropic:<name>'   → canonical anthropics/skills tree URL.
          - 'org/repo[/...]'     → https://github.com/org/repo[/...].
          - bare name ('pdf')    → anthropics/skills/tree/main/skills/<name>.
        """
        if "github.com" in source:
            return source
        # Anthropic official skills repo shortcut. anthropics/skills
        # hosts pdf, docx, xlsx, pptx, mcp-builder, skill-creator and
        # friends — the canonical reference set.
        if source.startswith("anthropic:"):
            name = source[len("anthropic:"):]
            return f"https://github.com/anthropics/skills/tree/main/skills/{name}"
        # GitHub-style path (org/repo/...)
        if "/" in source and not source.startswith("/"):
            return f"https://github.com/{source}"
        # Default: bare skill name → try Anthropic skills hub.
        return f"https://github.com/anthropics/skills/tree/main/skills/{source}"

    async def install_pack(self, source: str) -> list[InstalledSkill]:
        """Install every skill a source resolves to.

        Same identifier shapes as :meth:`install`. Behaviour split:

          * URL with a specific path (``.../tree/<branch>/<dir>``,
            ``org/repo/skills/pdf``, ``anthropic:pdf``, bare names) —
            installs exactly that one skill; returns ``[skill]``.
          * Repo-ROOT URL (the github-topic search shape) — discovers
            EVERY skill directory in the repo (root SKILL.md, top-level
            ``<dir>/SKILL.md`` "skill pack" layout, and the classic
            ``skills/<name>/SKILL.md`` convention) and installs each
            one sequentially. Individual failures are collected and
            skipped; raises only when ZERO skills installed, with an
            aggregate error message.

        Returns the list of installed skills (deduped by skill name).
        """
        url = self._normalize_source(source)
        org, repo, branch, path = self._parse_github_url(url)
        if branch is None:
            branch = await self._resolve_default_branch(org, repo)

        if path:
            # Specific-path URL — behaves exactly like install().
            return [await self._install_dir(org, repo, branch, path)]

        found = await self._discover_skill_dirs(org, repo, branch)
        if not found:
            raise RuntimeError(
                f"{org}/{repo} contains no skills — no SKILL.md at the "
                f"repo root, in any top-level directory, or under a "
                f"skills/ subfolder. Please verify the URL."
            )

        installed: list[InstalledSkill] = []
        seen: set[str] = set()
        errors: list[str] = []
        for p in found:
            try:
                skill = await self._install_dir(org, repo, branch, p)
            except Exception as e:  # noqa: BLE001 — keep going, aggregate
                logger.warning(
                    "install_pack: skill dir %r in %s/%s failed: %s",
                    p or "(root)", org, repo, e,
                )
                errors.append(f"{p or '(root)'}: {e}")
                continue
            if skill.name in seen:
                continue
            seen.add(skill.name)
            installed.append(skill)

        if not installed:
            raise RuntimeError(
                f"Installing skill pack {org}/{repo} failed — none of the "
                f"{len(found)} skill dir(s) could be installed: "
                + "; ".join(errors[:5])
            )
        return installed

    async def search_anthropic_official(
        self, query: str, limit: int = 10,
    ) -> list[dict]:
        """Search Anthropic's official Skills repo (anthropics/skills).

        Same shape as :meth:`search_gemini_official` — lists the
        ``/skills`` folder via GitHub's contents API, pulls each
        SKILL.md frontmatter for name + description, returns rows
        prefixed ``anthropic:<name>`` so the install path knows to
        rewrite into a tree URL.

        We hard-code this as a built-in source (rather than relying on
        the ``claude-skills`` GitHub topic) because the canonical
        Anthropic repo doesn't have that topic tag set — yet it's the
        single highest-quality skill catalog out there. Hard-coding
        guarantees PDF / docx / pptx / xlsx etc always surface from
        the canonical source even if topic-search misses them.
        """
        # In-process cache, same pattern as the Gemini search.
        if not hasattr(self, "_anthropic_listing_cache"):
            self._anthropic_listing_cache = None
        if self._anthropic_listing_cache is None:
            try:
                api_url = (
                    "https://api.github.com/repos/anthropics/skills"
                    "/contents/skills?ref=main"
                )
                listing = await asyncio.to_thread(self._http_get_json, api_url)
            except Exception as e:
                logger.warning("Anthropic skills listing failed: %s", e)
                return []
            if not isinstance(listing, list):
                return []

            async def _meta(item):
                if item.get("type") != "dir":
                    return None
                name = item.get("name", "")
                if not name:
                    return None
                raw = (
                    f"https://raw.githubusercontent.com/anthropics/"
                    f"skills/main/skills/{name}/SKILL.md"
                )
                title = name
                description = ""
                try:
                    text = await asyncio.to_thread(self._http_get_text, raw)
                    fm = self._parse_frontmatter(text)
                    title = fm.get("name") or fm.get("title") or name
                    description = (
                        fm.get("description") or fm.get("summary") or ""
                    )
                except Exception as e:
                    logger.debug("fetching SKILL.md failed: %s", e)
                return {
                    "identifier": f"anthropic:{name}",
                    "name": str(title),
                    "description": str(description)[:200],
                    "source": "anthropic",
                    "url": (
                        f"https://github.com/anthropics/skills/"
                        f"tree/main/skills/{name}"
                    ),
                }

            metas = await asyncio.gather(*[_meta(it) for it in listing])
            self._anthropic_listing_cache = [m for m in metas if m]

        if not query:
            return self._anthropic_listing_cache[:limit]

        # Tokenise on whitespace AND separators so "gif creator"
        # matches "slack-gif-creator", "doc_writer" matches
        # "doc writer", etc. The earlier rev was a raw substring count,
        # which silently missed every multi-word query that didn't
        # match a literal hyphen-free segment of the skill name.
        import re
        def _norm(s: str) -> str:
            return re.sub(r"[\-_/]+", " ", (s or "").lower())

        q_norm = _norm(query).strip()
        if not q_norm:
            return self._anthropic_listing_cache[:limit]
        q_tokens = [t for t in q_norm.split() if t]

        scored: list[tuple[int, dict]] = []
        for row in self._anthropic_listing_cache:
            haystack = _norm(
                row["name"] + " " + row["description"] + " " + row["identifier"]
            )
            # Phrase match (highest weight) + per-token bonus so
            # "gif creator" beats just "gif" alone.
            score = haystack.count(q_norm) * 3
            for t in q_tokens:
                if t in haystack:
                    score += 1
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda x: -x[0])
        return [r for _, r in scored[:limit]]

    async def search_gemini_official(
        self, query: str, limit: int = 10,
    ) -> list[dict]:
        """Search Google's official Gemini Skills repo (google-gemini/gemini-skills).

        Lists the ``/skills`` folder via GitHub's contents API, then for each
        directory pulls the SKILL.md frontmatter (name + description) so we
        can match against the user's query. Cached on the instance for the
        process lifetime — the repo is small (~dozens of skills) and the
        listing changes slowly, so a one-shot fetch is fine.

        Returns rows shaped like ``search_lobehub`` — same keys, ``identifier``
        prefixed with ``gemini:`` so the install side can route correctly.
        """
        # Cheap in-process cache: the listing rarely changes within one
        # conversation. Repeated searches in a chat don't re-hit GitHub.
        if not hasattr(self, "_gemini_listing_cache"):
            self._gemini_listing_cache = None
        if self._gemini_listing_cache is None:
            try:
                api_url = (
                    "https://api.github.com/repos/google-gemini/gemini-skills"
                    "/contents/skills?ref=main"
                )
                listing = await asyncio.to_thread(self._http_get_json, api_url)
            except Exception as e:
                logger.warning("Gemini skills listing failed: %s", e)
                return []
            if not isinstance(listing, list):
                logger.warning("Unexpected Gemini skills listing shape")
                return []

            # Pull each SKILL.md frontmatter so search has something to
            # match against beyond the directory name. We do this in
            # parallel-ish via to_thread to avoid serialising the round
            # trips. ``asyncio.gather`` keeps it bounded by the listing
            # size (no separate concurrency cap needed for ~dozens).
            async def _meta(item):
                if item.get("type") != "dir":
                    return None
                name = item.get("name", "")
                if not name:
                    return None
                raw = (
                    f"https://raw.githubusercontent.com/google-gemini/"
                    f"gemini-skills/main/skills/{name}/SKILL.md"
                )
                title = name
                description = ""
                try:
                    text = await asyncio.to_thread(self._http_get_text, raw)
                    fm = self._parse_frontmatter(text)
                    title = fm.get("name") or fm.get("title") or name
                    description = (
                        fm.get("description") or fm.get("summary") or ""
                    )
                except Exception as e:
                    logger.debug("fetching SKILL.md failed: %s", e)  # missing SKILL.md → still keep the dir, just no desc
                return {
                    "identifier": f"gemini:{name}",
                    "name": str(title),
                    "description": str(description)[:200],
                    "source": "gemini",
                    "url": (
                        f"https://github.com/google-gemini/gemini-skills/"
                        f"tree/main/skills/{name}"
                    ),
                }

            metas = await asyncio.gather(*[_meta(it) for it in listing])
            self._gemini_listing_cache = [m for m in metas if m]

        if not query:
            return self._gemini_listing_cache[:limit]

        # Naive substring match on name + description (case-insensitive).
        # Good enough for ~dozens of skills; if the official repo grows
        # past a few hundred entries, swap in BM25 / embedding rank.
        q = query.lower()
        scored: list[tuple[int, dict]] = []
        for row in self._gemini_listing_cache:
            haystack = (
                row["name"].lower() + " " + row["description"].lower()
            )
            score = haystack.count(q)
            if score > 0 or q in row["identifier"].lower():
                scored.append((score, row))
        scored.sort(key=lambda x: -x[0])
        return [r for _, r in scored[:limit]]

    @staticmethod
    def _http_get_text(url: str) -> str:
        """HTTP GET returning text, with the User-Agent GitHub requires.

        Direct-first; falls back to NEXUS_GITHUB_MIRROR only when the
        direct fetch fails (see :func:`_fetch_bytes`).
        """
        return _fetch_bytes(url, timeout=10).decode("utf-8", errors="replace")

    async def search_github_topic(
        self, query: str, limit: int = 10,
    ) -> list[dict]:
        """Search every public GitHub repo tagged ``claude-skills`` for
        ``query`` matches.

        This is the 3rd skill marketplace alongside LobeHub (community
        catalog via npx CLI) and ``google-gemini/gemini-skills`` (Google's
        official curated repo). The ``claude-skills`` topic is the de
        facto convention third-party skill authors apply to their
        repos — querying it brings in dozens of skills neither of the
        first two sources index.

        Implementation: GitHub Search API filters via ``topic:`` qualifier.
        Anonymous calls are rate-limited to 10/min, plenty for chat-driven
        searches. Results are projected to the same ``identifier`` /
        ``name`` / ``description`` shape as the other two sources so the
        caller can interleave them transparently. Identifier prefix is
        the raw ``https://github.com/...`` URL — :meth:`install` already
        routes those through ``_install_from_github``.
        """
        if not query.strip():
            return []
        # GitHub Search API: q=topic:claude-skills+<query>
        # Sort by stars to surface the most-maintained repos first.
        encoded = urllib_quote(f"topic:claude-skills {query}")
        url = (
            f"https://api.github.com/search/repositories"
            f"?q={encoded}&sort=stars&order=desc&per_page={limit}"
        )
        try:
            data = await asyncio.to_thread(self._http_get_json, url)
        except Exception as e:
            logger.debug("GitHub topic search failed: %s", e)
            return []
        if not isinstance(data, dict):
            return []
        out: list[dict] = []
        for item in (data.get("items") or [])[:limit]:
            html_url = item.get("html_url", "")
            full_name = item.get("full_name", "")
            description = (item.get("description") or "")[:200]
            stars = item.get("stargazers_count", 0)
            if not html_url:
                continue
            out.append({
                "identifier": html_url,
                "name": item.get("name") or full_name,
                "description": description,
                "source": "github-topic",
                "stars": stars,
                "url": html_url,
            })
        return out

    @staticmethod
    def _parse_frontmatter(markdown_text: str) -> dict:
        """Extract YAML frontmatter from a SKILL.md file (best-effort).

        Returns an empty dict when the file has no frontmatter or YAML fails
        to parse — search still gets a row, just without title/description.
        """
        if not markdown_text.startswith("---"):
            return {}
        # Frontmatter spans from line 1 to the next "---" line.
        try:
            _, fm, _ = markdown_text.split("---", 2)
        except ValueError:
            return {}
        try:
            import yaml
            data = yaml.safe_load(fm)
            return data if isinstance(data, dict) else {}
        except Exception:
            # No yaml installed or invalid YAML — pull a couple of common
            # keys via regex as a last-ditch effort. Keeps search useful
            # even when yaml isn't available in the runtime.
            out: dict = {}
            for line in fm.splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    out[key.strip()] = val.strip().strip("\"'")
            return out

    # Process-level cache for LobeHub availability. The CLI requires
    # `MARKET_CLIENT_ID` + `MARKET_CLIENT_SECRET` env vars (or a prior
    # `lhm register`). Without credentials EVERY search returns
    # "No credentials found" to stderr, exits non-zero, and we waste
    # a 30s subprocess timeout per search request — across N synonym
    # variants that's minutes of dead time per agent action. We probe
    # once, cache the answer, and fast-skip subsequent calls.
    _lobehub_credentials_state: str = ""  # "" | "ok" | "missing"

    async def search_lobehub(self, query: str, limit: int = 10) -> list[dict]:
        """Search LobeHub Skills Marketplace.

        Returns [] silently if LobeHub credentials aren't configured —
        the CLI is auth-only as of @lobehub/market-cli 0.0.28. Set
        MARKET_CLIENT_ID + MARKET_CLIENT_SECRET in the agent env to
        enable.
        """
        import subprocess

        # Fast-path: we already know creds are missing → skip silently.
        if SkillManager._lobehub_credentials_state == "missing":
            return []

        # Preflight: try to install Node if missing. Best-effort — on
        # failure we just return [] (caller treats it as no results).
        await _ensure_node_available()
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["npx", "-y", "@lobehub/market-cli", "skills", "search",
                 "--q", query, "--page-size", str(limit), "--output", "json"],
                capture_output=True, text=True, timeout=30,
            )
            # Detect the auth-required failure mode and disable for the
            # rest of the process. The CLI prints this exact phrase to
            # stdout / stderr depending on version.
            combined = (result.stdout or "") + "\n" + (result.stderr or "")
            if "No credentials found" in combined:
                if SkillManager._lobehub_credentials_state != "missing":
                    logger.warning(
                        "LobeHub backend disabled — `lhm` CLI requires "
                        "MARKET_CLIENT_ID / MARKET_CLIENT_SECRET env vars "
                        "or `lhm register`. Falling back to anthropic / "
                        "gemini / GitHub topic sources only. Set those "
                        "env vars to re-enable."
                    )
                SkillManager._lobehub_credentials_state = "missing"
                return []

            if result.stdout.strip():
                import json
                data = json.loads(result.stdout.strip())
                items = data.get("items", [])
                SkillManager._lobehub_credentials_state = "ok"
                return [
                    {
                        "identifier": item.get("identifier", ""),
                        "name": item.get("name", ""),
                        "description": item.get("description", "")[:100],
                        "installs": item.get("installCount", 0),
                        "stars": item.get("github", {}).get("stars", 0),
                        "author": item.get("author", ""),
                    }
                    for item in items
                ]
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
            logger.warning("LobeHub search failed: %s", e)
        return []

    async def _install_from_lobehub(self, identifier: str) -> InstalledSkill:
        """Install a skill from LobeHub marketplace via CLI."""
        import subprocess

        # Preflight: make sure ``npx`` exists. If it doesn't, try to
        # install Node ourselves so the agent can keep going instead of
        # bouncing the work back to the user. Best-effort — if we
        # can't install Node (no brew, no admin, …) we surface a clean
        # error instead of hanging.
        await _ensure_node_available()

        dest = str(self._skills_dir / identifier)
        logger.info("Installing skill '%s' from LobeHub marketplace...", identifier)

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["npx", "-y", "@lobehub/market-cli", "skills", "install",
                 identifier, "--dir", str(self._skills_dir)],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                error = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"LobeHub install failed: {error[:200]}")

            logger.info("LobeHub install output: %s", result.stdout.strip()[:200])

        except subprocess.TimeoutExpired:
            raise RuntimeError("LobeHub install timed out (60s)")
        except FileNotFoundError:
            raise RuntimeError(
                "npx not found and auto-install of Node.js failed. "
                "Please install Node.js from https://nodejs.org/ or via Homebrew "
                "(`brew install node`)."
            )

        # Load the installed skill
        skill_dir = self._skills_dir / identifier
        if not (skill_dir / "SKILL.md").exists():
            # Try without nested directory
            for d in self._skills_dir.iterdir():
                if d.is_dir() and (d / "SKILL.md").exists() and identifier in d.name:
                    skill_dir = d
                    break
            else:
                raise RuntimeError(f"Skill installed but SKILL.md not found in {skill_dir}")

        skill = self._load_skill(skill_dir)
        self._skills[skill.name] = skill
        logger.info("Installed LobeHub skill: %s (%s)", skill.name, skill.title)
        return skill

    @staticmethod
    def _parse_github_url(url: str) -> tuple[str, str, Optional[str], str]:
        """Parse a GitHub URL into ``(org, repo, branch, path)``.

        ``branch`` is None when the URL carried no explicit
        ``/tree/<branch>/`` segment — the caller must resolve the
        repo's actual default branch (NOT assume 'main'; plenty of
        community skill repos still ship 'master' or a custom default).
        ``path`` is '' for repo-root URLs.
        """
        match = re.match(
            r"https?://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/?(.*)$",
            url,
        )
        if match:
            org, repo, branch, path = match.groups()
            return org, repo, branch, path.rstrip("/")
        match = re.match(
            r"https?://github\.com/([^/]+)/([^/]+)/?(.*)$",
            url,
        )
        if not match:
            raise ValueError(f"Cannot parse GitHub URL: {url}")
        org, repo, path = match.groups()
        return org, repo, None, path.rstrip("/")

    async def _resolve_default_branch(self, org: str, repo: str) -> str:
        """Resolve a repo's default branch via the GitHub repos API.

        Falls back to 'main' on any failure (network, rate-limit,
        unexpected payload). Cached per (org, repo) in-process — one
        API call per repo per manager instance.
        """
        key = f"{org}/{repo}"
        cached = self._default_branch_cache.get(key)
        if cached:
            return cached
        branch = "main"
        try:
            info = await asyncio.to_thread(
                self._http_get_json,
                f"https://api.github.com/repos/{org}/{repo}",
            )
            if isinstance(info, dict) and info.get("default_branch"):
                branch = str(info["default_branch"])
        except Exception as e:  # noqa: BLE001 — 'main' fallback
            logger.debug(
                "default-branch lookup failed for %s: %s (assuming 'main')",
                key, e,
            )
        self._default_branch_cache[key] = branch
        return branch

    # Top-level directory names that are never skill dirs — skipped
    # during repo-root discovery so we don't waste SKILL.md probes on
    # docs/tests/assets folders in community "skill pack" repos.
    _NON_SKILL_DIRS = frozenset({
        "docs", "doc", "test", "tests", "assets", "images",
        "scripts", "template", "spec", ".github",
    })

    # Cap on how many candidate dirs get a SKILL.md probe — keeps a
    # huge monorepo from turning discovery into hundreds of fetches.
    _DISCOVERY_CAP = 20

    async def _discover_skill_dirs(
        self, org: str, repo: str, branch: str,
    ) -> list[str]:
        """Find every skill directory in a repo (paths relative to root).

        Handles the three layouts seen in the wild:
          a. Single-skill repo — SKILL.md at the repo root → ``['']``.
          b. "Skill pack" repo — MULTIPLE top-level dirs, each with its
             own SKILL.md (common for github-topic search hits like
             ``Lambenthan/paper-discipline-skills``).
          c. Classic ``skills/<name>/SKILL.md`` convention (Anthropic /
             Gemini style) → ``'skills/<name>'`` entries.

        b and c can coexist; results are concatenated. Non-skill dirs
        (docs, tests, assets, dotdirs, …) are skipped and probing is
        capped at :data:`_DISCOVERY_CAP` per level.
        """
        raw_root = f"https://raw.githubusercontent.com/{org}/{repo}/{branch}"

        async def _has_skill_md(rel: str) -> bool:
            probe = f"{raw_root}/{rel}/SKILL.md" if rel else f"{raw_root}/SKILL.md"
            try:
                await asyncio.to_thread(self._http_get_text, probe)
                return True
            except Exception:  # noqa: BLE001 — 404 / network = "no"
                return False

        # a. Root SKILL.md → single root-level skill.
        if await _has_skill_md(""):
            return [""]

        # b. List the repo ROOT and probe candidate top-level dirs.
        listing_url = (
            f"https://api.github.com/repos/{org}/{repo}/contents?ref={branch}"
        )
        try:
            listing = await asyncio.to_thread(self._http_get_json, listing_url)
        except Exception as e:
            raise RuntimeError(
                f"Can't list the contents of {org}/{repo}@{branch} ({e}). "
                f"Please pass a more specific URL like "
                f"https://github.com/{org}/{repo}/tree/{branch}/<skill-dir>."
            )
        if not isinstance(listing, list):
            listing = []
        root_dirs = [
            str(it.get("name") or "")
            for it in listing
            if it.get("type") == "dir" and it.get("name")
        ]

        found: list[str] = []
        candidates = [
            d for d in root_dirs
            if not d.startswith(".")
            and d.lower() not in self._NON_SKILL_DIRS
            and d != "skills"  # handled explicitly below
        ]
        for d in candidates[:self._DISCOVERY_CAP]:
            if await _has_skill_md(d):
                found.append(d)

        # c. Classic skills/ subfolder — probe each child dir too.
        if "skills" in root_dirs:
            sub_url = (
                f"https://api.github.com/repos/{org}/{repo}"
                f"/contents/skills?ref={branch}"
            )
            try:
                sub = await asyncio.to_thread(self._http_get_json, sub_url)
            except Exception as e:  # noqa: BLE001
                logger.debug("skills/ listing failed for %s/%s: %s",
                             org, repo, e)
                sub = []
            if isinstance(sub, list):
                subdirs = [
                    str(it.get("name") or "")
                    for it in sub
                    if it.get("type") == "dir" and it.get("name")
                    and not str(it.get("name")).startswith(".")
                ]
                for d in subdirs[:self._DISCOVERY_CAP]:
                    if await _has_skill_md(f"skills/{d}"):
                        found.append(f"skills/{d}")

        return found

    async def _install_from_github(self, url: str) -> InstalledSkill:
        """Download a single skill folder from GitHub.

        Repo-ROOT URLs (no path — the github-topic search shape) go
        through :meth:`_discover_skill_dirs`; exactly one discovered
        skill installs directly, multiple raise a descriptive error
        listing the choices (use :meth:`install_pack` to grab them all).
        """
        org, repo, branch, path = self._parse_github_url(url)
        if branch is None:
            # No explicit /tree/<branch>/ — never hardcode 'main';
            # community repos frequently default to 'master' etc.
            branch = await self._resolve_default_branch(org, repo)

        if path == "":
            found = await self._discover_skill_dirs(org, repo, branch)
            if not found:
                raise RuntimeError(
                    f"{org}/{repo} has no SKILL.md at root, in any "
                    f"top-level directory, or under a skills/ subfolder. "
                    f"Not a recognised skill repo layout — please verify "
                    f"the URL."
                )
            if len(found) == 1:
                path = found[0]
                logger.info(
                    "Repo %s/%s resolved to one skill dir: %s",
                    org, repo, path or "(root)",
                )
            else:
                choices = [p or "(root)" for p in found]
                raise RuntimeError(
                    f"{org}/{repo} is a multi-skill repo with "
                    f"{len(found)} skills: {', '.join(choices[:10])}"
                    f"{', …' if len(found) > 10 else ''}. "
                    f"Pick one and pass identifier="
                    f"'https://github.com/{org}/{repo}/tree/{branch}/"
                    f"<dir>' — or install them all at once via "
                    f"install_pack."
                )

        return await self._install_dir(org, repo, branch, path)

    # Files larger than this are skipped during skill-dir download —
    # scripts/assets referenced by SKILL.md are typically tiny; big
    # binaries would bloat the skills dir and slow installs.
    _MAX_SKILL_FILE_BYTES = 2 * 1024 * 1024  # 2 MB

    async def _install_dir(
        self, org: str, repo: str, branch: str, path: str,
    ) -> InstalledSkill:
        """Download ONE skill directory from GitHub into the skills dir.

        Shared by :meth:`_install_from_github` (single install) and
        :meth:`install_pack` (bulk). Downloads:
          * ``SKILL.md`` (required — failure aborts this dir),
          * every other top-level FILE in the dir via the contents API
            (scripts / assets referenced by SKILL.md; entries larger
            than :data:`_MAX_SKILL_FILE_BYTES` are skipped),
          * ``references/*.md`` (legacy behaviour, kept).

        ``path`` is the skill dir relative to the repo root; '' means
        the skill lives AT the root (skill name falls back to the repo
        name).
        """
        path = (path or "").strip("/")
        skill_name = path.split("/")[-1] if path else repo
        dest = self._skills_dir / skill_name

        logger.info(
            "Installing skill '%s' from %s/%s@%s (%s)...",
            skill_name, org, repo, branch, path or "repo root",
        )

        raw_base = (
            f"https://raw.githubusercontent.com/{org}/{repo}/{branch}"
            + (f"/{path}" if path else "")
        )

        # Download SKILL.md first — the one required artifact.
        dest.mkdir(parents=True, exist_ok=True)
        await self._download_file(f"{raw_base}/SKILL.md", dest / "SKILL.md")

        # Download every other top-level FILE in the skill dir so
        # scripts / assets referenced by SKILL.md come along. Failures
        # here are non-fatal — the skill still works from SKILL.md.
        contents_seg = f"contents/{path}" if path else "contents"
        dir_url = (
            f"https://api.github.com/repos/{org}/{repo}/{contents_seg}"
            f"?ref={branch}"
        )
        try:
            entries = await asyncio.to_thread(self._http_get_json, dir_url)
        except Exception as e:  # noqa: BLE001
            logger.debug("skill dir listing failed (%s): %s", dir_url, e)
            entries = []
        if isinstance(entries, list):
            for item in entries:
                if item.get("type") != "file":
                    continue
                fname = str(item.get("name") or "")
                # SKILL.md is already down; reject anything that could
                # escape the dest dir (defensive — the API never
                # returns slashes in ``name``).
                if not fname or fname == "SKILL.md" or "/" in fname or "\\" in fname:
                    continue
                try:
                    size = int(item.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                if size > self._MAX_SKILL_FILE_BYTES:
                    logger.info(
                        "Skipping %s/%s (%d bytes > %d limit)",
                        skill_name, fname, size, self._MAX_SKILL_FILE_BYTES,
                    )
                    continue
                dl_url = item.get("download_url")
                if not dl_url:
                    continue
                try:
                    await self._download_file(dl_url, dest / fname)
                except Exception as e:  # noqa: BLE001 — optional file
                    logger.debug("optional skill file %s failed: %s",
                                 fname, e)

        # Try to download common reference files
        refs_dir = dest / "references"
        refs_dir.mkdir(exist_ok=True)

        # Fetch directory listing via GitHub API to find reference files
        refs_seg = f"contents/{path}/references" if path else "contents/references"
        api_url = (
            f"https://api.github.com/repos/{org}/{repo}/{refs_seg}"
            f"?ref={branch}"
        )
        try:
            result = await asyncio.to_thread(
                self._http_get_json, api_url
            )
            if isinstance(result, list):
                for item in result:
                    if item.get("name", "").endswith(".md"):
                        await self._download_file(
                            item["download_url"],
                            refs_dir / item["name"],
                        )
        except Exception as e:
            logger.debug("No references directory or API error: %s", e)

        # Load the installed skill
        skill = self._load_skill(dest)
        self._skills[skill.name] = skill
        logger.info("Installed skill: %s (%s) — %d reference files",
                     skill.name, skill.title, len(skill.references))
        return skill

    async def _download_file(self, url: str, dest: Path, timeout: float = 15.0) -> None:
        """Download a file from URL to local path with a hard timeout.

        ``urllib.request.urlretrieve`` does NOT accept a timeout — if the
        remote is slow or the file is missing in a way that hangs the
        connection, the call blocks forever. That bug took down the
        whole "install skill" flow: the LLM's tool call never returned,
        the desktop sat on "Agent is thinking…" indefinitely, and the
        user couldn't even send a new message to escape.
        We use ``urlopen`` (which DOES accept timeout) + a streamed
        copy to disk instead, so any single download is bounded.
        """
        def _do_download() -> None:
            # Direct-first; NEXUS_GITHUB_MIRROR fallback on failure —
            # install downloads (SKILL.md + reference files) work
            # behind the GFW with a mirror set, without slowing down
            # networks that reach GitHub directly.
            data = _fetch_bytes(url, timeout=timeout)
            dest.write_bytes(data)

        await asyncio.to_thread(_do_download)

    def _http_get_json(self, url: str) -> Any:
        """Simple HTTP GET returning JSON.

        Direct-first; falls back to NEXUS_GITHUB_MIRROR only when the
        direct fetch fails (see :func:`_fetch_bytes`).
        """
        import json
        return json.loads(_fetch_bytes(url, timeout=10).decode())

    def install_local(self, path: str | Path) -> InstalledSkill:
        """Install a skill from a local directory.

        Copies the skill folder to the skills directory. Only valid
        for Layout A (folder with SKILL.md). For ``.claude/agents/``
        single-file imports, use :meth:`install_from_claude_agents`.
        """
        src = Path(path)
        if not (src / "SKILL.md").exists():
            raise FileNotFoundError(f"No SKILL.md found in {src}")

        skill_name = src.name
        dest = self._skills_dir / skill_name

        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)

        skill = self._load_skill(dest)
        self._skills[skill.name] = skill
        logger.info("Installed local skill: %s (%s)", skill.name, skill.title)
        return skill

    def install_from_claude_agents(self, path: str | Path) -> InstalledSkill:
        """Install a ``.claude/agents/``-style agent markdown file.

        Path can point to:
          * A single ``.md`` file (e.g. ``~/proj/.claude/agents/researcher.md``).
          * A directory — installs every ``.md`` file in it as a flat skill.

        Files are copied verbatim into the skills dir under their
        original filename. The flat-layout loader picks them up.
        Frontmatter ``name`` is the addressable identifier; falls
        back to the filename stem.

        Returns the installed skill (or the LAST one if a directory
        was passed; iterate ``installed`` for the full set).
        """
        src = Path(path).expanduser()
        if not src.exists():
            raise FileNotFoundError(f"Path not found: {src}")

        if src.is_file():
            if src.suffix != ".md":
                raise ValueError(f"Not a markdown file: {src}")
            return self._install_flat_file(src)

        # Directory — install all .md files inside.
        installed: list[InstalledSkill] = []
        for md in sorted(src.glob("*.md")):
            if md.name.startswith(".") or md.name == "SKILL.md":
                continue
            try:
                installed.append(self._install_flat_file(md))
            except Exception as e:
                logger.warning("Skipped %s during bulk import: %s", md, e)
        if not installed:
            raise RuntimeError(f"No installable .md files found in {src}")
        return installed[-1]

    def _install_flat_file(self, src: Path) -> InstalledSkill:
        """Copy a single Claude Code agent file into the skills dir."""
        # Preserve the original filename so the round-trip (export →
        # import) is identity. The frontmatter `name` is the
        # addressable identifier, but the filename is what the user
        # sees on disk.
        dest = self._skills_dir / src.name
        if dest.exists():
            dest.unlink()
        shutil.copyfile(src, dest)

        skill = self._load_skill_flat(dest)
        # Evict any previously-loaded skill of the same name (re-install).
        self._skills[skill.name] = skill
        logger.info(
            "Installed Claude Code agent: %s (%s) from %s",
            skill.name, skill.title, src,
        )
        return skill

    def export_to_claude_agents(
        self, name: str, dest_dir: str | Path,
    ) -> Path:
        """Export an installed skill as a ``.claude/agents/``-compatible
        single-file markdown.

        Writes ``{dest_dir}/{name}.md`` with frontmatter normalized to
        the Claude Code shape:

          ---
          name: <skill-name>
          description: <one-line>
          model: <hint>     # only if set
          tools: [...]      # only if non-empty
          version: <ver>    # only if non-default
          author: <author>  # only if set
          ---

          <body>

        Folder-skill references / ``.local.md`` are NOT included — the
        flat layout can't carry them. Caller is responsible for
        bundling references separately if needed.

        Returns the path to the written file.
        """
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(f"No installed skill named {name!r}")

        dest = Path(dest_dir).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        out_path = dest / f"{skill.name}.md"

        # Build frontmatter in a stable, readable order. Only include
        # fields that have meaningful values so the output stays clean.
        lines: list[str] = ["---"]
        lines.append(f"name: {skill.name}")
        # description: quote if it contains a colon to keep the YAML
        # parser sane.
        desc = skill.description.replace("\n", " ").strip()
        if ":" in desc:
            desc = f'"{desc}"'
        lines.append(f"description: {desc}")
        if skill.model:
            lines.append(f"model: {skill.model}")
        if skill.tools:
            lines.append(f"tools: [{', '.join(skill.tools)}]")
        if skill.version and skill.version != "0.0.0":
            lines.append(f"version: {skill.version}")
        if skill.author:
            lines.append(f"author: {skill.author}")
        lines.append("---")
        lines.append("")
        lines.append(skill.instructions.strip())
        lines.append("")

        out_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Exported skill %s → %s", skill.name, out_path)
        return out_path

    def uninstall(self, name: str) -> bool:
        """Remove an installed skill."""
        skill = self._skills.pop(name, None)
        if skill and skill.path.exists():
            shutil.rmtree(skill.path)
            logger.info("Uninstalled skill: %s", name)
            return True
        return False

    def get(self, name: str) -> Optional[InstalledSkill]:
        """Get an installed skill by name."""
        return self._skills.get(name)

    @property
    def installed(self) -> list[InstalledSkill]:
        """List all installed skills."""
        return list(self._skills.values())

    @property
    def names(self) -> list[str]:
        """List installed skill names."""
        return list(self._skills.keys())

    def get_prompt_context(self) -> str:
        """Generate the skill context block for LLM system prompt.

        Returns a formatted string containing all skill instructions,
        ready to be appended to the system prompt.
        """
        if not self._skills:
            return ""

        # Hermes-style: only inject skill INDEX (name + description),
        # not full instructions. Agent loads full skill via tool when needed.
        parts = ["\n\n## Installed Skills"]
        parts.append("Before replying, scan these skills. If one matches the user's request, "
                      "use the skill's name as a reference. Full instructions are loaded on demand.\n")
        for skill in self._skills.values():
            desc = skill.description[:80] + "..." if len(skill.description) > 80 else skill.description
            refs = f" (refs: {', '.join(skill.references.keys())})" if skill.references else ""
            parts.append(f"- **{skill.name}**: {desc}{refs}")

        parts.append("")

        return "\n".join(parts)

    # ── LobeHub MCP Marketplace ──

    # ── Curated MCP catalog (#159 续) ────────────────────────────────
    #
    # Hand-vetted shortlist baked into the SDK so search_mcp doesn't
    # depend on LobeHub auth (the lhm CLI requires MARKET_CLIENT_ID,
    # which 99% of self-hosted deployments don't have). Loaded once,
    # cached process-wide. See curated_mcp.json for the schema.

    _curated_cache: Optional[list[dict]] = None

    @classmethod
    def _curated_catalog(cls) -> list[dict]:
        if cls._curated_cache is not None:
            return cls._curated_cache
        catalog_path = Path(__file__).parent / "curated_mcp.json"
        try:
            with open(catalog_path, "r") as f:
                data = json.load(f)
            cls._curated_cache = data.get("items", [])
        except Exception as e:  # noqa: BLE001
            logger.warning("curated_mcp.json load failed: %s", e)
            cls._curated_cache = []
        return cls._curated_cache

    @staticmethod
    def _match_curated(items: list[dict], query: str) -> list[dict]:
        """Substring-match the query against name + description + keywords."""
        q = query.lower().strip()
        if not q:
            return []
        out: list[tuple[int, dict]] = []
        for it in items:
            score = 0
            if q in (it.get("name", "") or "").lower():
                score += 5
            for kw in it.get("keywords", []):
                if q in kw.lower() or kw.lower() in q:
                    score += 3
            if q in (it.get("description", "") or "").lower():
                score += 1
            if q in (it.get("category", "") or "").lower():
                score += 1
            if score > 0:
                out.append((score, it))
        out.sort(key=lambda r: -r[0])
        return [it for _, it in out]

    async def search_mcp(self, query: str, limit: int = 10) -> list[dict]:
        """Search the curated MCP catalog.

        Reads ``curated_mcp.json`` (hand-vetted, ~30 servers covering
        the common cases — chains, SaaS, dev tools, databases). Zero
        auth, zero network — ideal for the agent to surface vetted
        options without hitting external services.

        Earlier revs also tried LobeHub and Smithery as supplementary
        backends. Both ended up not earning their keep:
          * LobeHub (@lobehub/market-cli) silently requires
            MARKET_CLIENT_ID/_SECRET — no creds = 30 s subprocess per
            query for zero results.
          * Smithery search is fully public and finds ~3000 servers,
            but every hosted entry needs OAuth to actually install
            (out of scope for a server-side agent). Surfaced 3000
            results that the agent then couldn't act on.
        Both removed; if/when we want the long tail back, do it via
        a dedicated tool with a clear UX rather than a silent layer.

        Returns: list of {identifier, name, description, tools_count, source}.
        Empty list = nothing matched in the catalog — caller can
        legitimately say "nothing found, fall back to web_search".
        """
        results: list[dict] = []
        for it in self._match_curated(self._curated_catalog(), query)[:limit]:
            results.append({
                "identifier":  it.get("identifier", ""),
                "name":        it.get("name", ""),
                "description": (it.get("description") or "")[:140],
                "author":      it.get("trust", "curated"),
                "tools_count": it.get("tools_count", 0),
                "category":    it.get("category", ""),
                "source":      "curated",
            })
        return results

    async def install_mcp(self, identifier: str, tool_registry=None) -> dict:
        """Install an MCP server and register its tools.

        Identifier formats:
          * ``npm:<package>``       → run ``npx -y <package>`` directly.
            Used by curated catalog entries pointing at npm-published
            MCP servers (most Anthropic-official ones live here).
          * ``github:owner/repo``  → not implemented yet — log + return.
          * Anything else          → treat as a LobeHub marketplace id
                                     and go through the lhm CLI (needs
                                     MARKET_CLIENT_ID auth).

        Args:
            identifier: prefixed identifier as above
            tool_registry: ToolRegistry to register the new tools into

        Returns:
            {"name": ..., "tools": [...]}, or {..., "error": ...}
        """
        logger.info("Installing MCP server '%s'...", identifier)

        # Curated path: npm:@scope/pkg
        if identifier.startswith("npm:"):
            package = identifier[len("npm:"):]
            if not tool_registry:
                return {"name": package, "tools": [],
                        "note": "No tool_registry provided"}
            try:
                from ..mcp import MCPServerConfig
                config = MCPServerConfig(
                    name=package,
                    transport="stdio",
                    command="npx",
                    args=["-y", package],
                )
                tool_names = await tool_registry.register_mcp_server(config)
                logger.info(
                    "MCP server '%s' installed (npm): %d tools",
                    package, len(tool_names),
                )
                return {"name": package, "tools": tool_names, "source": "npm"}
            except Exception as e:  # noqa: BLE001
                logger.warning("npm MCP install failed for %s: %s", package, e)
                return {"name": package, "tools": [], "error": str(e)}

        # Curated path: github:owner/repo (deferred — agent can fall
        # back to manual git clone via Bash if it really wants this).
        if identifier.startswith("github:"):
            return {
                "name": identifier,
                "tools": [],
                "error": (
                    "github:* MCP install not implemented yet — try the "
                    "npm: equivalent if available, or install via Bash + "
                    "register the resulting binary manually."
                ),
            }

        # Smithery path: smithery:<qualified-name>
        # Hosted Smithery MCP servers require an OAuth round-trip
        # (auth.smithery.ai/<server>/authorize) which we cannot drive
        # headlessly from a server-side agent. Return a structured
        # error with actionable next steps so the agent can tell the
        # user exactly how to complete the install. Self-hosted
        # Smithery entries (rare) would have surfaced as `npm:` in
        # their search payload, hitting the curated branch above.
        if identifier.startswith("smithery:"):
            qn = identifier[len("smithery:"):]
            return {
                "name": qn,
                "tools": [],
                "source": "smithery",
                "error": (
                    f"Smithery hosted MCP servers need a one-time OAuth "
                    f"authorization that the server-side agent can't "
                    f"complete on its own. To finish install:\n"
                    f"  1. Open https://smithery.ai/server/{qn} in a "
                    f"browser and click Connect.\n"
                    f"  2. On the agent host, run "
                    f"`npx -y @smithery/cli install {qn}` once — "
                    f"it'll register the server in your local config "
                    f"using the auth from step 1.\n"
                    f"Hosted-server auto-install is tracked separately."
                ),
            }

        # No matching backend. Earlier rev would have tried a LobeHub
        # marketplace fallback here, but that path required
        # MARKET_CLIENT_ID/_SECRET and silently failed for every
        # un-credentialed agent. With LobeHub removed the only valid
        # identifiers are `npm:` (curated) and `smithery:` (above);
        # anything else is a typo or hallucination from the LLM.
        return {
            "name": identifier, "tools": [],
            "error": (
                f"Unknown MCP identifier '{identifier}'. Valid prefixes:\n"
                f"  * npm:<package>   (curated catalog — direct install)\n"
                f"  * smithery:<name> (Smithery registry — needs OAuth)\n"
                f"Run manage_mcp(action='search', query='...') to see "
                f"matching identifiers."
            ),
        }


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from a markdown file.

    Returns (frontmatter_dict, body_text).
    Hand-rolled parser — no PyYAML dependency. Handles:
      * Top-level scalars:           ``name: foo``
      * Nested objects (1-level):    ``metadata:\\n  version: 1.0``
      * Inline lists:                ``tools: [Read, Edit]``
      * Block lists (1-level):       ``tools:\\n  - Read\\n  - Edit``

    Anything fancier (deep nesting, multi-line strings, anchors) isn't
    needed for ``.claude/agents/`` or Nexus skills, so we don't try.
    """
    if not text.startswith("---"):
        return {}, text

    # Find closing ---
    end = text.find("---", 3)
    if end < 0:
        return {}, text

    yaml_block = text[3:end].strip()
    body = text[end + 3:].strip()

    frontmatter: dict[str, Any] = {}
    current_key: Optional[str] = None  # last top-level key with an open value (dict or list)
    pending_key: Optional[str] = None  # ``key:`` line with no value yet — type TBD

    def _scalar(raw: str) -> str:
        return raw.strip().strip("\"'")

    def _inline_list(raw: str) -> list[str]:
        # "[a, b, c]" → ["a","b","c"]
        inner = raw.strip()
        if inner.startswith("[") and inner.endswith("]"):
            inner = inner[1:-1]
        return [
            item.strip().strip("\"'")
            for item in inner.split(",")
            if item.strip()
        ]

    for raw_line in yaml_block.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())

        # Continuation of a pending or open block list / dict:
        # ``  - item``  →  it's a list.
        if stripped.startswith("-") and (pending_key or current_key):
            target_key = pending_key or current_key
            # First "-" after a pending key materialises the value as a list.
            if pending_key:
                frontmatter[pending_key] = []
                current_key = pending_key
                pending_key = None
            elif not isinstance(frontmatter.get(current_key), list):
                # current_key was an open dict but we're seeing a list
                # entry — flip to list (typical when user writes
                # ``metadata:\n  - x`` which we'd previously assumed was a
                # dict).
                frontmatter[current_key] = []
            item = _scalar(stripped[1:])
            if item:
                frontmatter[current_key].append(item)
            continue

        # Indented ``key: value`` → nested scalar under the open dict.
        if indent > 0 and ":" in stripped:
            # Promote a pending key into an open dict on first nested write.
            if pending_key:
                frontmatter[pending_key] = {}
                current_key = pending_key
                pending_key = None
            if current_key is not None and isinstance(frontmatter.get(current_key), dict):
                k, _, v = stripped.partition(":")
                frontmatter[current_key][k.strip()] = _scalar(v)
                continue

        # Any non-continuation line at indent 0 closes the previous
        # open structure. A pending_key with no follow-up resolves to
        # an empty dict so it's harmless to consumers that just probe
        # for keys.
        if indent == 0:
            if pending_key is not None:
                frontmatter.setdefault(pending_key, {})
                current_key = pending_key
                pending_key = None

        if ":" not in stripped:
            continue

        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()

        if not value:
            # ``key:`` with no inline value — defer typing until we
            # see what the next line looks like.
            pending_key = key
            current_key = None  # close any prior open struct
            continue

        if value.startswith("["):
            frontmatter[key] = _inline_list(value)
            current_key = key
            pending_key = None
            continue

        # Plain scalar value.
        frontmatter[key] = _scalar(value)
        current_key = key
        pending_key = None

    # Trailing pending key with no follow-up: keep it as an empty dict.
    if pending_key is not None:
        frontmatter.setdefault(pending_key, {})

    return frontmatter, body


# ── Helpers shared by folder + flat loaders ────────────────────────

def _extract_version_author(fm: dict) -> tuple[str, str]:
    """Pull version + author from frontmatter, accepting both shapes.

    Claude Code style: top-level ``version: 1.0`` / ``author: alice``.
    Nexus legacy:      nested ``metadata: { version: 1.0, author: alice }``.

    Top-level wins when both are present so a migrated skill that
    carries an updated top-level value isn't masked by stale legacy
    ``metadata.*``.
    """
    version = fm.get("version")
    author = fm.get("author")
    metadata = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    if not version:
        version = metadata.get("version", "0.0.0")
    if not author:
        author = metadata.get("author", "")
    return str(version), str(author)


def _extract_model(fm: dict) -> Optional[str]:
    """Read the model hint. Accept either ``model:`` (Claude Code) or
    ``metadata.model:`` (Nexus legacy). Returns None when absent.

    Values can be:
      * Workflow tier hints: ``strong`` / ``fast`` / ``cheap``.
      * Explicit model ids: ``claude-sonnet-4-6``, ``claude-haiku-4-5``,
        etc. — passed through verbatim to the runtime.
    """
    val = fm.get("model")
    if val:
        return str(val).strip() or None
    metadata = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    val = metadata.get("model")
    return (str(val).strip() or None) if val else None


def _extract_tools(fm: dict) -> list[str]:
    """Read the tool allow-list. Accept Claude Code shapes:
      * ``tools: [Read, Edit]`` (inline)
      * ``tools:\\n  - Read\\n  - Edit`` (block)
    Also accepts the agentskills.io ``allowed-tools`` synonym.
    Empty / missing = no restriction.
    """
    val = fm.get("tools") or fm.get("allowed-tools")
    if isinstance(val, list):
        return [str(t).strip() for t in val if str(t).strip()]
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    return []


def _extract_license(fm: dict) -> str:
    """#104: agentskills.io marketplace alignment — each skill carries a
    per-skill ``license`` field in SKILL.md frontmatter. Accept the
    top-level shape (Claude Code + agentskills.io convention) and the
    legacy ``metadata.license`` nesting. Empty string when absent so
    callers can treat "no license declared" uniformly.
    """
    val = fm.get("license")
    if val:
        return str(val).strip()
    metadata = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    return str(metadata.get("license") or "").strip()


# ── #119: SkillOpt-style evolution helpers ────────────────────────


# Fenced markers that delineate the SKILL.md region that step-level
# evolution must NOT touch. The validation-gated evolver passes these
# blocks through verbatim — only content outside them is editable.
DURABLE_OPEN  = "<!-- nexus:durable -->"
DURABLE_CLOSE = "<!-- /nexus:durable -->"


def _extract_optimizer_model(fm: dict) -> Optional[str]:
    """#119(C) target/optimizer split — a skill can declare a
    stronger model to use ONLY during evolve runs. Inference still
    uses the cheap ``model`` field. None = use the same model for
    both. Accept top-level ``optimizer_model`` (preferred) and
    ``metadata.optimizer_model`` (legacy).
    """
    val = fm.get("optimizer_model")
    if val:
        return str(val).strip() or None
    metadata = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    val = metadata.get("optimizer_model")
    return (str(val).strip() or None) if val else None


def extract_durable_regions(body: str) -> tuple[list[str], str]:
    """#119(B) protected region. Returns
    ``(durable_blocks, editable_body)`` — durable_blocks is the list
    of inner contents from each fenced region, editable_body is the
    skill text with each fenced block REPLACED by a placeholder so
    optimizers can re-thread them on edit.

    Placeholder uses an opaque sentinel so optimizer LLMs are
    unlikely to mangle it. The reverse op is :func:`reweave_durable`.
    """
    if DURABLE_OPEN not in body:
        return [], body
    out_blocks: list[str] = []
    cursor = 0
    pieces: list[str] = []
    while True:
        i = body.find(DURABLE_OPEN, cursor)
        if i < 0:
            pieces.append(body[cursor:])
            break
        pieces.append(body[cursor:i])
        end = body.find(DURABLE_CLOSE, i)
        if end < 0:
            # Malformed — bail and treat the rest as editable so we
            # don't blackhole the file.
            pieces.append(body[i:])
            break
        inner = body[i + len(DURABLE_OPEN): end].strip("\n")
        out_blocks.append(inner)
        pieces.append(f"<<<NEXUS_DURABLE_{len(out_blocks)-1}>>>")
        cursor = end + len(DURABLE_CLOSE)
    return out_blocks, "".join(pieces)


def reweave_durable(editable_body: str, durable_blocks: list[str]) -> str:
    """Reverse of :func:`extract_durable_regions`. Replaces each
    ``<<<NEXUS_DURABLE_N>>>`` placeholder with its protected block
    re-fenced. If the optimizer accidentally deleted a placeholder
    we re-append the block at the end so durable rules are never
    lost."""
    out = editable_body
    used: set[int] = set()
    for idx, inner in enumerate(durable_blocks):
        ph = f"<<<NEXUS_DURABLE_{idx}>>>"
        fenced = f"{DURABLE_OPEN}\n{inner}\n{DURABLE_CLOSE}"
        if ph in out:
            out = out.replace(ph, fenced, 1)
            used.add(idx)
    for idx, inner in enumerate(durable_blocks):
        if idx not in used:
            out += f"\n\n{DURABLE_OPEN}\n{inner}\n{DURABLE_CLOSE}\n"
    return out
