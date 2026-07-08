"""ThinkingEmitter — turn / session counter contract.

Locks in the dual-counter design (Phase A1):
  * ``turn_id`` is twin-global monotonic — never resets, audit-stable
  * ``session_turn_id`` is per-session — resets per session_id, what
    the desktop renders as "Turn N of this conversation"

Without this dual scheme, the cognition panel's "Turn N" header keeps
climbing forever as the user opens new sessions, which is confusing
("why am I on turn 47 when I just started a new conversation?").
"""

from __future__ import annotations

import asyncio

import pytest

from nexus_core.thinking import ThinkingEmitter


def test_turn_id_is_twin_global_monotonic():
    """Across multiple start_turn calls in the SAME session, turn_id
    keeps incrementing. This is the audit-stable identifier."""
    em = ThinkingEmitter()
    a = em.start_turn(session_id="session_a")
    b = em.start_turn(session_id="session_a")
    c = em.start_turn(session_id="session_a")
    assert (a, b, c) == (1, 2, 3)


def test_turn_id_does_not_reset_across_sessions():
    """Switching session must NOT roll back the global turn_id —
    audit references like "thinking step at turn 17, seq 4" must
    point to one chat turn for life."""
    em = ThinkingEmitter()
    em.start_turn(session_id="session_a")  # turn 1
    em.start_turn(session_id="session_a")  # turn 2
    third = em.start_turn(session_id="session_b")  # turn 3 (NOT 1)
    assert third == 3


def test_session_turn_id_resets_per_session():
    """The per-session counter is what the UI renders. Each new
    session starts back at 1 so the user sees "Turn 1 of THIS chat",
    matching their mental model."""
    em = ThinkingEmitter()

    em.start_turn(session_id="session_a")
    em.emit("heard", "")
    ev_a1 = em.emit("replied", "")
    assert ev_a1.session_id == "session_a"
    assert ev_a1.session_turn_id == 1
    assert ev_a1.turn_id == 1

    em.start_turn(session_id="session_a")
    em.emit("heard", "")
    ev_a2 = em.emit("replied", "")
    assert ev_a2.session_turn_id == 2
    assert ev_a2.turn_id == 2

    # Switch to a different session — session_turn_id resets to 1,
    # turn_id keeps climbing.
    em.start_turn(session_id="session_b")
    em.emit("heard", "")
    ev_b1 = em.emit("replied", "")
    assert ev_b1.session_id == "session_b"
    assert ev_b1.session_turn_id == 1
    assert ev_b1.turn_id == 3

    # Back to session_a — picks up where it left off.
    em.start_turn(session_id="session_a")
    em.emit("heard", "")
    ev_a3 = em.emit("replied", "")
    assert ev_a3.session_id == "session_a"
    assert ev_a3.session_turn_id == 3, (
        "returning to a session must continue its turn count, not "
        "restart at 1"
    )
    assert ev_a3.turn_id == 4


def test_event_carries_session_and_session_turn_id():
    """Every emit() output exposes both ids so subscribers (SSE
    handler, EventLog persister, log scrapers) can route / filter
    by either dimension without re-querying."""
    em = ThinkingEmitter()
    em.start_turn(session_id="my_session")
    ev = em.emit("reasoning", "thinking")
    assert ev.session_id == "my_session"
    assert ev.session_turn_id == 1
    assert ev.turn_id == 1
    d = ev.to_dict()
    assert d["session_id"] == "my_session"
    assert d["session_turn_id"] == 1
    assert d["turn_id"] == 1


def test_seq_resets_per_turn():
    """Seq is per-turn — the Nth step within a turn. New turn ⇒
    seq starts at 1 again."""
    em = ThinkingEmitter()
    em.start_turn(session_id="s")
    e1 = em.emit("heard", "")
    e2 = em.emit("recall", "")
    assert (e1.seq, e2.seq) == (1, 2)

    em.start_turn(session_id="s")
    e3 = em.emit("heard", "")
    assert e3.seq == 1


def test_emit_without_start_turn_auto_bootstraps():
    """Back-compat: callers who emit without start_turn (older test
    paths) get auto-bootstrapped to turn 1 of the empty session."""
    em = ThinkingEmitter()
    ev = em.emit("heard", "first event")
    assert ev.turn_id == 1
    assert ev.session_id == ""
    assert ev.session_turn_id == 1


def test_session_turn_id_default_when_no_session_passed():
    """start_turn() with no session_id keeps the legacy unscoped
    counter (session_id="") so existing tests / callers see no
    behaviour change. The "" key is just another session as far
    as the dict is concerned."""
    em = ThinkingEmitter()
    em.start_turn()
    em.start_turn()
    ev = em.emit("heard", "")
    assert ev.session_id == ""
    assert ev.session_turn_id == 2  # 2nd start_turn under "" session
    assert ev.turn_id == 2          # also 2nd globally


# ── Phase A2: persistence (audit + on-chain anchor) ────────────────


class _FakeEventLog:
    """In-memory stand-in for EventLog. Records every append so
    tests can assert what was persisted without a real SQLite."""
    def __init__(self):
        self.rows: list[dict] = []

    def append(self, event_type, content, session_id="", metadata=None):
        self.rows.append({
            "event_type": event_type,
            "content": content,
            "session_id": session_id,
            "metadata": dict(metadata or {}),
        })
        return len(self.rows)


def test_emit_persists_thinking_step_when_attached():
    """After attach(), every emit also lands a thinking_step row in
    the EventLog. This is the audit trail — one row per reasoning
    step, ready to be hashed into the next BSC state-root anchor."""
    em = ThinkingEmitter()
    log = _FakeEventLog()
    em.attach(event_log=log)

    em.start_turn(session_id="s1")
    em.emit("heard", "Heard the user", content="hello world")
    em.emit("reasoning", "Thinking", content="trying option A")
    em.emit("replied", "Replied", content="here you go")

    assert len(log.rows) == 3, (
        "every emit must produce one thinking_step EventLog row"
    )
    for row in log.rows:
        assert row["event_type"] == "thinking_step"
        assert row["session_id"] == "s1"

    md = log.rows[0]["metadata"]
    assert md["kind"] == "heard"
    assert md["session_turn_id"] == 1
    assert md["turn_id"] == 1
    assert md["seq"] == 1
    assert md["raw_content"] == "hello world"
    assert md["content_storage"] == "inline"


def test_emit_without_attach_skips_persistence_silently():
    """If the host hasn't called attach() (e.g. standalone SDK
    use, tests for unrelated code), emit must work exactly like
    pre-Phase-A2 — pub/sub only, no EventLog write."""
    em = ThinkingEmitter()
    # No attach() call.
    ev = em.emit("heard", "")
    assert ev.kind == "heard"  # in-process delivery still happens
    # No way to assert no-write directly, but persist_failures
    # stays at 0 ⇒ we never even attempted.
    assert em.persist_failure_count == 0


def test_oversize_content_offloads_to_blob_with_hash_and_preview():
    """Content above the inline cap rides as (hash + path + preview)
    instead of inline raw_content — keeps EventLog rows compact and
    state-root hashing fast even for 50KB Gemini chain-of-thought.
    """
    em = ThinkingEmitter()
    log = _FakeEventLog()
    em.attach(event_log=log, inline_cap=1024)

    em.start_turn(session_id="s1")
    big = "x" * 5000
    em.emit("reasoning", "huge", content=big)

    md = log.rows[0]["metadata"]
    assert md["content_storage"] == "backend_blob"
    assert "raw_content" not in md, (
        "oversize content must NOT be inlined — that would defeat "
        "the chain WAL compaction guarantee"
    )
    assert md["content_bytes"] == 5000
    assert len(md["content_hash"]) == 64  # sha256 hex
    assert md["blob_path"].startswith("thinking/")
    # Preview gives the UI something to show without a network round-trip.
    assert md["preview"] == "x" * 512


@pytest.mark.asyncio
async def test_blob_writer_invoked_for_oversize_content():
    """When a blob_writer is wired in (chain mode), oversize content
    triggers a fire-and-forget upload. We verify the call shape +
    that emit returns immediately (doesn't block on the upload)."""
    em = ThinkingEmitter()
    log = _FakeEventLog()
    captured: list[tuple[str, bytes]] = []

    async def fake_blob_writer(path: str, data: bytes):
        captured.append((path, data))
        return "fakehash"

    em.attach(event_log=log, blob_writer=fake_blob_writer, inline_cap=128)
    em.start_turn(session_id="s1")
    em.emit("reasoning", "x", content="y" * 1000)

    # Let the fire-and-forget task run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(captured) == 1
    path, data = captured[0]
    assert path.startswith("thinking/")
    assert len(data) == 1000


def test_persist_failure_does_not_break_in_process_delivery():
    """If the EventLog throws (disk full, sqlite locked, …), the
    in-process subscribers must still see the event. Persistence
    is best-effort; SSE realtime is the user-facing path."""
    class BrokenLog:
        def append(self, *a, **kw):
            raise RuntimeError("disk full")

    em = ThinkingEmitter()
    em.subscribe()
    em.attach(event_log=BrokenLog())
    em.start_turn(session_id="s1")
    ev = em.emit("heard", "")
    # In-process delivery still happened.
    assert ev.kind == "heard"
    assert em.persist_failure_count == 1
