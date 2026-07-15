"""Writing Studio (P1) — contract tests.

Covers P1 scope:
  * docs CRUD + per-user isolation
  * version snapshot on body-changing save + restore
  * @ patient reference → FROZEN de-identified snapshot (planted name
    must NOT appear) + DOC_REFERENCE_CREATED audit event
  * phi-scan flags planted patient name / exact date / phone
  * export blocks on unresolved PHI (422 phi_unresolved), succeeds
    after resolutions with a valid .docx (zip magic bytes) and expands
    {{ref:ID}} chips
  * polish SSE stream frames with a mocked LLM (revised_chunk →
    provenance_warning → done)
"""
from __future__ import annotations

import io
import json
import zipfile
from unittest.mock import AsyncMock

import pytest

PW = "Str0ng-Pass-123"


# ─────────────────────────────────────────────────────────────────────
# Helpers
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


def _sse_frames(body_text: str) -> list[dict]:
    frames = []
    for block in body_text.split("\n\n"):
        block = block.strip()
        if block.startswith("data: "):
            frames.append(json.loads(block[len("data: "):]))
    return frames


# ─────────────────────────────────────────────────────────────────────
# CRUD + isolation
# ─────────────────────────────────────────────────────────────────────


def test_docs_crud_roundtrip(client):
    user = _register(client, "alice")

    # Empty list at first.
    r = client.get("/api/v1/docs", headers=_auth(user))
    assert r.status_code == 200
    assert r.json()["docs"] == []

    doc = _create_doc(client, user, title="NSCLC 病例")
    assert doc["title"] == "NSCLC 病例"
    assert doc["body"] == ""

    # List shows it with ref_count 0.
    r = client.get("/api/v1/docs", headers=_auth(user))
    docs = r.json()["docs"]
    assert len(docs) == 1
    assert docs[0]["id"] == doc["id"]
    assert docs[0]["ref_count"] == 0
    assert "updated_at" in docs[0]

    # Update title + body.
    r = client.put(
        f"/api/v1/docs/{doc['id']}",
        json={"title": "新标题", "body": "正文第一版"},
        headers=_auth(user),
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["snapshot_created"] is True

    r = client.get(f"/api/v1/docs/{doc['id']}", headers=_auth(user))
    got = r.json()
    assert got["title"] == "新标题"
    assert got["body"] == "正文第一版"
    assert got["references"] == []

    # Delete.
    r = client.delete(f"/api/v1/docs/{doc['id']}", headers=_auth(user))
    assert r.status_code == 200
    r = client.get(f"/api/v1/docs/{doc['id']}", headers=_auth(user))
    assert r.status_code == 404


def test_docs_user_isolation(client):
    alice = _register(client, "alice")
    bob = _register(client, "bob")

    doc = _create_doc(client, alice, title="alice 的文档")

    # Bob can't read / update / delete / scan alice's doc.
    assert client.get(
        f"/api/v1/docs/{doc['id']}", headers=_auth(bob),
    ).status_code == 404
    assert client.put(
        f"/api/v1/docs/{doc['id']}", json={"body": "hijack"},
        headers=_auth(bob),
    ).status_code == 404
    assert client.delete(
        f"/api/v1/docs/{doc['id']}", headers=_auth(bob),
    ).status_code == 404
    assert client.post(
        f"/api/v1/docs/{doc['id']}/phi-scan", json={}, headers=_auth(bob),
    ).status_code == 404

    # Bob's list doesn't leak it.
    r = client.get("/api/v1/docs", headers=_auth(bob))
    assert r.json()["docs"] == []

    # Alice still sees her doc untouched.
    r = client.get(f"/api/v1/docs/{doc['id']}", headers=_auth(alice))
    assert r.status_code == 200
    assert r.json()["body"] == ""

    # No token at all → 401/403.
    assert client.get("/api/v1/docs").status_code in (401, 403)


# ─────────────────────────────────────────────────────────────────────
# Snapshots
# ─────────────────────────────────────────────────────────────────────


def test_snapshot_on_save_and_restore(client):
    user = _register(client, "alice")
    doc = _create_doc(client, user)

    client.put(f"/api/v1/docs/{doc['id']}", json={"body": "版本一"},
               headers=_auth(user))
    client.put(f"/api/v1/docs/{doc['id']}", json={"body": "版本二"},
               headers=_auth(user))
    # Title-only save must NOT create a snapshot.
    r = client.put(f"/api/v1/docs/{doc['id']}", json={"title": "改名"},
                   headers=_auth(user))
    assert r.json()["snapshot_created"] is False
    # Same-body save must NOT create a snapshot either.
    r = client.put(f"/api/v1/docs/{doc['id']}", json={"body": "版本二"},
                   headers=_auth(user))
    assert r.json()["snapshot_created"] is False

    r = client.get(f"/api/v1/docs/{doc['id']}/snapshots",
                   headers=_auth(user))
    snaps = r.json()["snapshots"]
    assert len(snaps) == 2  # latest first
    oldest = snaps[-1]

    # Restore the first version.
    r = client.post(
        f"/api/v1/docs/{doc['id']}/snapshots/{oldest['id']}/restore",
        headers=_auth(user),
    )
    assert r.status_code == 200
    assert r.json()["body"] == "版本一"

    r = client.get(f"/api/v1/docs/{doc['id']}", headers=_auth(user))
    assert r.json()["body"] == "版本一"

    # The restore itself lands in the version chain.
    r = client.get(f"/api/v1/docs/{doc['id']}/snapshots",
                   headers=_auth(user))
    assert len(r.json()["snapshots"]) == 3

    # Restoring a bogus snapshot id → 404.
    r = client.post(
        f"/api/v1/docs/{doc['id']}/snapshots/999999/restore",
        headers=_auth(user),
    )
    assert r.status_code == 404


def test_snapshot_cap_keeps_latest_50(client):
    user = _register(client, "alice")
    doc = _create_doc(client, user)
    for i in range(55):
        client.put(f"/api/v1/docs/{doc['id']}", json={"body": f"v{i}"},
                   headers=_auth(user))
    r = client.get(f"/api/v1/docs/{doc['id']}/snapshots",
                   headers=_auth(user))
    snaps = r.json()["snapshots"]
    assert len(snaps) == 50


# ─────────────────────────────────────────────────────────────────────
# References — de-identification + audit
# ─────────────────────────────────────────────────────────────────────


def test_patient_reference_is_deidentified_and_audited(client):
    user = _register(client, "alice")
    patient_hash = _register_patient(client, user, initials="张三丰")
    doc = _create_doc(client, user)

    r = client.post(
        f"/api/v1/docs/{doc['id']}/references",
        json={
            "ref_type": "patient", "target_id": patient_hash,
            "granularity": "basics",
        },
        headers=_auth(user),
    )
    assert r.status_code == 200, r.text
    out = r.json()
    code = "P-" + patient_hash[:6]

    assert out["ref_id"]
    assert "张三丰" not in out["snapshot_preview"]
    assert code in out["snapshot_preview"]
    assert code in out["chip_label"]
    # Age survives (birthdate→age rule) — we registered 57.
    assert "57" in out["snapshot_preview"]

    # The stored FROZEN snapshot is de-identified too.
    from nexus_server.database import get_db_connection
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT snapshot, ref_type, target_id, granularity "
            "FROM doc_references WHERE id = ?",
            (out["ref_id"],),
        ).fetchone()
    assert row is not None
    assert "张三丰" not in row[0]
    assert code in row[0]
    assert (row[1], row[2], row[3]) == ("patient", patient_hash, "basics")

    # Audit event landed in twin_event_log.
    with get_db_connection() as conn:
        audit = conn.execute(
            "SELECT user_id, patient_hash, payload_json "
            "FROM twin_event_log WHERE event_kind = 'doc_reference' "
            "ORDER BY event_idx DESC LIMIT 1",
        ).fetchone()
    assert audit is not None
    assert audit[0] == user["user_id"]
    assert audit[1] == patient_hash
    payload = json.loads(audit[2])
    assert payload["doc_id"] == doc["id"]
    assert payload["ref_id"] == out["ref_id"]
    assert payload["ref_type"] == "patient"
    assert payload["granularity"] == "basics"

    # GET doc lists the reference with a chip label.
    r = client.get(f"/api/v1/docs/{doc['id']}", headers=_auth(user))
    refs = r.json()["references"]
    assert len(refs) == 1
    assert refs[0]["ref_id"] == out["ref_id"]
    assert code in refs[0]["chip_label"]
    assert "张三丰" not in refs[0]["snapshot"]

    # And the list view counts it.
    r = client.get("/api/v1/docs", headers=_auth(user))
    assert r.json()["docs"][0]["ref_count"] == 1


def test_timeline_reference_missing_graph_data_is_defensive(client):
    """No clinical_graph rows for this patient → shorter snapshot,
    never a 500."""
    user = _register(client, "alice")
    patient_hash = _register_patient(client, user, initials="李四")
    doc = _create_doc(client, user)
    r = client.post(
        f"/api/v1/docs/{doc['id']}/references",
        json={
            "ref_type": "patient", "target_id": patient_hash,
            "granularity": "timeline",
        },
        headers=_auth(user),
    )
    assert r.status_code == 200, r.text
    assert "李四" not in r.json()["snapshot_preview"]


def test_reference_bogus_target_404s(client):
    user = _register(client, "alice")
    doc = _create_doc(client, user)
    r = client.post(
        f"/api/v1/docs/{doc['id']}/references",
        json={"ref_type": "file", "target_id": "no-such-file",
              "granularity": "summary"},
        headers=_auth(user),
    )
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────
# PHI scan
# ─────────────────────────────────────────────────────────────────────


def test_phi_scan_flags_name_date_phone(client):
    user = _register(client, "alice")
    patient_hash = _register_patient(client, user, initials="张三丰")
    doc = _create_doc(client, user)

    body = (
        "患者张三丰于2025年3月14日入院，联系电话13812345678，"
        "行胸部CT提示右上肺占位。"
    )
    client.put(f"/api/v1/docs/{doc['id']}", json={"body": body},
               headers=_auth(user))

    r = client.post(f"/api/v1/docs/{doc['id']}/phi-scan", json={},
                    headers=_auth(user))
    assert r.status_code == 200, r.text
    findings = r.json()["findings"]
    kinds = {f["kind"] for f in findings}
    assert {"patient_name", "exact_date", "phone"} <= kinds

    name_f = next(f for f in findings if f["kind"] == "patient_name")
    assert name_f["excerpt"] == "张三丰"
    assert body[name_f["start"]:name_f["end"]] == "张三丰"
    assert "P-" + patient_hash[:6] in name_f["suggestion"]

    date_f = next(f for f in findings if f["kind"] == "exact_date")
    assert date_f["suggestion"] == "改为相对时间"
    assert "2025" in date_f["excerpt"]


def test_phi_scan_skips_ref_placeholders_and_clean_text(client):
    user = _register(client, "alice")
    doc = _create_doc(client, user)
    # The placeholder id contains a date-shaped run (2025-11-22) that
    # must NOT be flagged — chip content was de-identified at insert.
    body = "本队列客观缓解率 62%。{{ref:2025-11-22-aaaa-bbbbccccdddd}}"
    client.put(f"/api/v1/docs/{doc['id']}", json={"body": body},
               headers=_auth(user))
    r = client.post(f"/api/v1/docs/{doc['id']}/phi-scan", json={},
                    headers=_auth(user))
    assert r.json()["findings"] == []


# ─────────────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────────────


def test_export_blocks_on_unresolved_phi_then_succeeds(client):
    user = _register(client, "alice")
    patient_hash = _register_patient(client, user, initials="张三丰")
    code = "P-" + patient_hash[:6]
    doc = _create_doc(client, user, title="脱敏导出测试")

    # Insert a reference chip so we can verify expansion + appendix.
    r = client.post(
        f"/api/v1/docs/{doc['id']}/references",
        json={"ref_type": "patient", "target_id": patient_hash,
              "granularity": "basics"},
        headers=_auth(user),
    )
    ref_id = r.json()["ref_id"]

    body = (
        "病例摘要：张三丰，2025年3月14日入院。\n\n"
        f"基线特征：{{{{ref:{ref_id}}}}}\n\n"
        "结论：治疗有效。"
    )
    client.put(f"/api/v1/docs/{doc['id']}", json={"body": body},
               headers=_auth(user))

    # 1. Unresolved PHI → 422 with findings echoed back.
    r = client.post(f"/api/v1/docs/{doc['id']}/export", json={},
                    headers=_auth(user))
    assert r.status_code == 422, r.text
    payload = r.json()
    assert payload["code"] == "phi_unresolved"
    assert payload["findings"]
    kinds = {f["kind"] for f in payload["findings"]}
    assert {"patient_name", "exact_date"} <= kinds

    # 2. Resolve each finding (replace name with code, date with
    #    relative time) → export succeeds with a real .docx.
    resolutions = []
    for f in payload["findings"]:
        resolutions.append({
            **f,
            "replacement": code if f["kind"] == "patient_name" else "D0",
        })
    r = client.post(
        f"/api/v1/docs/{doc['id']}/export",
        json={"resolutions": resolutions, "include_sources": True},
        headers=_auth(user),
    )
    assert r.status_code == 200, r.text
    assert r.content[:2] == b"PK"  # zip magic — valid OOXML container
    assert "attachment" in r.headers.get("content-disposition", "")

    # Inspect the docx: chip expanded to the de-identified snapshot,
    # no raw name anywhere, sources appendix present.
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    assert "张三丰" not in xml
    assert code in xml                 # expanded snapshot + resolution
    assert "引用来源" in xml            # appendix
    assert "脱敏导出测试" in xml        # title heading
    assert "{{ref:" not in xml         # placeholder fully expanded


def test_export_clean_doc_needs_no_resolutions(client):
    user = _register(client, "alice")
    doc = _create_doc(client, user, title="干净文档")
    client.put(f"/api/v1/docs/{doc['id']}",
               json={"body": "无任何敏感信息的正文。"},
               headers=_auth(user))
    r = client.post(f"/api/v1/docs/{doc['id']}/export", json={},
                    headers=_auth(user))
    assert r.status_code == 200
    assert r.content[:2] == b"PK"


# ─────────────────────────────────────────────────────────────────────
# Polish (SSE, mocked LLM)
# ─────────────────────────────────────────────────────────────────────


def test_polish_streams_frames_with_provenance_warning(
    client, monkeypatch,
):
    user = _register(client, "alice")
    doc = _create_doc(client, user)

    revised_text = "本研究客观缓解率达85%，疗效显著。"
    mock = AsyncMock(return_value=(revised_text, "mock-model", "stop", []))
    monkeypatch.setattr("nexus_server.llm_gateway.call_llm", mock)

    r = client.post(
        f"/api/v1/docs/{doc['id']}/polish",
        json={"selection": "疗效不错。", "instruction": "更学术",
              "ref_ids": []},
        headers=_auth(user),
    )
    assert r.status_code == 200
    frames = _sse_frames(r.text)
    types = [f["type"] for f in frames]

    assert "revised_chunk" in types
    chunks = "".join(
        f["text"] for f in frames if f["type"] == "revised_chunk"
    )
    assert chunks == revised_text

    # '85%' has no source in selection (and no refs) → warned.
    warn = next(f for f in frames if f["type"] == "provenance_warning")
    assert "85%" in warn["numbers"]

    done = next(f for f in frames if f["type"] == "done")
    assert done["revised"] == revised_text
    # done must be the final frame.
    assert types[-1] == "done"

    # The mocked LLM saw the persona + grounding rule + instruction.
    args, _ = mock.call_args
    messages, system_prompt = args[0], args[1]
    assert messages[0]["content"] == "疗效不错。"
    assert "不得编造数值" in system_prompt
    assert "更学术" in system_prompt


def test_polish_numbers_grounded_in_ref_snapshot_not_warned(
    client, monkeypatch,
):
    user = _register(client, "alice")
    patient_hash = _register_patient(client, user, initials="王五",
                                     age=63)
    doc = _create_doc(client, user)
    r = client.post(
        f"/api/v1/docs/{doc['id']}/references",
        json={"ref_type": "patient", "target_id": patient_hash,
              "granularity": "basics"},
        headers=_auth(user),
    )
    ref_id = r.json()["ref_id"]

    # '63' comes from the ref snapshot (age) → allowed, no warning.
    mock = AsyncMock(
        return_value=("该 63 岁患者疗效良好。", "mock-model", "stop", []),
    )
    monkeypatch.setattr("nexus_server.llm_gateway.call_llm", mock)

    r = client.post(
        f"/api/v1/docs/{doc['id']}/polish",
        json={"selection": "患者疗效良好。", "instruction": "",
              "ref_ids": [ref_id]},
        headers=_auth(user),
    )
    frames = _sse_frames(r.text)
    types = [f["type"] for f in frames]
    assert "provenance_warning" not in types
    assert types[-1] == "done"

    # Ref snapshot was injected into the system prompt as context.
    args, _ = mock.call_args
    system_prompt = args[1]
    assert "P-" + patient_hash[:6] in system_prompt


def test_polish_llm_failure_yields_error_frame(client, monkeypatch):
    user = _register(client, "alice")
    doc = _create_doc(client, user)

    mock = AsyncMock(side_effect=RuntimeError("provider exploded"))
    monkeypatch.setattr("nexus_server.llm_gateway.call_llm", mock)

    r = client.post(
        f"/api/v1/docs/{doc['id']}/polish",
        json={"selection": "文本", "instruction": "", "ref_ids": []},
        headers=_auth(user),
    )
    assert r.status_code == 200  # error travels inside the stream
    frames = _sse_frames(r.text)
    assert frames[-1]["type"] == "error"
    assert "provider exploded" in frames[-1]["message"]


def test_polish_unknown_doc_404s(client):
    user = _register(client, "alice")
    r = client.post(
        "/api/v1/docs/nope/polish",
        json={"selection": "x", "instruction": "", "ref_ids": []},
        headers=_auth(user),
    )
    assert r.status_code == 404
