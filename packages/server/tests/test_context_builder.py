"""context_builder — token budgeting, layer assembly, memory projection,
and the rolling session summary (context-management redesign phase 1).

Covers:
  * estimate_tokens sanity (CJK vs ASCII weighting)
  * build(): layer ordering S → M → R → tail; H rides as messages
  * budget trimming order: oldest H first (summary + last 2 turns
    protected), then lowest-priority R blocks, then M tail-first;
    S and the current user message never trimmed
  * Layer M read straight from seeded curated-memory files (no twin
    instantiation); missing memory degrades gracefully
  * rolling summary lifecycle: seed >18 messages → run the post-turn
    hook synchronously → summary row created, synthetic summary message
    prepended by get_session_history, window still 12, staleness gate
  * yield_t3_llm emits the additive ``context_info`` frame after
    tier_classified
"""
from __future__ import annotations

import asyncio
import pathlib
import sqlite3
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from nexus_server import context_builder as cb
from nexus_server.context_builder import RetrievalBlock


# ─────────────────────────────────────────────────────────────────────
# estimate_tokens
# ─────────────────────────────────────────────────────────────────────


def test_estimate_tokens_empty_and_min():
    assert cb.estimate_tokens("") == 0
    assert cb.estimate_tokens("a") == 1  # non-empty is never 0


def test_estimate_tokens_ascii_len_over_3_5():
    # 350 ASCII chars ≈ 100 tokens (len / 3.5)
    assert 90 <= cb.estimate_tokens("a" * 350) <= 110


def test_estimate_tokens_cjk_one_per_char():
    # 100 CJK chars ≈ 100 tokens (~1 token per char)
    assert 90 <= cb.estimate_tokens("肺" * 100) <= 110


def test_estimate_tokens_cjk_heavier_than_ascii():
    assert cb.estimate_tokens("癌" * 40) > cb.estimate_tokens("a" * 40)


# ─────────────────────────────────────────────────────────────────────
# build() — layer ordering, no trimming under budget
# ─────────────────────────────────────────────────────────────────────


def test_build_layer_order_and_messages():
    bundle = cb.build(
        system_text="SYSTEM-PERSONA",
        memory_text="## Your Memory\n- fact-one",
        retrieval_blocks=[
            RetrievalBlock(text="R-BLOCK-A", priority=10, tag="a"),
            RetrievalBlock(text="R-BLOCK-B", priority=99, tag="b"),
        ],
        history=[{"role": "user", "content": "earlier question"},
                 {"role": "assistant", "content": "earlier answer"}],
        current_user_message="the live question",
        system_tail="ACTIVE SKILLS\nskill body",
    )
    st = bundle.system_text
    # S + M first (stable prefix), then R in caller order, tail last.
    assert (st.index("SYSTEM-PERSONA")
            < st.index("## Your Memory")
            < st.index("R-BLOCK-A")
            < st.index("R-BLOCK-B")
            < st.index("ACTIVE SKILLS"))
    # H rides as the messages array; current user message appended last.
    assert bundle.messages == [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "the live question"},
    ]
    assert bundle.dropped == {"history_msgs": 0, "retrieval_blocks": 0}
    assert bundle.token_estimate > 0
    assert bundle.summary_included is False


def test_build_empty_layers_skipped():
    bundle = cb.build(system_text="SYS",
                      memory_text="",
                      current_user_message="q")
    assert bundle.system_text == "SYS"
    assert bundle.messages == [{"role": "user", "content": "q"}]


# ─────────────────────────────────────────────────────────────────────
# Trimming order
# ─────────────────────────────────────────────────────────────────────


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def test_trimming_drops_oldest_history_first_protects_summary_and_recent():
    # 350 ASCII chars ≈ 100 tokens per message (+4 overhead).
    olds = [_msg("user" if i % 2 == 0 else "assistant",
                 f"old-{i}-" + "h" * 350) for i in range(6)]
    recents = [_msg("user" if i % 2 == 0 else "assistant",
                    f"recent-{i}-" + "h" * 350) for i in range(4)]
    summary = _msg("user", cb.SUMMARY_PREFIX + "\n早前摘要")
    history = [summary] + olds + recents

    # budget=900 → usable=810. Full total ≈ 1060+ → exactly the 3
    # oldest non-summary messages must drop.
    bundle = cb.build(
        system_text="SYS",
        memory_text="",
        history=history,
        current_user_message="q",
        budget=900,
    )
    contents = [m["content"] for m in bundle.messages]
    # Summary survives, and stays first.
    assert contents[0].startswith(cb.SUMMARY_PREFIX)
    assert bundle.summary_included is True
    # Oldest history dropped first…
    assert bundle.dropped["history_msgs"] == 3
    assert not any(c.startswith("old-0-") for c in contents)
    assert not any(c.startswith("old-1-") for c in contents)
    assert not any(c.startswith("old-2-") for c in contents)
    assert any(c.startswith("old-3-") for c in contents)
    # …while the last 2 turns (4 messages) are protected.
    for i in range(4):
        assert any(c.startswith(f"recent-{i}-") for c in contents)
    # Current user message is never trimmed and is last.
    assert bundle.messages[-1] == {"role": "user", "content": "q"}
    # S never trimmed.
    assert "SYS" in bundle.system_text


def test_trimming_drops_lowest_priority_blocks_after_history():
    protected_history = [
        _msg("user" if i % 2 == 0 else "assistant", "m" * 35)
        for i in range(4)
    ]  # all 4 protected (last 2 turns)
    blocks = [
        RetrievalBlock(text="BLOCK-A-" + "x" * 350, priority=5, tag="A"),
        RetrievalBlock(text="BLOCK-B-" + "x" * 350, priority=1, tag="B"),
        RetrievalBlock(text="BLOCK-C-" + "x" * 350, priority=9, tag="C"),
    ]
    bundle = cb.build(
        system_text="SYS",
        memory_text="",
        retrieval_blocks=blocks,
        history=protected_history,
        current_user_message="q",
        budget=280,   # usable 252 — forces dropping the 2 lowest blocks
    )
    # No history was droppable (all protected) → R took the hit,
    # lowest priority first (B prio 1, then A prio 5). C survives.
    assert bundle.dropped["history_msgs"] == 0
    assert bundle.dropped["retrieval_blocks"] == 2
    assert "BLOCK-C-" in bundle.system_text
    assert "BLOCK-A-" not in bundle.system_text
    assert "BLOCK-B-" not in bundle.system_text
    assert len(bundle.messages) == 5  # 4 protected history + current


def test_memory_only_trimmed_after_retrieval_blocks():
    # Over budget by roughly one block: the R block must drop while
    # the memory layer stays intact.
    memory = "## Your Memory\n- " + "m" * 300     # ~90 tokens
    block = RetrievalBlock(text="DROPPABLE-" + "x" * 350, priority=1)
    bundle = cb.build(
        system_text="SYS",
        memory_text=memory,
        retrieval_blocks=[block],
        current_user_message="q",
        budget=140,   # usable 126 < mem+block (~195) but > mem alone
    )
    assert bundle.dropped["retrieval_blocks"] == 1
    assert "DROPPABLE-" not in bundle.system_text
    assert "## Your Memory" in bundle.system_text


def test_memory_truncated_tail_first_as_last_resort():
    memory = "MEMHEAD-" + "m" * 2000 + "-MEMTAIL"
    bundle = cb.build(
        system_text="SYS",
        memory_text=memory,
        current_user_message="q",
        budget=400,   # usable 360; memory alone ≈ 575 → tail cut
    )
    assert "MEMHEAD-" in bundle.system_text     # head kept
    assert "MEMTAIL" not in bundle.system_text  # tail truncated
    assert "SYS" in bundle.system_text          # S untouched
    assert bundle.messages[-1]["content"] == "q"
    assert bundle.token_estimate <= 360


def test_memory_layer_fraction_cap_applies_upfront(monkeypatch):
    # Pin the budget (defaults are now model-aware, so the test must
    # not depend on whatever DEFAULT_LLM_MODEL the test env carries).
    # 32k budget → M cap = 4800 tokens. A 10k-token memory must come
    # back capped even with no overall budget pressure.
    monkeypatch.setenv("NEXUS_CONTEXT_BUDGET", "32000")
    memory = "MEMHEAD-" + "m" * 35_000 + "-MEMTAIL"
    bundle = cb.build(
        system_text="SYS",
        memory_text=memory,
        current_user_message="q",
    )
    assert "MEMHEAD-" in bundle.system_text
    assert "MEMTAIL" not in bundle.system_text
    assert bundle.token_estimate <= int(32_000 * cb.BUDGETS["M"]) + 50


def test_budget_env_override(monkeypatch):
    monkeypatch.setenv("NEXUS_CONTEXT_BUDGET", "700")
    olds = [_msg("user", f"old-{i}-" + "h" * 350) for i in range(8)]
    bundle = cb.build(
        system_text="SYS",
        memory_text="",
        history=olds,
        current_user_message="q",
    )
    # 8×104 ≈ 832 > 630 usable → some history must have been dropped.
    assert bundle.dropped["history_msgs"] >= 1
    assert bundle.token_estimate <= 630


# ─────────────────────────────────────────────────────────────────────
# Layer M — curated-memory projection from twin files
# ─────────────────────────────────────────────────────────────────────


def test_memory_projection_reads_twin_files_without_twin(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_TWIN_BASE_DIR", str(tmp_path))
    d = tmp_path / "u-mem" / "curated_memory"
    d.mkdir(parents=True)
    (d / "MEMORY.md").write_text(
        "患者张三偏好中文报告\n§\n上次讨论了 RECIST 1.1 评估",
        encoding="utf-8",
    )
    (d / "USER.md").write_text("喜欢简洁要点式回复", encoding="utf-8")

    proj = cb.get_memory_projection("u-mem")
    # Same shape CuratedMemory.get_prompt_context() renders.
    assert "## Your Memory" in proj
    assert "- 患者张三偏好中文报告" in proj
    assert "- 上次讨论了 RECIST 1.1 评估" in proj
    assert "## About This User" in proj
    assert "- 喜欢简洁要点式回复" in proj

    # build() picks it up via user_id and puts it right after S.
    bundle = cb.build(system_text="SYS-PERSONA", user_id="u-mem",
                      current_user_message="hi")
    st = bundle.system_text
    assert st.index("SYS-PERSONA") < st.index("## Your Memory")
    assert "患者张三偏好中文报告" in st


def test_memory_projection_missing_is_graceful(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_TWIN_BASE_DIR", str(tmp_path))
    assert cb.get_memory_projection("ghost-user") == ""
    bundle = cb.build(system_text="SYS", user_id="ghost-user",
                      current_user_message="q")
    assert bundle.system_text == "SYS"   # layer skipped entirely


def test_memory_projection_empty_files_graceful(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_TWIN_BASE_DIR", str(tmp_path))
    d = tmp_path / "u-empty" / "curated_memory"
    d.mkdir(parents=True)
    (d / "MEMORY.md").write_text("", encoding="utf-8")
    assert cb.get_memory_projection("u-empty") == ""


# ─────────────────────────────────────────────────────────────────────
# Rolling session summary
# ─────────────────────────────────────────────────────────────────────


def _seed_session(user_id: str, session_id: str, n: int) -> None:
    from nexus_server import twin_event_log
    for i in range(n):
        twin_event_log.append_event(
            user_id,
            "user_message" if i % 2 == 0 else "assistant_response",
            f"msg-{i} 病灶 3.2 cm",
            session_id=session_id,
        )


def _mock_summary_llm(monkeypatch, text="早前讨论：左肺病灶 3.2 cm，方案未定。"):
    from nexus_server import llm_gateway
    mock = AsyncMock(return_value=(text, "mock-model", "stop", []))
    monkeypatch.setattr(llm_gateway, "call_llm", mock)
    return mock


def test_rolling_summary_lifecycle(monkeypatch):
    uid, sid = "user-sum", "sess-1"
    _seed_session(uid, sid, 20)          # 8 beyond the 12-window
    mock = _mock_summary_llm(monkeypatch)

    # Run the post-turn hook's coroutine synchronously.
    assert asyncio.run(cb.maybe_update_session_summary(uid, sid)) is True

    # LLM contract: cheap call, anti-invention prompt, verbatim numbers.
    kwargs = mock.call_args.kwargs
    assert kwargs["max_tokens"] == 512
    assert "编造" in kwargs["system_prompt"]        # forbids inventing facts
    assert "逐字保留" in kwargs["system_prompt"]    # clinical numbers verbatim
    body = kwargs["messages"][0]["content"]
    # Only the fallen-out messages (0..7) go to the summariser.
    assert "msg-0 " in body and "msg-7 " in body
    assert "msg-8 " not in body

    # Row upserted.
    from nexus_server.database import get_db_connection
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT upto_idx, summary FROM chat_session_summaries "
            "WHERE user_id = ? AND session_id = ?", (uid, sid),
        ).fetchone()
    assert row is not None
    assert int(row[0]) == 8              # 20 total − 12 window
    assert "3.2 cm" in row[1]

    # get_session_history: synthetic summary + the 12-window intact.
    hist = cb.get_session_history(uid, sid)
    assert len(hist) == 13
    assert hist[0]["role"] == "user"
    assert hist[0]["content"].startswith(cb.SUMMARY_PREFIX)
    assert "3.2 cm" in hist[0]["content"]
    assert [m["content"] for m in hist[1:]] == [
        f"msg-{i} 病灶 3.2 cm" for i in range(8, 20)
    ]

    # Staleness gate: immediately re-running is a no-op (fresh summary).
    mock.reset_mock()
    assert asyncio.run(cb.maybe_update_session_summary(uid, sid)) is False
    mock.assert_not_called()


def test_summary_not_generated_for_short_sessions(monkeypatch):
    uid, sid = "user-short", "sess-2"
    _seed_session(uid, sid, 15)          # only 3 beyond window < 6
    mock = _mock_summary_llm(monkeypatch)
    assert asyncio.run(cb.maybe_update_session_summary(uid, sid)) is False
    mock.assert_not_called()
    # No summary row → history is the plain window.
    hist = cb.get_session_history(uid, sid)
    assert len(hist) == 12
    assert not hist[0]["content"].startswith(cb.SUMMARY_PREFIX)


def test_schedule_hook_runs_inline_without_running_loop(monkeypatch):
    """chat_router's post-turn hook, exercised synchronously (no event
    loop → schedule_session_summary_update runs the coroutine inline)."""
    uid, sid = "user-hook", "sess-3"
    _seed_session(uid, sid, 20)
    _mock_summary_llm(monkeypatch)

    cb.schedule_session_summary_update(uid, sid)

    from nexus_server.database import get_db_connection
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT upto_idx FROM chat_session_summaries "
            "WHERE user_id = ? AND session_id = ?", (uid, sid),
        ).fetchone()
    assert row is not None and int(row[0]) == 8


def test_schedule_hook_precheck_skips_short_sessions(monkeypatch):
    uid, sid = "user-pre", "sess-4"
    _seed_session(uid, sid, 6)
    mock = _mock_summary_llm(monkeypatch)
    cb.schedule_session_summary_update(uid, sid)
    mock.assert_not_called()


def test_llm_failure_leaves_no_summary_row(monkeypatch):
    uid, sid = "user-fail", "sess-5"
    _seed_session(uid, sid, 20)
    from nexus_server import llm_gateway
    monkeypatch.setattr(
        llm_gateway, "call_llm",
        AsyncMock(side_effect=RuntimeError("provider down")),
    )
    assert asyncio.run(cb.maybe_update_session_summary(uid, sid)) is False
    hist = cb.get_session_history(uid, sid)
    assert len(hist) == 12               # degrades to the plain window


def test_get_session_history_custom_fetcher_no_summary_row():
    """Doc-chat style fetcher: >window total but no stored summary →
    plain window (legacy behaviour)."""
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(12)]

    def fetcher(_u, _s, _limit):
        return msgs, 30

    hist = cb.get_session_history("u", "doc:xyz", fetcher=fetcher)
    assert hist == msgs


# ─────────────────────────────────────────────────────────────────────
# context_info SSE frame from the T3 path
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def graph_conn(tmp_path):
    db = tmp_path / "graph.db"
    c = sqlite3.connect(db)
    c.executescript(
        """
        CREATE TABLE clinical_graph_nodes (
            node_id INTEGER PRIMARY KEY,
            user_id TEXT NOT NULL,
            patient_hash TEXT NOT NULL,
            node_type TEXT NOT NULL,
            content_json TEXT NOT NULL,
            weight REAL DEFAULT 1.0
        );
        CREATE TABLE clinical_graph_edges (
            src_node INTEGER, dst_node INTEGER,
            user_id TEXT, patient_hash TEXT,
            edge_type TEXT
        );
        """
    )
    c.execute(
        "INSERT INTO clinical_graph_nodes VALUES "
        "(1, 'u1', 'p1', 'finding', '{\"label\":\"left renal mass\"}', 0.9)"
    )
    c.commit()
    yield c
    c.close()


def test_yield_t3_llm_emits_context_info_frame(graph_conn, monkeypatch):
    from nexus_server import retrieval_tiers, llm_gateway

    captured: dict = {}

    async def fake_call_llm(*, messages, system_prompt, model,
                            temperature, max_tokens, tools):
        captured["messages"] = messages
        captured["system_prompt"] = system_prompt
        return ("Answer.", "mock-model", "stop", [])

    monkeypatch.setattr(llm_gateway, "call_llm", fake_call_llm)

    async def _drain(it):
        out = []
        async for chunk in it:
            out.append((chunk.kind, dict(chunk.data)))
        return out

    chunks = asyncio.run(_drain(retrieval_tiers.yield_t3_llm(
        graph_conn, user_id="u1", patient_hash="p1",
        question="What about the renal mass?",
    )))
    kinds = [k for k, _ in chunks]
    assert kinds[0] == "tier_classified"
    assert "context_info" in kinds
    assert kinds.index("context_info") > kinds.index("tier_classified")
    assert kinds.index("context_info") < kinds.index("final_answer_chunk")

    info = dict(chunks[kinds.index("context_info")][1])
    assert set(info) == {
        "history_msgs", "summary_included", "retrieval_blocks",
        "dropped_history", "dropped_blocks", "token_estimate",
    }
    assert info["history_msgs"] == 0          # no session_id → no history
    assert info["summary_included"] is False
    assert info["retrieval_blocks"] >= 1      # patient context block
    assert info["dropped_history"] == 0
    assert info["dropped_blocks"] == 0
    assert info["token_estimate"] > 0

    # Behaviour pins from the legacy path still hold.
    assert captured["messages"] == [
        {"role": "user", "content": "What about the renal mass?"},
    ]
    assert "left renal mass" in captured["system_prompt"]
    assert "PATIENT CONTEXT" in captured["system_prompt"]
