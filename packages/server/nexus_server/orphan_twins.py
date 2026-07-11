"""Orphan twin recovery — #105 follow-up to #101.

Users on the local desktop sometimes end up with multiple user_ids on
the same machine — typically because the legacy login UI defaulted to
"Create Account" instead of "Sign In" (fixed in #101), so a returning
user accidentally registered a fresh account and their old chat
history became invisible (it's still on disk under
``~/.nexus_server/twins/<old_user_id>/``, just not addressable from
the new user_id).

This module surfaces those orphan twin DBs to the current
authenticated user and provides a one-click merge that copies the
orphan's events into the current user's event log.

Privacy
=======
Orphan discovery touches the filesystem outside the calling user's
twin dir. In single-tenant local-desktop mode this is fine — only one
human uses the machine. In hosted multi-tenant deployments it would
leak across users, so the endpoints are gated on
``NEXUS_ALLOW_ORPHAN_RECOVERY=1``. ``start.sh`` sets that for the
.dmg install path; the production hosted config does not.

Public surface
==============
* :func:`list_orphan_twins(current_user_id)` — scan the twin base dir
  and return summary rows (user_id, event_count, last_active,
  session_count) for every twin DB that is NOT the current user's.
* :func:`merge_orphan_into(orphan_user_id, current_user_id)` — copy
  ALL events from ``orphan_user_id``'s log into ``current_user_id``'s
  log, with fresh idx values, preserving session_id + content +
  metadata. Returns the number of events merged. After this call the
  orphan twin's directory still exists on disk; the caller can delete
  it via the rm helper below.
* :func:`rm_orphan_twin(orphan_user_id)` — delete the orphan twin
  directory after a successful merge.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _twin_base_dir() -> Path:
    """Match twin_event_log._twin_base_dir."""
    return Path(
        os.environ.get(
            "NEXUS_TWIN_BASE_DIR",
            os.path.expanduser("~/.nexus_server/twins"),
        )
    )


def _agent_id_for(user_id: str) -> str:
    """Match twin_event_log._agent_id_for."""
    return f"user-{user_id[:8]}"


def _db_path_for(user_id: str) -> Path:
    return (
        _twin_base_dir() / user_id / "event_log" / f"{_agent_id_for(user_id)}.db"
    )


def is_enabled() -> bool:
    """Privacy gate — orphan recovery only allowed in single-tenant mode."""
    return os.environ.get("NEXUS_ALLOW_ORPHAN_RECOVERY", "").strip() == "1"


def list_orphan_twins(current_user_id: str) -> list[dict]:
    """Walk the twin base dir and return summaries of every user_id
    directory that ISN'T the current user. Empty / corrupt entries
    are skipped silently."""
    base = _twin_base_dir()
    if not base.exists():
        return []
    out: list[dict] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name == current_user_id:
            continue
        db_path = entry / "event_log" / f"{_agent_id_for(entry.name)}.db"
        if not db_path.exists():
            continue
        try:
            summary = _summarise_twin_db(db_path)
        except Exception as e:  # noqa: BLE001
            logger.debug("orphan_twins: skipping %s — %s", entry.name, e)
            continue
        # Filter out empty orphans — no point offering a merge if the
        # twin never accumulated anything.
        if summary["event_count"] == 0:
            continue
        out.append({
            "user_id": entry.name,
            "agent_id": _agent_id_for(entry.name),
            **summary,
        })
    # Sort newest-active first so the user sees the most likely
    # "this is the one I lost" candidate at the top.
    out.sort(key=lambda r: r.get("last_active") or "", reverse=True)
    return out


def _summarise_twin_db(db_path: Path) -> dict:
    """Pull cheap aggregates from a twin event_log DB."""
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro", uri=True,
    )
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        total = cur.execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()[0]
        last_ts_row = cur.execute(
            "SELECT MAX(timestamp) FROM events"
        ).fetchone()
        last_ts = last_ts_row[0] if last_ts_row else None
        # Distinct sessions (some events have empty session_id — that's
        # the "default" session, count it once if present)
        sessions = cur.execute(
            "SELECT COUNT(DISTINCT COALESCE(session_id, '')) FROM events"
        ).fetchone()[0]
        msgs = cur.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE event_type IN ('user_message','assistant_response','assistant_message')"
        ).fetchone()[0]
        return {
            "event_count":   int(total),
            "message_count": int(msgs),
            "session_count": int(sessions),
            "last_active":   _iso_or_none(last_ts),
        }
    finally:
        conn.close()


def _iso_or_none(ts) -> Optional[str]:
    """events.timestamp is stored as a Unix epoch float. Render it as
    a UTC ISO string for the UI to format; None passes through."""
    if ts is None:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


def merge_orphan_into(orphan_user_id: str, current_user_id: str) -> int:
    """Copy every event row from ``orphan_user_id``'s twin DB into
    ``current_user_id``'s. Returns the number of rows merged.

    Implementation notes:
      * We write through ``twin_event_log.append_event`` rather than a
        raw INSERT so any future row-shape evolution (added columns,
        metadata reshapes) flows through one canonical path.
      * The orphan's ``idx`` is dropped; the destination assigns its
        own. Causal order is preserved because we read the source in
        idx-ASC order.
      * Original timestamps are preserved in metadata under
        ``__orphan_origin__`` so future audits can tell which events
        used to belong to a different user_id.
    """
    src_db = _db_path_for(orphan_user_id)
    if not src_db.exists():
        raise FileNotFoundError(f"No orphan twin DB at {src_db}")

    from nexus_server import twin_event_log

    conn = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM events ORDER BY idx ASC"
        ).fetchall()
    finally:
        conn.close()

    n = 0
    for r in rows:
        # Defensive against schema drift — pull columns by name with
        # safe defaults.
        try:
            event_type = r["event_type"]
            content = r["content"] or ""
            session_id = ""
            try:
                session_id = r["session_id"] or ""
            except (KeyError, IndexError) as exc:
                logger.debug("reading session_id column failed: %s", exc)
            import json as _json
            meta = {}
            try:
                meta_raw = r["metadata"]
                if meta_raw:
                    meta = _json.loads(meta_raw)
                    if not isinstance(meta, dict):
                        meta = {"raw": meta}
            except Exception:
                meta = {}
            meta["__orphan_origin__"] = {
                "user_id": orphan_user_id,
                "original_idx": int(r["idx"]),
                "original_timestamp": float(r["timestamp"]) if r["timestamp"] else None,
            }
            twin_event_log.append_event(
                user_id=current_user_id,
                event_type=event_type,
                content=content,
                metadata=meta,
                session_id=session_id,
            )
            n += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "orphan merge: skipping row idx=%s for %s → %s — %s",
                r["idx"] if "idx" in r.keys() else "?",
                orphan_user_id, current_user_id, e,
            )
    logger.info(
        "Orphan merge complete: %d events from %s → %s",
        n, orphan_user_id, current_user_id,
    )
    return n


def rm_orphan_twin(orphan_user_id: str) -> bool:
    """Permanently delete the orphan twin's data dir after a merge.
    Returns True if a directory was removed."""
    target = _twin_base_dir() / orphan_user_id
    if not target.exists():
        return False
    shutil.rmtree(target, ignore_errors=True)
    logger.warning("Removed orphan twin dir for %s", orphan_user_id)
    return not target.exists()
