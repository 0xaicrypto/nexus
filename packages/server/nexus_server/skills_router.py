"""Per-user skills management API + chat prompt injection helpers.

Endpoints (all auth-gated via ``Depends(get_current_user)``):

  * ``GET    /api/v1/skills``               — list installed skills with the
    per-user enabled/auto_apply overlay from ``user_skill_prefs``.
  * ``GET    /api/v1/skills/search``        — proxy the SDK SkillManager's
    marketplace search (``source=official`` → anthropics/skills catalog,
    ``source=github`` → the ``claude-skills`` GitHub topic).
  * ``POST   /api/v1/skills/install``       — install by identifier via the
    user's SkillManager (``anthropic:pdf``, a GitHub tree URL, a bare name).
  * ``DELETE /api/v1/skills/{name}``        — uninstall (manager remove +
    defensive on-disk cleanup for both folder and flat layouts).
  * ``POST   /api/v1/skills/{name}/toggle`` — persist enabled / auto_apply
    to ``user_skill_prefs``.

Per-user isolation
==================
Skills live under the same directory tree the user's DigitalTwin uses:
``{TWIN_BASE_DIR}/{user_id}/skills/`` (twin constructs its SkillManager
with ``base_dir=<user twin dir>``; see nexus/twin.py + twin_manager).
Because the path is keyed by user_id, one user's installs are invisible
to every other user — no overlay-based scoping needed.

Two SkillManager views exist:

  * The DISK view — a fresh ``SkillManager`` built per request over the
    user's skills dir. Always sees everything installed on disk,
    including skills the user has disabled. All API endpoints use this.
  * The LIVE-TWIN view — ``twin.skills`` on a cached DigitalTwin (legacy
    /llm/chat path folds ``twin.skills.get_prompt_context()`` into its
    system prompt). We keep that cache in sync: toggling a skill off
    pops it from the live twin's in-memory dict, toggling back on
    reloads it from disk, and :func:`apply_disabled_overlay` filters
    disabled skills out at twin cold-start (called by twin_manager).

Chat injection (v2 path)
========================
``build_skills_block(user_id, requested)`` composes the ``## Skill: x``
sections that chat_router appends to the tiered-retrieval system prompt:

  * every installed+enabled skill whose name appears in ``requested``
    (the explicit "/" invocation from the composer menu); unknown /
    disabled names are silently dropped;
  * plus every installed+enabled skill flagged ``auto_apply=1`` — those
    ride along on EVERY v2 turn without explicit invocation.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from nexus_server.auth.routes import get_current_user
from nexus_server.database import get_db_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


# Skill names come from directory names / frontmatter — constrain to a
# safe charset so a crafted name can never traverse out of the user's
# skills dir (DELETE takes the name as a path segment).
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _name_is_safe(name: str) -> bool:
    return bool(_SAFE_NAME_RE.match(name)) and ".." not in name


# ── user_skill_prefs schema ───────────────────────────────────────────


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent — also mirrored in database.init_db for fresh DBs."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_skill_prefs (
            user_id    TEXT NOT NULL,
            skill_name TEXT NOT NULL,
            enabled    INTEGER NOT NULL DEFAULT 1,
            auto_apply INTEGER NOT NULL DEFAULT 0,
            source     TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, skill_name)
        )
        """
    )
    # Guard for pre-existing tables that predate a column (ALTER has no
    # IF NOT EXISTS in SQLite).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(user_skill_prefs)")}
    if "auto_apply" not in cols:
        conn.execute(
            "ALTER TABLE user_skill_prefs "
            "ADD COLUMN auto_apply INTEGER NOT NULL DEFAULT 0"
        )
    if "source" not in cols:
        conn.execute(
            "ALTER TABLE user_skill_prefs "
            "ADD COLUMN source TEXT NOT NULL DEFAULT ''"
        )


def _load_prefs(conn: sqlite3.Connection, user_id: str) -> dict[str, dict]:
    _ensure_schema(conn)
    rows = conn.execute(
        "SELECT skill_name, enabled, auto_apply, source, created_at "
        "FROM user_skill_prefs WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    return {
        str(r[0]): {
            "enabled":    bool(r[1]),
            "auto_apply": bool(r[2]),
            "source":     str(r[3] or ""),
            "created_at": str(r[4] or ""),
        }
        for r in rows
    }


def _upsert_pref(
    conn: sqlite3.Connection,
    user_id: str,
    skill_name: str,
    *,
    enabled: Optional[bool] = None,
    auto_apply: Optional[bool] = None,
    source: Optional[str] = None,
) -> None:
    """Insert-or-update one pref row; None fields keep their value."""
    _ensure_schema(conn)
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO user_skill_prefs
            (user_id, skill_name, enabled, auto_apply, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (user_id, skill_name) DO UPDATE SET
            enabled    = COALESCE(?, enabled),
            auto_apply = COALESCE(?, auto_apply),
            source     = COALESCE(?, source)
        """,
        (
            user_id, skill_name,
            1 if (enabled is None or enabled) else 0,
            1 if auto_apply else 0,
            source or "",
            now_iso,
            None if enabled is None else (1 if enabled else 0),
            None if auto_apply is None else (1 if auto_apply else 0),
            source,
        ),
    )
    conn.commit()


def _delete_pref(conn: sqlite3.Connection, user_id: str, skill_name: str) -> None:
    _ensure_schema(conn)
    conn.execute(
        "DELETE FROM user_skill_prefs WHERE user_id = ? AND skill_name = ?",
        (user_id, skill_name),
    )
    conn.commit()


# ── SkillManager plumbing ─────────────────────────────────────────────


def _user_twin_dir(user_id: str) -> Path:
    from nexus_server.twin_manager import TWIN_BASE_DIR
    return TWIN_BASE_DIR / user_id


def _disk_skill_manager(user_id: str):
    """Fresh SkillManager over the user's twin skills dir.

    Always reflects the FULL on-disk state — including skills the user
    disabled (which a live twin's in-memory cache deliberately drops).
    Small dir scan; cheap enough to construct per request.
    """
    from nexus_core.skills.manager import SkillManager
    return SkillManager(base_dir=_user_twin_dir(user_id))


def _live_twin_skills(user_id: str):
    """The cached DigitalTwin's SkillManager, or None when no twin is
    resident (or a test override without ``.skills`` is installed)."""
    try:
        from nexus_server import twin_manager
        sess = twin_manager._sessions.get(user_id)
        if sess is not None:
            return getattr(sess.twin, "skills", None)
    except Exception as e:  # noqa: BLE001
        logger.debug("live twin lookup failed for %s: %s", user_id, e)
    return None


def _reload_skill_into(mgr, name: str) -> bool:
    """(Re)load one on-disk skill into ``mgr``'s in-memory dict.

    Handles both layouts: folder (``{dir}/{name}/SKILL.md``) and flat
    (``{dir}/{name}.md`` — with a frontmatter-name fallback scan).
    """
    skills_dir: Path = mgr._skills_dir
    folder = skills_dir / name
    if folder.is_dir() and (folder / "SKILL.md").exists():
        skill = mgr._load_skill_folder(folder)
        mgr._skills[skill.name] = skill
        return True
    flat = skills_dir / f"{name}.md"
    if flat.is_file():
        skill = mgr._load_skill_flat(flat)
        mgr._skills[skill.name] = skill
        return True
    # Flat file whose frontmatter name differs from its stem.
    for entry in sorted(skills_dir.glob("*.md")):
        if entry.name == "SKILL.md" or entry.name.startswith("."):
            continue
        try:
            skill = mgr._load_skill_flat(entry)
        except Exception:  # noqa: BLE001
            continue
        if skill.name == name:
            mgr._skills[skill.name] = skill
            return True
    return False


def _sync_live_twin(user_id: str, name: str, enabled: bool) -> None:
    """Mirror a toggle / uninstall / install into a resident twin's
    in-memory SkillManager so the legacy /llm/chat prompt (which folds
    ``twin.skills.get_prompt_context()``) respects the pref immediately."""
    live = _live_twin_skills(user_id)
    if live is None:
        return
    try:
        if enabled:
            _reload_skill_into(live, name)
        else:
            live._skills.pop(name, None)
    except Exception as e:  # noqa: BLE001
        logger.warning("live twin skill sync failed (%s/%s): %s",
                       user_id, name, e)


def apply_disabled_overlay(twin, user_id: str) -> None:
    """Drop user-disabled skills from a freshly created twin's in-memory
    SkillManager. Called by twin_manager._create_twin so the legacy
    /llm/chat path never sees disabled skills after a cold start.
    The on-disk files are untouched — re-enabling reloads them."""
    mgr = getattr(twin, "skills", None)
    if mgr is None:
        return
    with get_db_connection() as conn:
        prefs = _load_prefs(conn, user_id)
    disabled = {n for n, p in prefs.items() if not p["enabled"]}
    for name in disabled:
        mgr._skills.pop(name, None)
    if disabled:
        logger.info(
            "skill overlay: dropped %d disabled skill(s) for %s",
            len(disabled), user_id,
        )


# ── Chat injection (v2 tiered path) ──────────────────────────────────


def build_skills_block(
    user_id: str, requested: Optional[list[str]] = None,
) -> tuple[str, list[str]]:
    """Compose the ``## Skill: {name}`` prompt sections for one v2 turn.

    Included skills = (installed ∩ enabled ∩ requested)
                    ∪ (installed ∩ enabled ∩ auto_apply).
    Requested names that aren't installed+enabled are silently dropped.

    Returns ``(block_text, applied_names)`` — block_text is ``""`` when
    nothing applies.
    """
    requested_set = {str(n) for n in (requested or []) if str(n).strip()}
    mgr = _disk_skill_manager(user_id)
    with get_db_connection() as conn:
        prefs = _load_prefs(conn, user_id)

    sections: list[str] = []
    applied: list[str] = []
    for skill in mgr.installed:
        p = prefs.get(skill.name)
        enabled = p["enabled"] if p else True
        auto = p["auto_apply"] if p else False
        if not enabled:
            continue
        if skill.name not in requested_set and not auto:
            continue
        body = (skill.instructions or "").strip()
        header = f"## Skill: {skill.name}"
        desc = (skill.description or "").strip()
        section = header
        if desc:
            section += f"\n{desc}"
        if body:
            section += f"\n\n{body}"
        sections.append(section)
        applied.append(skill.name)

    if not sections:
        return "", []
    block = (
        "ACTIVE SKILLS — the user has activated the following skill(s) "
        "for this conversation. Follow each skill's instructions when "
        "they apply to the request:\n\n"
        + "\n\n".join(sections)
    )
    return block, applied


# ── Request/response models ──────────────────────────────────────────


class InstallRequest(BaseModel):
    identifier: str


class ToggleRequest(BaseModel):
    enabled: bool
    auto_apply: Optional[bool] = None


# ── Offline curated catalog (source=official fallback) ───────────────
#
# Static snapshot of the canonical anthropics/skills document-creation
# set (the /skills folder as of 2026-07). Served when the live GitHub
# listing is unreachable — 国内 (GFW) / offline deployments — so the
# 发现 tab still surfaces the flagship skills instead of a bare 502.
# Search works offline; INSTALL still needs network (or a
# NEXUS_GITHUB_MIRROR mirror — see the SDK skills manager docstring).
_OFFICIAL_FALLBACK_CATALOG: list[dict] = [
    {
        "identifier": "anthropic:docx",
        "name": "docx",
        "description": ("Create, edit, and analyze Word documents "
                        "(.docx) — formatting, tracked changes, "
                        "document generation."),
    },
    {
        "identifier": "anthropic:pdf",
        "name": "pdf",
        "description": ("Read, create, merge, split, and fill PDF "
                        "files; extract text and tables."),
    },
    {
        "identifier": "anthropic:pptx",
        "name": "pptx",
        "description": ("Create and edit PowerPoint presentations "
                        "(.pptx) — slides, layouts, speaker notes."),
    },
    {
        "identifier": "anthropic:xlsx",
        "name": "xlsx",
        "description": ("Create, edit, and analyze Excel spreadsheets "
                        "(.xlsx) with formulas and charts."),
    },
]


def _fallback_official_results(q: str, installed_names: set[str]) -> list[dict]:
    """Filter the static catalog by ``q`` (case-insensitive substring
    over identifier + name + description; empty q = everything) and
    project to the search response shape. Each row is flagged
    ``cached: True`` so the client can show an 离线目录 badge."""
    ql = (q or "").strip().lower()
    out: list[dict] = []
    for row in _OFFICIAL_FALLBACK_CATALOG:
        haystack = " ".join(
            (row["identifier"], row["name"], row["description"])
        ).lower()
        if ql and ql not in haystack:
            continue
        out.append({
            "identifier":  row["identifier"],
            "name":        row["name"],
            "description": row["description"],
            "source":      "official",
            "installed":   _identifier_to_name(row["identifier"])
                           in installed_names,
            "cached":      True,
        })
    return out


def _is_network_error(exc: BaseException) -> bool:
    """Best-effort 'GitHub is unreachable' classifier for install
    failures. Walks the exception chain: OS-level errors (URLError,
    socket, TLS, proxy-tunnel failures are all OSError subclasses)
    count as network — EXCEPT HTTPError, which means the remote
    answered (404 missing skill / 403 rate-limit ≠ blocked network)
    unless it's a gateway-side 5xx. Falls back to message keywords for
    wrapped RuntimeErrors."""
    import urllib.error

    seen: set[int] = set()
    e: Optional[BaseException] = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, urllib.error.HTTPError):
            return e.code in (502, 503, 504)
        if isinstance(e, (OSError, TimeoutError)):
            return True
        msg = str(e).lower()
        if any(hint in msg for hint in (
            "tunnel connection failed", "connection refused",
            "connection reset", "connection aborted", "timed out",
            "timeout", "unreachable", "proxy", "getaddrinfo",
            "name resolution", "temporary failure", "ssl",
            "eof occurred", "remote end closed", "network",
        )):
            return True
        e = e.__cause__ or e.__context__
    return False


def _identifier_to_name(identifier: str) -> str:
    """Best-effort skill name from an install identifier — used only
    for the pre-install duplicate check. The authoritative name comes
    from the installed skill's frontmatter."""
    s = identifier.strip().rstrip("/")
    if ":" in s and not s.startswith(("http://", "https://")):
        s = s.split(":", 1)[1]
    s = s.split("/")[-1]
    if s.endswith(".md"):
        s = s[:-3]
    return s


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get("")
async def list_skills(current_user: str = Depends(get_current_user)):
    """Installed skills + per-user enabled/auto_apply overlay."""
    mgr = _disk_skill_manager(current_user)
    with get_db_connection() as conn:
        prefs = _load_prefs(conn, current_user)

    out = []
    for skill in mgr.installed:
        p = prefs.get(skill.name)
        installed_at = (p or {}).get("created_at") or ""
        if not installed_at:
            try:
                installed_at = datetime.fromtimestamp(
                    skill.path.stat().st_mtime, tz=timezone.utc,
                ).isoformat()
            except OSError:
                installed_at = ""
        out.append({
            "name":         skill.name,
            "description":  skill.description,
            "source":       (p or {}).get("source") or "local",
            "enabled":      p["enabled"] if p else True,
            "auto_apply":   p["auto_apply"] if p else False,
            "installed_at": installed_at,
            "invocable":    bool((skill.instructions or "").strip()),
        })
    return {"skills": out}


@router.get("/search")
async def search_skills(
    q: str = "",
    source: str = "official",
    current_user: str = Depends(get_current_user),
):
    """Marketplace search proxy. ``source=official`` hits the canonical
    anthropics/skills catalog; ``source=github`` searches repos tagged
    with the ``claude-skills`` topic.

    Network failure: ``source=official`` falls back to the built-in
    static catalog (rows carry ``cached: true``) so the Discover tab
    keeps working behind the GFW / offline; ``source=github`` has no
    offline equivalent → 502 ``search_unavailable`` (never a raw 500).
    """
    if source not in ("official", "github"):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_source",
                    "message": "source must be 'official' or 'github'"},
        )
    mgr = _disk_skill_manager(current_user)
    installed_names = set(mgr.names)

    try:
        if source == "official":
            raw = await mgr.search_anthropic_official(q, limit=20)
            # search_anthropic_official swallows listing failures and
            # returns [] — distinguish "no match" from "GitHub is down"
            # via the listing cache the call populates on success.
            if not raw and getattr(mgr, "_anthropic_listing_cache", None) is None:
                raise RuntimeError("anthropic skills listing unreachable")
        else:
            raw = await mgr.search_github_topic(q, limit=20)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("skill search failed (source=%s q=%r): %s", source, q, e)
        if source == "official":
            # GitHub unreachable → serve the built-in snapshot instead
            # of a 502. Install still needs network / a mirror.
            logger.info(
                "official skill search unreachable — serving the "
                "built-in cached catalog",
            )
            return {"results": _fallback_official_results(q, installed_names)}
        raise HTTPException(
            status_code=502,
            detail={"code": "search_unavailable",
                    "message": f"skill search backend unreachable: {e}"},
        )

    results = []
    for row in raw or []:
        identifier = str(row.get("identifier") or "")
        if not identifier:
            continue
        results.append({
            "identifier":  identifier,
            "name":        str(row.get("name") or _identifier_to_name(identifier)),
            "description": str(row.get("description") or ""),
            "source":      source,
            "installed":   _identifier_to_name(identifier) in installed_names,
        })
    return {"results": results}


@router.post("/install")
async def install_skill(
    req: InstallRequest,
    current_user: str = Depends(get_current_user),
):
    """Install a skill into the user's twin skills dir via SkillManager."""
    identifier = (req.identifier or "").strip()
    if not identifier:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_identifier",
                    "message": "identifier must be non-empty"},
        )

    mgr = _disk_skill_manager(current_user)
    candidate = _identifier_to_name(identifier)
    if candidate in set(mgr.names):
        raise HTTPException(
            status_code=409,
            detail={"code": "already_installed",
                    "message": f"skill '{candidate}' is already installed"},
        )

    try:
        # install_pack handles every identifier shape install() does;
        # for repo-ROOT URLs ("skill pack" repos from the github-topic
        # search) it installs EVERY discovered skill dir instead of
        # bailing with a "multi-skill repo, pick one" error.
        skills = await mgr.install_pack(identifier)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_identifier", "message": str(e)},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("skill install failed (%r): %s", identifier, e)
        if _is_network_error(e):
            # GitHub blocked (GFW) / offline — unlike search there is
            # no offline fallback for install, so point the user at
            # the mirror setting.
            raise HTTPException(
                status_code=502,
                detail={"code": "install_network",
                        "message": ("GitHub 无法访问——请在设置的 .env 中"
                                    "配置 NEXUS_GITHUB_MIRROR 镜像后重试")},
            )
        raise HTTPException(
            status_code=502,
            detail={"code": "install_failed", "message": str(e)[:300]},
        )

    if not skills:
        # install_pack raises rather than returning [] — defensive.
        raise HTTPException(
            status_code=502,
            detail={"code": "install_failed",
                    "message": "installer returned no skills"},
        )

    # Dedupe by skill name — a pack repo can resolve two dirs to the
    # same frontmatter name (or re-install an already-present skill);
    # neither is an error for pack installs, the last write wins on
    # disk and we report the name once.
    unique: list = []
    seen: set[str] = set()
    for s in skills:
        if s.name in seen:
            continue
        seen.add(s.name)
        unique.append(s)

    if identifier.startswith("anthropic:") or "github.com/anthropics/" in identifier:
        src = "official"
    elif "github.com" in identifier or "/" in identifier:
        src = "github"
    else:
        src = "official"  # bare names resolve against anthropics/skills
    with get_db_connection() as conn:
        for s in unique:
            _upsert_pref(conn, current_user, s.name,
                         enabled=True, source=src)

    # A resident twin picks the new skills up immediately (legacy path).
    for s in unique:
        _sync_live_twin(current_user, s.name, enabled=True)

    payload = [{"name": s.name, "description": s.description}
               for s in unique]
    return {"ok": True,
            "skills": payload,
            "count": len(payload),
            # Backward-compat: single-skill callers keep reading .skill.
            "skill": payload[0]}


@router.delete("/{name}")
async def uninstall_skill(
    name: str,
    current_user: str = Depends(get_current_user),
):
    """Uninstall: SkillManager remove when it knows the skill, otherwise
    defensive on-disk cleanup (folder AND flat layouts)."""
    if not _name_is_safe(name):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_name", "message": "invalid skill name"},
        )
    mgr = _disk_skill_manager(current_user)
    removed = False
    try:
        removed = mgr.uninstall(name)
    except Exception as e:  # noqa: BLE001
        # e.g. shutil.rmtree on a flat-layout FILE — fall through to the
        # defensive path below.
        logger.debug("SkillManager.uninstall(%s) raised: %s", name, e)
        mgr._skills.pop(name, None)

    skills_dir = _user_twin_dir(current_user) / "skills"
    folder = skills_dir / name
    flat = skills_dir / f"{name}.md"
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
        removed = True
    if flat.is_file():
        try:
            flat.unlink()
            removed = True
        except OSError as e:
            logger.warning("flat skill unlink failed (%s): %s", flat, e)

    if not removed:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_installed",
                    "message": f"no installed skill named '{name}'"},
        )

    with get_db_connection() as conn:
        _delete_pref(conn, current_user, name)
    _sync_live_twin(current_user, name, enabled=False)
    return {"ok": True}


@router.post("/{name}/toggle")
async def toggle_skill(
    name: str,
    req: ToggleRequest,
    current_user: str = Depends(get_current_user),
):
    """Persist enabled (and optionally auto_apply) for one skill."""
    if not _name_is_safe(name):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_name", "message": "invalid skill name"},
        )
    mgr = _disk_skill_manager(current_user)
    if name not in set(mgr.names):
        raise HTTPException(
            status_code=404,
            detail={"code": "not_installed",
                    "message": f"no installed skill named '{name}'"},
        )
    with get_db_connection() as conn:
        _upsert_pref(conn, current_user, name,
                     enabled=req.enabled, auto_apply=req.auto_apply)
        prefs = _load_prefs(conn, current_user)
    _sync_live_twin(current_user, name, enabled=req.enabled)
    p = prefs.get(name, {})
    return {"ok": True,
            "enabled": bool(p.get("enabled", req.enabled)),
            "auto_apply": bool(p.get("auto_apply", False))}
