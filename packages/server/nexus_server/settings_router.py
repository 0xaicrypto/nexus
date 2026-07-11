"""
Settings · LLM endpoints — runtime LLM provider/key configuration.

Why this exists
───────────────
The legacy v1 desktop (Avalonia / .NET, removed — see git tag
``legacy/avalonia-final``) had no in-app key UI; it depended on a
bash setup.sh + start.sh to seed and ``export`` keys from
``~/Library/Application Support/RuneProtocol/.env`` before spawning
the backend.

The v2 Tauri spawn reads the same .env at boot (see
``packages/desktop-v2/src-tauri/src/lib.rs::load_user_env``) so users
migrating from v1 get parity. But there is no way to ADD a key without
restarting the app — and a fresh install has no .env at all. This
router fills both gaps:

  GET  /api/v1/settings/llm
      → reports which provider is active, which keys are populated
        (booleans only — keys themselves are never returned), and the
        on-disk .env path for transparency.

  PUT  /api/v1/settings/llm
      → accepts a provider/model + any subset of GEMINI_API_KEY,
        OPENAI_API_KEY, ANTHROPIC_API_KEY values, writes them to
        $RUNE_HOME/.env, AND mutates the in-process config singleton
        so the next chat turn picks them up without a restart.

Per docs/design/m3-memory-architecture.md the chat path can run without
an LLM key (T1/T2 are templated/SQL-only), but T3 reasoning, Twin
memory extraction, embeddings, and Quick scan all require Gemini or
Anthropic. This is the only place that surfaces "you haven't set a key"
to the medic instead of letting it 500 deep in the stack.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from nexus_server.auth import get_current_user
from nexus_server.config import get_config

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


# Keys we let the desktop set. Intentionally narrow — billing /
# rate-limit settings are not surfaced (they're deploy-level,
# not per-medic). DEFAULT_LLM_* are settable so the desktop can switch
# provider in one round-trip.
ALLOWED_KEYS = {
    "DEFAULT_LLM_PROVIDER",
    "DEFAULT_LLM_MODEL",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "KIMI_API_KEY",
    # T4 web-grounded retrieval. Lives here (not under a separate
    # /settings/web endpoint) because the medic perceives "API keys
    # for the AI integrations" as one config surface. Tavily key
    # missing → T4 silently degrades to T3.
    "TAVILY_API_KEY",
    # Optional override of web_search.DEFAULT_CLINICAL_DOMAINS. Comma
    # list, lowercased. The literal "NONE" disables the allow-list.
    "NEXUS_WEB_ALLOWED_DOMAINS",
}


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _rune_home() -> Path:
    """Resolve $RUNE_HOME (v1 parity) or fall back to the macOS default.

    The Tauri sidecar sets RUNE_HOME explicitly in lib.rs::spawn_backend_sidecar
    so this is always set in the bundled .app. The fallback is for `uvicorn`
    runs and pytest where the user-level path makes sense anyway.
    """
    env = os.environ.get("RUNE_HOME")
    if env:
        return Path(env)
    return Path(os.path.expanduser("~/Library/Application Support/RuneProtocol"))


def _env_file() -> Path:
    return _rune_home() / ".env"


# ─────────────────────────────────────────────────────────────────────
# DB-backed persistence (the new source of truth)
# ─────────────────────────────────────────────────────────────────────
#
# `user_settings` lives in rune_server.db and survives:
#   - re-install of Nexus.app (db is in user data dir, not the bundle)
#   - upgrade of Nexus.app (same)
#   - accidental `rm ~/Library/.../RuneProtocol/.env` (only the .env
#     file is at risk; the DB row stays)
#
# `.env` is still written for backward compat with the Tauri sidecar's
# launch-time env loader (`lib.rs::load_user_env`); keys land in
# `os.environ` either way. On startup, ``hydrate_env_from_db()`` writes
# DB values into os.environ + ServerConfig so the new build picks up
# previously-saved keys even if the .env file was lost.

_KEYS_PERSISTED = (
    "DEFAULT_LLM_PROVIDER",
    "DEFAULT_LLM_MODEL",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "KIMI_API_KEY",
)


def _db_set(user_id: str, key: str, value: str) -> None:
    """Upsert one user_settings row. Empty value deletes the row."""
    from nexus_server.database import get_db_connection
    now_ms = int(time.time() * 1000)
    with get_db_connection() as conn:
        if value:
            conn.execute(
                "INSERT INTO user_settings (user_id, key, value, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET "
                "  value = excluded.value, updated_at = excluded.updated_at",
                (user_id, key, value, now_ms),
            )
        else:
            conn.execute(
                "DELETE FROM user_settings WHERE user_id = ? AND key = ?",
                (user_id, key),
            )
        conn.commit()


def _db_get_all(user_id: str = "_global") -> dict[str, str]:
    """Return all settings for the given user_id (defaults to operator-
    global). Missing rows produce an empty dict rather than raising."""
    from nexus_server.database import get_db_connection
    try:
        with get_db_connection() as conn:
            rows = conn.execute(
                "SELECT key, value FROM user_settings WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception as exc:  # noqa: BLE001
        # Table may not exist on a brand-new DB before init_db runs in a
        # particular code path — degrade silently.
        logger.debug("user_settings read failed: %s", exc)
        return {}


# Module-level record of which keys hydrate filled from DB. Lets
# GET /settings/llm report active_key_source without re-querying the
# DB on every poll (and without leaking the DB contents). Mutated by
# hydrate_env_from_db at boot + _apply_to_running_config on save.
#   - "db"  → DB row wrote into os.environ at boot
#   - "env" → key was already in os.environ (Tauri sidecar's .env
#             loader, shell exec, or operator hand-edit)
#   - absent → no key (key is None / empty)
_KEY_SOURCE: dict[str, str] = {}


def hydrate_env_from_db() -> None:
    """At server boot, copy DB-persisted settings into ``os.environ``
    + ``ServerConfig`` so the rest of the process sees them — exactly
    as if they'd been loaded from .env.

    Precedence policy (changed in F17):
      DB-saved value WINS over .env / shell.

    Why: the bundled default.env can carry stale or shared keys
    (e.g. a previous build's default GEMINI_API_KEY). On reinstall,
    Tauri's ``seed_or_merge_user_env`` adds keys to the user .env
    only if missing — but if the user previously HAD set a key via
    Settings, it lives in both .env AND DB. The .env on disk might
    have been overwritten by ``_write_env`` correctly, but a future
    bundled value with a non-empty default could sneak in via the
    sidecar's launch loader and shadow the user's actual saved key.

    Making DB authoritative removes that whole class of foot-gun:
    whatever the medic last clicked "Save" on in Settings · LLM is
    what every chat turn uses, regardless of what's in any file.

    Hand-edit escape hatch: if an operator REALLY wants .env to win
    (rare, dev-only), they can delete the row from user_settings
    via sqlite3 — then hydrate has nothing to inject and env stays.
    """
    db = _db_get_all("_global")
    # Pre-pass: anything that's ALREADY in env was loaded from .env /
    # shell at sidecar spawn — tag it ``env`` for the diagnostic UI.
    # We'll flip this to "db" below for any key DB has.
    for k in _KEYS_PERSISTED:
        existing = os.environ.get(k)
        if existing:
            _KEY_SOURCE[k] = "env"

    if not db:
        logger.info(
            "hydrate_env_from_db: user_settings DB has 0 entries — using "
            "whatever .env / shell already provided (sources: %s)",
            {k: v for k, v in _KEY_SOURCE.items() if k.endswith("_API_KEY")},
        )
        return
    from nexus_server.config import ServerConfig
    filled: list[str] = []
    overrode: list[str] = []
    for k in _KEYS_PERSISTED:
        v = db.get(k, "")
        if not v:
            continue
        existing = os.environ.get(k)
        if existing and existing != v:
            # DB value differs from what .env / shell loaded. DB wins
            # (per the policy doc above) — overwrite and remember
            # this so we can log the change without leaking values.
            overrode.append(k)
        os.environ[k] = v
        if hasattr(ServerConfig, k):
            try:
                setattr(ServerConfig, k, v)
            except Exception as e:  # noqa: BLE001
                logger.debug("setting ServerConfig.%s failed: %s", k, e)
        filled.append(k)
        _KEY_SOURCE[k] = "db"
    logger.info(
        "hydrate_env_from_db: filled %d key(s) from DB (%s); "
        "overrode %d different .env values (%s); DB had %d total entries.",
        len(filled), ",".join(filled) or "—",
        len(overrode), ",".join(overrode) or "—",
        len(db),
    )


def _read_env() -> dict[str, str]:
    """Parse $RUNE_HOME/.env into a flat dict. Same rules as v1's start.sh:
    skip blanks + ``#`` comments, split on first ``=``, strip one pair of
    surrounding quotes from values."""
    path = _env_file()
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip("\r\n")
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if not k:
            continue
        v = v.strip()
        if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
            v = v[1:-1]
        out[k] = v
    return out


_LINE_RE_TMPL = r"^[ \t]*{key}=.*$"


def _write_env(updates: dict[str, str]) -> Path:
    """Idempotent merge of ``updates`` into $RUNE_HOME/.env.

    Strategy: read all lines, replace any existing assignment of a
    target key in place, append any keys not already present.  We do
    NOT touch unrelated lines so user comments and ordering survive.
    Atomic via tempfile + rename so a crash mid-write can't truncate
    the file the next launch needs to load.
    """
    path = _env_file()
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    remaining = dict(updates)  # keys we still need to write

    new_lines: list[str] = []
    for line in existing_lines:
        replaced = False
        for k in list(remaining.keys()):
            if re.match(_LINE_RE_TMPL.format(key=re.escape(k)), line):
                new_lines.append(f"{k}={remaining.pop(k)}")
                replaced = True
                break
        if not replaced:
            new_lines.append(line)

    if remaining:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append("# ── Settings · LLM (written via /api/v1/settings/llm) ──")
        for k, v in remaining.items():
            new_lines.append(f"{k}={v}")

    # Atomic write.
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False,
        dir=str(path.parent), prefix=".env.", suffix=".tmp",
    )
    try:
        tmp.write("\n".join(new_lines))
        if new_lines:
            tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()
    os.replace(tmp.name, path)
    # Tighten perms — file holds API keys.
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger.debug("chmod settings file failed: %s", e)
    return path


def _apply_to_running_config(updates: dict[str, str]) -> None:
    """Mutate the in-process config so the very next LLM call sees the
    new key without a restart.

    Subtle: ``get_config()`` returns a fresh ``ServerConfig()`` instance
    on every call, but the keys are CLASS attributes populated at import
    time from ``os.environ``. So patching an instance is a no-op for
    every other call site; we must patch the class itself. We also
    mirror to ``os.environ`` for any code that re-reads env hot (e.g.
    twin_manager.create_twin reads ``os.environ.get`` directly)."""
    from nexus_server.config import ServerConfig
    for k, v in updates.items():
        os.environ[k] = v
        if hasattr(ServerConfig, k):
            try:
                setattr(ServerConfig, k, v)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "could not set ServerConfig.%s in-process; "
                    "will pick up on next boot", k,
                )
        # Mark as DB-sourced — the medic just saved it. GET
        # /settings/llm reports active_key_source="db" so the UI can
        # confirm "✓ key loaded from your saved settings".
        if k in _KEYS_PERSISTED:
            _KEY_SOURCE[k] = "db"


def _mask_key(key: Optional[str]) -> str:
    """Show first 6 + last 4 with a fixed-width mask in the middle.
    Empty / None returns ''. Used by the diagnostic UI so the medic
    can confirm "yes that's the key I expect" without us echoing the
    full secret back. AIzaSyA0bCdEfGhIjKlMn → AIzaSy••••••••KlMn."""
    if not key:
        return ""
    if len(key) <= 12:
        # Too short to safely partial-mask; show 1 char each side.
        return key[:1] + "••••" + key[-1:]
    return key[:6] + "••••••••" + key[-4:]


# ─────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────


class LlmStatusResponse(BaseModel):
    provider: str
    model: str
    env_file_path: str
    env_file_exists: bool
    has_gemini_key: bool
    has_openai_key: bool
    has_anthropic_key: bool
    has_kimi_key: bool = False
    # Free-form note rendered under the form — e.g. tells the user the
    # active provider has no key configured.
    advisory: Optional[str] = None
    # Diagnostic: where the ACTIVE provider's key came from. One of
    # "db" (loaded from user_settings table at boot), "env" (loaded
    # from .env / shell, hydrate-from-db was a no-op), "none" (no key
    # available). Lets the medic answer "did my saved key load?"
    # without having to grep server logs.
    active_key_source: Optional[str] = None
    # First 6 + last 4 of the active provider's key, with the middle
    # masked. Empty if no key. The user reads this to confirm "yes
    # that's the key I expect" — e.g. ``AIzaSy••••••••2gJk``. Server
    # never returns the full key.
    active_key_preview: Optional[str] = None
    active_key_length: Optional[int] = None


class LlmUpdateRequest(BaseModel):
    provider: Optional[str] = Field(
        default=None, description="One of: gemini | openai | anthropic | kimi",
    )
    model: Optional[str] = Field(default=None)
    gemini_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    kimi_api_key: Optional[str] = None


class LlmUpdateResponse(BaseModel):
    ok: bool
    env_file_path: str
    written_keys: list[str]
    status: LlmStatusResponse


class LlmTestResponse(BaseModel):
    """Result of POST /api/v1/settings/llm/test — actually attempts a
    tiny LLM call with the in-process key so the medic can confirm
    "yes my saved key works" or see the exact upstream error."""
    ok: bool
    provider: str
    model: str
    # Round-trip latency in ms (only set on success). Sub-1s ⇒ key OK,
    # > 8s ⇒ likely network / quota throttle but key was accepted.
    latency_ms: Optional[int] = None
    # Trimmed upstream error message on failure. Example:
    # ``400 INVALID_ARGUMENT: API key not valid. Please pass a valid
    # API key.``  Verbatim from the LLM SDK; we don't paraphrase
    # because the wording is the diagnostic.
    error: Optional[str] = None
    # Short diagnosis the UI can render as a hint:
    #   - "key_missing"       → no key at all (set advisory to point Settings · LLM)
    #   - "key_invalid"       → upstream said the key is invalid (revoked / typo / wrong project)
    #   - "quota_exceeded"    → 429 / RESOURCE_EXHAUSTED
    #   - "network"           → couldn't reach the API
    #   - "other"             → something else (timeout / 5xx / json parse)
    diagnosis: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────


def _make_status() -> LlmStatusResponse:
    cfg = get_config()
    path = _env_file()
    provider = cfg.DEFAULT_LLM_PROVIDER
    has_gemini    = bool(cfg.GEMINI_API_KEY)
    has_openai    = bool(cfg.OPENAI_API_KEY)
    has_anthropic = bool(cfg.ANTHROPIC_API_KEY)
    has_kimi      = bool(cfg.KIMI_API_KEY)
    advisory: Optional[str] = None
    if provider == "gemini" and not has_gemini:
        advisory = "Active provider is Gemini but GEMINI_API_KEY is not set."
    elif provider == "openai" and not has_openai:
        advisory = "Active provider is OpenAI but OPENAI_API_KEY is not set."
    elif provider == "anthropic" and not has_anthropic:
        advisory = "Active provider is Anthropic but ANTHROPIC_API_KEY is not set."
    elif provider == "kimi" and not has_kimi:
        advisory = "Active provider is Kimi but KIMI_API_KEY is not set."

    # Map the active provider to the env-var name that holds its key,
    # then pull the runtime value + source for the diagnostic block.
    _provider_key_var = {
        "gemini":    "GEMINI_API_KEY",
        "openai":    "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "kimi":      "KIMI_API_KEY",
    }.get(provider)
    active_key = getattr(cfg, _provider_key_var) if _provider_key_var else None
    active_source: Optional[str] = None
    if active_key:
        # Check our boot-time tracker first; if absent, peek at env to
        # tell .env-from-shell apart from a manually-injected runtime
        # value (rare but possible during tests).
        active_source = _KEY_SOURCE.get(_provider_key_var or "")
        if not active_source:
            active_source = "env" if os.environ.get(_provider_key_var or "") else "none"
    else:
        active_source = "none"

    return LlmStatusResponse(
        provider=provider,
        model=cfg.DEFAULT_LLM_MODEL,
        env_file_path=str(path),
        env_file_exists=path.exists(),
        has_gemini_key=has_gemini,
        has_openai_key=has_openai,
        has_anthropic_key=has_anthropic,
        has_kimi_key=has_kimi,
        advisory=advisory,
        active_key_source=active_source,
        active_key_preview=_mask_key(active_key) if active_key else "",
        active_key_length=len(active_key) if active_key else 0,
    )


@router.get("/llm", response_model=LlmStatusResponse)
async def get_llm_settings(_: str = Depends(get_current_user)):
    return _make_status()


@router.put("/llm", response_model=LlmUpdateResponse)
async def put_llm_settings(
    body: LlmUpdateRequest,
    _: str = Depends(get_current_user),
):
    """Persist any subset of LLM provider/key settings to $RUNE_HOME/.env
    AND mutate the in-process config so the next chat turn uses them.
    Returns the new status (booleans only — keys themselves never leave
    the server)."""
    updates: dict[str, str] = {}
    if body.provider is not None:
        p = body.provider.strip().lower()
        if p not in {"gemini", "openai", "anthropic", "kimi"}:
            raise HTTPException(status_code=400, detail=f"unknown provider: {p}")
        updates["DEFAULT_LLM_PROVIDER"] = p
    if body.model is not None and body.model.strip():
        updates["DEFAULT_LLM_MODEL"] = body.model.strip()
    if body.gemini_api_key is not None and body.gemini_api_key.strip():
        updates["GEMINI_API_KEY"] = body.gemini_api_key.strip()
    if body.openai_api_key is not None and body.openai_api_key.strip():
        updates["OPENAI_API_KEY"] = body.openai_api_key.strip()
    if body.anthropic_api_key is not None and body.anthropic_api_key.strip():
        updates["ANTHROPIC_API_KEY"] = body.anthropic_api_key.strip()
    if body.kimi_api_key is not None and body.kimi_api_key.strip():
        updates["KIMI_API_KEY"] = body.kimi_api_key.strip()

    if not updates:
        raise HTTPException(status_code=400, detail="no settings provided")

    # ── Persist (DB first, .env second, in-process third) ──────────
    # DB is the source of truth — survives reinstall / upgrade /
    # accidental .env wipe. .env is still written for backward compat
    # with the Tauri sidecar's launch-time loader (lib.rs::load_user_env).
    # If DB write fails, we still try .env so the medic doesn't lose
    # their keys completely; if .env write fails too, we give up.
    db_errors: list[str] = []
    for k, v in updates.items():
        try:
            _db_set("_global", k, v)
        except Exception as exc:  # noqa: BLE001
            db_errors.append(f"{k}: {exc}")
            logger.warning("user_settings DB write failed for %s: %s", k, exc)
    if db_errors:
        logger.warning(
            "user_settings: %d key(s) didn't persist to DB; relying on .env "
            "+ in-process only: %s",
            len(db_errors), db_errors,
        )

    try:
        path = _write_env(updates)
    except OSError as exc:
        logger.exception("write_env failed")
        # Only raise if DB ALSO failed — otherwise the medic's keys are
        # safe in the DB even if .env couldn't be written, and the next
        # ``hydrate_env_from_db()`` on boot will recover the in-process
        # config.
        if db_errors:
            raise HTTPException(
                status_code=500,
                detail=f"failed to write .env AND DB: {exc} / {db_errors}",
            ) from exc
        # DB succeeded; .env didn't — log + continue with a fake path.
        path = _env_file()

    _apply_to_running_config(updates)

    return LlmUpdateResponse(
        ok=True,
        env_file_path=str(path),
        written_keys=sorted(updates.keys()),
        status=_make_status(),
    )


@router.post("/llm/test", response_model=LlmTestResponse)
async def test_llm_key(_: str = Depends(get_current_user)):
    """Live-test the in-process active provider key.

    Sends a tiny generation request (system="ping", user="ok",
    max_tokens=4) and reports either ✓ success with latency or the
    verbatim upstream error. This is the canonical answer to "does
    my saved key actually work?" — checking just the boolean
    has_*_key only tells you a key exists, not whether Google/OpenAI/
    Anthropic accept it.

    The result also classifies the failure (key_missing / key_invalid /
    quota_exceeded / network / other) so the UI can colour-code and
    suggest the right remediation.
    """
    import time as _time
    cfg = get_config()
    provider = cfg.DEFAULT_LLM_PROVIDER
    model = cfg.DEFAULT_LLM_MODEL

    # Cheap fast-path: no key at all → don't bother making the call.
    active_key = {
        "gemini":    cfg.GEMINI_API_KEY,
        "openai":    cfg.OPENAI_API_KEY,
        "anthropic": cfg.ANTHROPIC_API_KEY,
        "kimi":      cfg.KIMI_API_KEY,
    }.get(provider)
    if not active_key:
        return LlmTestResponse(
            ok=False, provider=provider, model=model,
            error=f"No {provider.upper()}_API_KEY configured.",
            diagnosis="key_missing",
        )

    t0 = _time.monotonic()
    try:
        from nexus_server import llm_gateway
        content, _model, _stop, _tools = await llm_gateway.call_llm(
            messages=[{"role": "user", "content": "ok"}],
            system_prompt="Reply with the single word: pong",
            model=None,
            temperature=0.0,
            max_tokens=4,
            tools=None,
        )
        latency = int((_time.monotonic() - t0) * 1000)
        # Successfully reached the LLM; even an empty response means
        # the key was accepted. Surface latency so the medic can spot
        # network slowness.
        return LlmTestResponse(
            ok=True, provider=provider, model=model,
            latency_ms=latency,
        )
    except Exception as exc:  # noqa: BLE001
        latency = int((_time.monotonic() - t0) * 1000)
        msg = str(exc)
        msg_lower = msg.lower()
        # Heuristic classification — the upstream SDK error text is
        # pretty consistent (we sampled gemini, openai, anthropic):
        if "api key not valid" in msg_lower or "api_key_invalid" in msg_lower \
                or "invalid api key" in msg_lower or "incorrect api key" in msg_lower \
                or "invalid_authentication" in msg_lower or "401" in msg_lower:
            diag = "key_invalid"
        elif "quota" in msg_lower or "resource_exhausted" in msg_lower \
                or "rate limit" in msg_lower or "429" in msg_lower:
            diag = "quota_exceeded"
        elif "name resolution" in msg_lower or "connection" in msg_lower \
                or "timed out" in msg_lower or "unreachable" in msg_lower:
            diag = "network"
        else:
            diag = "other"
        logger.warning(
            "Settings · LLM test failed (provider=%s model=%s diagnosis=%s "
            "latency_ms=%d): %s",
            provider, model, diag, latency, msg[:300],
        )
        return LlmTestResponse(
            ok=False, provider=provider, model=model,
            latency_ms=latency,
            error=msg[:500],
            diagnosis=diag,
        )
