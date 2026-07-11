"""Writing Studio conversational co-writing — contract tests.

Product pivot: the primary writing interaction is a chat with the AI
which GENERATES and REVISES the document; the human reviews and
directs. Covers:

  * GET  /docs/{id}/chat  — chronological history, user isolation
  * POST /docs/{id}/chat  — SSE frames with a mocked LLM:
      - reply-only turn (no <doc> block → no doc change, no snapshot)
      - <doc> revision turn (previous body snapshotted with label
        '对话修订前', doc body updated, doc_applied=1, {{ref:ID}}
        tokens preserved)
      - unknown {{ref:ID}} tokens stripped defensively
      - <doc> tag split across stream chunk boundaries
      - provenance warning for numbers absent from context
      - LLM failure → error frame, user message still persisted
      - F-skills: requested+enabled skill content injected into the
        co-writing system prompt (skills_router.build_skills_block)
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

PW = "Str0ng-Pass-123"


# ─────────────────────────────────────────────────────────────────────
# Helpers (same conventions as test_writing_studio.py)
# ─────────────────────────────────────────────────────────────────────


def _register(client, name):
    r = client.post("/api/v1/auth/register",
                    json={"username": name, "password": PW})
    assert r.status_code == 201, r.text
    return r.json()


def _auth(user):
    return {"Authorization": f"Bearer {user['jwt_token']}"}


def _create_doc(client, user, title="病例报告"):
    r = client.post("/api/v1/docs", json={"title": title},
                    headers=_auth(user))
    assert r.status_code == 200, r.text
    return r.json()


def _register_patient(client, user, initials="张三丰", age=57, sex="M"):
    r = client.post(
        "/api/v1/dicom/patients/register-manual",
        json={
            "initials": initials, "age": age, "sex": sex,
            "chief_complaint": "咳嗽伴消瘦 2 月",
        },
        headers=_auth(user),
    )
    assert r.status_code == 200, r.text
    return r.json()["patient_hash"]


def _add_reference(client, user, doc_id, patient_hash):
    r = client.post(
        f"/api/v1/docs/{doc_id}/references",
        json={"ref_type": "patient", "target_id": patient_hash,
              "granularity": "basics"},
        headers=_auth(user),
    )
    assert r.status_code == 200, r.text
    return r.json()["ref_id"]


def _sse_frames(body_text: str) -> list[dict]:
    frames = []
    for block in body_text.split("\n\n"):
        block = block.strip()
        if block.startswith("data: "):
            frames.append(json.loads(block[len("data: "):]))
    return frames


def _chat(client, user, doc_id, message, ref_ids=None):
    r = client.post(
        f"/api/v1/docs/{doc_id}/chat",
        json={"message": message, "ref_ids": ref_ids or []},
        headers=_auth(user),
    )
    assert r.status_code == 200, r.text
    return _sse_frames(r.text)


def _mock_llm(monkeypatch, content):
    mock = AsyncMock(return_value=(content, "mock-model", "stop", []))
    monkeypatch.setattr("nexus_server.llm_gateway.call_llm", mock)
    return mock


# ─────────────────────────────────────────────────────────────────────
# Reply-only turn
# ─────────────────────────────────────────────────────────────────────


def test_chat_reply_only_no_doc_change(client, monkeypatch):
    user = _register(client, "alice")
    doc = _create_doc(client, user, title="NSCLC 病例")
    client.put(f"/api/v1/docs/{doc['id']}", json={"body": "原始正文。"},
               headers=_auth(user))

    reply_text = "这个标题建议突出治疗方案，比如强调靶向药名称。"
    mock = _mock_llm(monkeypatch, reply_text)

    frames = _chat(client, user, doc["id"], "标题怎么起比较好？")
    types = [f["type"] for f in frames]

    # Reply streamed, no doc frames at all.
    assert "reply_chunk" in types
    assert "doc_started" not in types
    assert "doc_chunk" not in types
    assert "".join(
        f["text"] for f in frames if f["type"] == "reply_chunk"
    ) == reply_text

    done = frames[-1]
    assert done["type"] == "done"
    assert done["reply"] == reply_text
    assert done["doc_body"] is None
    assert done["snapshot_id"] is None

    # Doc untouched, no snapshot beyond the initial PUT's.
    r = client.get(f"/api/v1/docs/{doc['id']}", headers=_auth(user))
    assert r.json()["body"] == "原始正文。"
    r = client.get(f"/api/v1/docs/{doc['id']}/snapshots",
                   headers=_auth(user))
    assert len(r.json()["snapshots"]) == 1  # only the PUT's snapshot

    # Both turns persisted; assistant not doc_applied.
    r = client.get(f"/api/v1/docs/{doc['id']}/chat", headers=_auth(user))
    msgs = r.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["text"] == "标题怎么起比较好？"
    assert msgs[1]["text"] == reply_text
    assert msgs[0]["doc_applied"] == 0
    assert msgs[1]["doc_applied"] == 0

    # System prompt carried the co-writing contract + title + body.
    args, _ = mock.call_args
    messages, system_prompt = args[0], args[1]
    assert messages[-1] == {"role": "user",
                            "content": "标题怎么起比较好？"}
    assert "<doc>" in system_prompt and "</doc>" in system_prompt
    assert "NSCLC 病例" in system_prompt
    assert "原始正文。" in system_prompt
    assert "{{ref:ID}}" in system_prompt
    assert "不得编造数值" in system_prompt


# ─────────────────────────────────────────────────────────────────────
# <doc> revision turn
# ─────────────────────────────────────────────────────────────────────


def test_chat_doc_revision_applies_snapshots_and_preserves_refs(
    client, monkeypatch,
):
    user = _register(client, "alice")
    patient_hash = _register_patient(client, user, initials="张三丰")
    doc = _create_doc(client, user)
    ref_id = _add_reference(client, user, doc["id"], patient_hash)

    prev_body = f"引言。{{{{ref:{ref_id}}}}}"
    client.put(f"/api/v1/docs/{doc['id']}", json={"body": prev_body},
               headers=_auth(user))

    new_body = f"扩写后的引言，病情概述见下。{{{{ref:{ref_id}}}}}\n\n结论：治疗有效。"
    llm_out = f"已按要求扩写引言并补充结论。<doc>{new_body}</doc>"
    mock = _mock_llm(monkeypatch, llm_out)

    frames = _chat(client, user, doc["id"], "把引言扩写一下，加个结论",
                   ref_ids=[ref_id])
    types = [f["type"] for f in frames]

    assert "reply_chunk" in types
    assert "doc_started" in types
    assert "doc_chunk" in types
    # doc_started comes after the reply chunks and before doc chunks.
    assert types.index("doc_started") > types.index("reply_chunk")
    assert types.index("doc_started") < types.index("doc_chunk")

    assert "".join(
        f["text"] for f in frames if f["type"] == "reply_chunk"
    ).strip() == "已按要求扩写引言并补充结论。"
    assert "".join(
        f["text"] for f in frames if f["type"] == "doc_chunk"
    ).strip() == new_body

    done = frames[-1]
    assert done["type"] == "done"
    assert done["reply"] == "已按要求扩写引言并补充结论。"
    assert done["doc_body"] == new_body
    assert done["snapshot_id"] is not None

    # Doc body actually updated; ref token survived verbatim.
    r = client.get(f"/api/v1/docs/{doc['id']}", headers=_auth(user))
    assert r.json()["body"] == new_body
    assert f"{{{{ref:{ref_id}}}}}" in r.json()["body"]

    # The PREVIOUS body was snapshotted with the chat label.
    r = client.get(f"/api/v1/docs/{doc['id']}/snapshots",
                   headers=_auth(user))
    snaps = r.json()["snapshots"]
    labels = [s["label"] for s in snaps]
    assert "对话修订前" in labels
    chat_snap = next(s for s in snaps if s["label"] == "对话修订前")
    assert chat_snap["id"] == done["snapshot_id"]

    # Restoring that snapshot brings the previous body back.
    r = client.post(
        f"/api/v1/docs/{doc['id']}/snapshots/{chat_snap['id']}/restore",
        headers=_auth(user),
    )
    assert r.status_code == 200
    assert r.json()["body"] == prev_body

    # Assistant message flagged doc_applied=1.
    r = client.get(f"/api/v1/docs/{doc['id']}/chat", headers=_auth(user))
    msgs = r.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["doc_applied"] == 1
    assert msgs[1]["text"] == "已按要求扩写引言并补充结论。"

    # The requested ref snapshot was injected into the system prompt.
    args, _ = mock.call_args
    system_prompt = args[1]
    assert "P-" + patient_hash[:6] in system_prompt


def test_chat_doc_tag_split_across_chunks(client, monkeypatch):
    """The server feeds the LLM output through the parser in 64-char
    chunks — place '<doc>' straddling a chunk boundary and make sure it
    is still recognised."""
    user = _register(client, "alice")
    doc = _create_doc(client, user)

    reply = "a" * 62  # '<doc>' spans positions 62..67 → split at 64
    body = "正文内容。"
    _mock_llm(monkeypatch, f"{reply}<doc>{body}</doc>")

    frames = _chat(client, user, doc["id"], "写一版正文")
    done = frames[-1]
    assert done["type"] == "done"
    assert done["reply"] == reply
    assert done["doc_body"] == body
    assert "".join(
        f["text"] for f in frames if f["type"] == "reply_chunk"
    ) == reply
    r = client.get(f"/api/v1/docs/{doc['id']}", headers=_auth(user))
    assert r.json()["body"] == body


def test_chat_unknown_ref_token_stripped(client, monkeypatch):
    user = _register(client, "alice")
    patient_hash = _register_patient(client, user, initials="王五")
    doc = _create_doc(client, user)
    ref_id = _add_reference(client, user, doc["id"], patient_hash)

    bogus = "00000000-0000-0000-0000-000000000000"
    llm_body = (
        f"基线：{{{{ref:{ref_id}}}}} 伪造引用：{{{{ref:{bogus}}}}} 结束。"
    )
    _mock_llm(monkeypatch, f"好的。<doc>{llm_body}</doc>")

    frames = _chat(client, user, doc["id"], "写基线部分")
    done = frames[-1]
    assert done["type"] == "done"
    assert f"{{{{ref:{ref_id}}}}}" in done["doc_body"]
    assert bogus not in done["doc_body"]

    r = client.get(f"/api/v1/docs/{doc['id']}", headers=_auth(user))
    body = r.json()["body"]
    assert f"{{{{ref:{ref_id}}}}}" in body
    assert bogus not in body


def test_chat_provenance_warning_on_unsourced_numbers(
    client, monkeypatch,
):
    user = _register(client, "alice")
    doc = _create_doc(client, user)
    client.put(f"/api/v1/docs/{doc['id']}", json={"body": "入组 42 例。"},
               headers=_auth(user))

    # '42' is in the previous body → allowed; '85%' is invented.
    _mock_llm(monkeypatch,
              "已补充疗效数据。<doc>入组 42 例，客观缓解率 85%。</doc>")

    frames = _chat(client, user, doc["id"], "补充疗效")
    warn = next(f for f in frames if f["type"] == "provenance_warning")
    assert "85%" in warn["numbers"]
    assert "42" not in warn["numbers"]
    # Warning precedes done; done is last.
    types = [f["type"] for f in frames]
    assert types.index("provenance_warning") < types.index("done")
    assert types[-1] == "done"


def test_chat_numbers_from_refs_and_message_not_warned(
    client, monkeypatch,
):
    user = _register(client, "alice")
    patient_hash = _register_patient(client, user, initials="王五",
                                     age=63)
    doc = _create_doc(client, user)
    ref_id = _add_reference(client, user, doc["id"], patient_hash)

    # '63' from the ref snapshot (age), '3' from the user message.
    _mock_llm(monkeypatch,
              "好的。<doc>该 63 岁患者随访 3 月，疗效良好。</doc>")

    frames = _chat(client, user, doc["id"], "写一句随访 3 月的总结",
                   ref_ids=[ref_id])
    types = [f["type"] for f in frames]
    assert "provenance_warning" not in types
    assert types[-1] == "done"


# ─────────────────────────────────────────────────────────────────────
# History endpoint — ordering, window, isolation
# ─────────────────────────────────────────────────────────────────────


def test_chat_history_ordering_and_isolation(client, monkeypatch):
    alice = _register(client, "alice")
    bob = _register(client, "bob")
    doc = _create_doc(client, alice)

    _mock_llm(monkeypatch, "第一轮回复。")
    _chat(client, alice, doc["id"], "第一个问题")
    _mock_llm(monkeypatch, "第二轮回复。")
    _chat(client, alice, doc["id"], "第二个问题")

    r = client.get(f"/api/v1/docs/{doc['id']}/chat", headers=_auth(alice))
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert [m["role"] for m in msgs] == [
        "user", "assistant", "user", "assistant",
    ]
    assert [m["text"] for m in msgs] == [
        "第一个问题", "第一轮回复。", "第二个问题", "第二轮回复。",
    ]
    assert all(
        {"id", "role", "text", "doc_applied", "created_at"} <= set(m)
        for m in msgs
    )

    # User B: history and posting are both 404 (doc invisible).
    assert client.get(
        f"/api/v1/docs/{doc['id']}/chat", headers=_auth(bob),
    ).status_code == 404
    assert client.post(
        f"/api/v1/docs/{doc['id']}/chat", json={"message": "hijack"},
        headers=_auth(bob),
    ).status_code == 404

    # Unknown doc → 404 for both verbs.
    assert client.get(
        "/api/v1/docs/nope/chat", headers=_auth(alice),
    ).status_code == 404
    assert client.post(
        "/api/v1/docs/nope/chat", json={"message": "x"},
        headers=_auth(alice),
    ).status_code == 404

    # No token → 401/403.
    assert client.get(
        f"/api/v1/docs/{doc['id']}/chat",
    ).status_code in (401, 403)


def test_chat_history_window_caps_at_12(client, monkeypatch):
    """Only the last 12 stored turns are sent to the LLM (plus the live
    user message)."""
    user = _register(client, "alice")
    doc = _create_doc(client, user)

    import uuid as _uuid
    from datetime import datetime, timedelta, timezone
    from nexus_server.database import get_db_connection
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with get_db_connection() as conn:
        for i in range(15):
            conn.execute(
                "INSERT INTO doc_chat_messages "
                "(id, doc_id, user_id, role, text, doc_applied, "
                " created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
                (str(_uuid.uuid4()), doc["id"], user["user_id"],
                 "user" if i % 2 == 0 else "assistant", f"msg-{i}",
                 (base + timedelta(minutes=i)).isoformat()),
            )
        conn.commit()

    mock = _mock_llm(monkeypatch, "收到。")
    _chat(client, user, doc["id"], "最新问题")

    args, _ = mock.call_args
    messages = args[0]
    assert len(messages) == 13  # 12 history + the live user turn
    assert messages[0]["content"] == "msg-3"   # oldest 3 dropped
    assert messages[-2]["content"] == "msg-14"
    assert messages[-1] == {"role": "user", "content": "最新问题"}


# ─────────────────────────────────────────────────────────────────────
# Error path
# ─────────────────────────────────────────────────────────────────────


def test_chat_llm_failure_persists_user_message_no_doc_change(
    client, monkeypatch,
):
    user = _register(client, "alice")
    doc = _create_doc(client, user)
    client.put(f"/api/v1/docs/{doc['id']}", json={"body": "正文。"},
               headers=_auth(user))

    mock = AsyncMock(side_effect=RuntimeError("provider exploded"))
    monkeypatch.setattr("nexus_server.llm_gateway.call_llm", mock)

    r = client.post(
        f"/api/v1/docs/{doc['id']}/chat", json={"message": "改写一下"},
        headers=_auth(user),
    )
    assert r.status_code == 200  # error travels inside the stream
    frames = _sse_frames(r.text)
    assert frames[-1]["type"] == "error"
    assert "provider exploded" in frames[-1]["message"]

    # User message survived; no assistant message.
    r = client.get(f"/api/v1/docs/{doc['id']}/chat", headers=_auth(user))
    msgs = r.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert msgs[0]["text"] == "改写一下"

    # Doc untouched, no chat snapshot.
    r = client.get(f"/api/v1/docs/{doc['id']}", headers=_auth(user))
    assert r.json()["body"] == "正文。"
    r = client.get(f"/api/v1/docs/{doc['id']}/snapshots",
                   headers=_auth(user))
    assert all(
        s["label"] != "对话修订前" for s in r.json()["snapshots"]
    )


# ─────────────────────────────────────────────────────────────────────
# F-skills — "/" invocations ride into the co-writing system prompt
# ─────────────────────────────────────────────────────────────────────

SKILL_MARKER = "SKILLMARKER-WRITING-4242"
SKILL_BODY = (
    "---\n"
    "name: haiku-mode\n"
    "description: Answer in haiku form\n"
    "---\n"
    "\n"
    f"When this skill is active, always respond in traditional haiku "
    f"form (5-7-5 syllables). {SKILL_MARKER}\n"
)


def _install_skill(client, user, monkeypatch):
    """Install a folder-layout skill without any network (same fake as
    test_skills_router._patch_install)."""
    from nexus_core.skills.manager import SkillManager

    async def fake_install(self, source):
        name = source.split(":")[-1].split("/")[-1]
        d = self._skills_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(SKILL_BODY, encoding="utf-8")
        skill = self._load_skill_folder(d)
        self._skills[skill.name] = skill
        return skill

    monkeypatch.setattr(SkillManager, "install", fake_install)
    r = client.post("/api/v1/skills/install",
                    json={"identifier": "anthropic:haiku-mode"},
                    headers=_auth(user))
    assert r.status_code == 200, r.text


def test_chat_requested_enabled_skill_injected_into_prompt(
    client, monkeypatch,
):
    user = _register(client, "alice")
    doc = _create_doc(client, user)
    _install_skill(client, user, monkeypatch)

    mock = _mock_llm(monkeypatch, "好的，收到。")
    r = client.post(
        f"/api/v1/docs/{doc['id']}/chat",
        json={"message": "帮我起个标题", "skills": ["haiku-mode"]},
        headers=_auth(user),
    )
    assert r.status_code == 200, r.text
    frames = _sse_frames(r.text)
    assert frames[-1]["type"] == "done"

    # The enabled skill's instructions landed in the system prompt,
    # alongside (not replacing) the co-writing contract.
    args, _ = mock.call_args
    system_prompt = args[1]
    assert "## Skill: haiku-mode" in system_prompt
    assert SKILL_MARKER in system_prompt
    assert "ACTIVE SKILLS" in system_prompt
    assert "<doc>" in system_prompt  # co-writing contract intact

    # A turn WITHOUT the explicit invocation doesn't inject it.
    mock2 = _mock_llm(monkeypatch, "好的。")
    r = client.post(
        f"/api/v1/docs/{doc['id']}/chat", json={"message": "再改一下"},
        headers=_auth(user),
    )
    assert r.status_code == 200, r.text
    assert SKILL_MARKER not in mock2.call_args[0][1]


# ─────────────────────────────────────────────────────────────────────
# Attachments — 📎 uploads ride into the co-writing system prompt
# ─────────────────────────────────────────────────────────────────────


def _seed_upload(user_id, file_id, name, extracted_text,
                 mime="text/plain"):
    """Insert a fake uploads row directly (same convention as
    test_files_endpoints / the chat attachment tests) so we don't have
    to round-trip a multipart upload."""
    from nexus_server import files as _files
    from nexus_server.database import get_db_connection
    _files._ensure_uploads_table()
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO uploads (file_id, user_id, name, mime, "
            " size_bytes, disk_path, created_at, sha256, gnfd_path, "
            " extracted_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (file_id, user_id, name, mime, 100, "/tmp/nonexistent",
             "2026-07-01T00:00:00Z", "deadbeef", "", extracted_text),
        )
        conn.commit()


def test_chat_attachment_content_injected_into_prompt(
    client, monkeypatch,
):
    """A staged 📎 upload's extracted text lands in the co-writing
    system prompt as a '## 用户附件: {name}' section — after the
    reference snapshots, before the skills block."""
    user = _register(client, "alice")
    patient_hash = _register_patient(client, user, initials="张三丰")
    doc = _create_doc(client, user)
    ref_id = _add_reference(client, user, doc["id"], patient_hash)
    _install_skill(client, user, monkeypatch)
    _seed_upload(
        user["user_id"], "file-labs", "labs.txt",
        "Hemoglobin 8.5 g/dL (low). WBC 14k.",
    )

    mock = _mock_llm(monkeypatch, "好的，已参考附件内容。")
    r = client.post(
        f"/api/v1/docs/{doc['id']}/chat",
        json={
            "message": "参考附件里的化验结果写一段基线描述",
            "ref_ids": [ref_id],
            "skills": ["haiku-mode"],
            "attachments": [{"file_id": "file-labs", "name": "labs.txt"}],
        },
        headers=_auth(user),
    )
    assert r.status_code == 200, r.text
    frames = _sse_frames(r.text)
    assert frames[-1]["type"] == "done"

    args, _ = mock.call_args
    system_prompt = args[1]
    # Section header + the file's extracted text made it in.
    assert "## 用户附件: labs.txt" in system_prompt
    assert "Hemoglobin 8.5 g/dL (low). WBC 14k." in system_prompt
    # Co-writing contract intact.
    assert "<doc>" in system_prompt
    # Ordering: references → attachments → skills.
    assert (system_prompt.index("引用数据")
            < system_prompt.index("## 用户附件: labs.txt")
            < system_prompt.index("ACTIVE SKILLS"))


def test_chat_attachment_text_capped_at_8k(client, monkeypatch):
    """Same per-attachment cap as the v2 chat path — 8000 chars."""
    user = _register(client, "alice")
    doc = _create_doc(client, user)
    big = "A" * 8000 + "TAIL-MARKER-NOT-IN-PROMPT"
    _seed_upload(user["user_id"], "file-big", "big.txt", big)

    mock = _mock_llm(monkeypatch, "好的。")
    r = client.post(
        f"/api/v1/docs/{doc['id']}/chat",
        json={"message": "总结附件",
              "attachments": [{"file_id": "file-big", "name": "big.txt"}]},
        headers=_auth(user),
    )
    assert r.status_code == 200, r.text
    system_prompt = mock.call_args[0][1]
    assert "## 用户附件: big.txt" in system_prompt
    assert "AAAA" in system_prompt
    assert "TAIL-MARKER-NOT-IN-PROMPT" not in system_prompt


def test_chat_foreign_and_unknown_file_ids_silently_skipped(
    client, monkeypatch,
):
    """Another user's file_id (and a nonexistent one) must not leak
    into the prompt — no error, the turn proceeds without them."""
    alice = _register(client, "alice")
    bob = _register(client, "bob")
    doc = _create_doc(client, alice)
    _seed_upload(bob["user_id"], "bob-file", "secret.txt",
                 "BOB-SECRET-99321")

    mock = _mock_llm(monkeypatch, "好的。")
    r = client.post(
        f"/api/v1/docs/{doc['id']}/chat",
        json={
            "message": "用附件写一段",
            "attachments": [
                {"file_id": "bob-file", "name": "secret.txt"},
                {"file_id": "no-such-file", "name": "ghost.txt"},
            ],
        },
        headers=_auth(alice),
    )
    assert r.status_code == 200, r.text
    frames = _sse_frames(r.text)
    assert frames[-1]["type"] == "done"

    system_prompt = mock.call_args[0][1]
    assert "BOB-SECRET-99321" not in system_prompt
    assert "## 用户附件" not in system_prompt


def test_chat_attachment_numbers_not_provenance_warned(
    client, monkeypatch,
):
    """Numbers the model copies FROM an attachment are sourced —
    they must not trip the hallucination guard."""
    user = _register(client, "alice")
    doc = _create_doc(client, user)
    _seed_upload(user["user_id"], "file-labs", "labs.txt",
                 "血红蛋白 8.5，白细胞 14。")

    _mock_llm(monkeypatch,
              "好的。<doc>基线：血红蛋白 8.5，白细胞 14。</doc>")
    r = client.post(
        f"/api/v1/docs/{doc['id']}/chat",
        json={"message": "按附件写基线",
              "attachments": [{"file_id": "file-labs", "name": "labs.txt"}]},
        headers=_auth(user),
    )
    assert r.status_code == 200, r.text
    frames = _sse_frames(r.text)
    types = [f["type"] for f in frames]
    assert "provenance_warning" not in types
    assert types[-1] == "done"


def test_chat_deleting_doc_clears_transcript(client, monkeypatch):
    user = _register(client, "alice")
    doc = _create_doc(client, user)
    _mock_llm(monkeypatch, "好的。")
    _chat(client, user, doc["id"], "你好")

    r = client.delete(f"/api/v1/docs/{doc['id']}", headers=_auth(user))
    assert r.status_code == 200

    from nexus_server.database import get_db_connection
    with get_db_connection() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM doc_chat_messages WHERE doc_id = ?",
            (doc["id"],),
        ).fetchone()[0]
    assert n == 0
