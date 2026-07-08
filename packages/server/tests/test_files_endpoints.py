"""Tests for the Files UI backend — list / preview / delete."""
from __future__ import annotations

import pytest


def _register(client) -> str:
    reg = client.post(
        "/api/v1/auth/register", json={"display_name": "FilesUser"},
    )
    return reg.json()["jwt_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_upload(user_id: str, **kwargs) -> str:
    """Insert a fake uploads row directly so we don't have to round-
    trip a multipart upload in every test."""
    from nexus_server import files as _files
    from nexus_server.database import get_db_connection
    _files._ensure_uploads_table()
    file_id = kwargs.pop("file_id", "test-" + user_id[:6])
    defaults = {
        "name": "memo.docx",
        "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "size_bytes": 12345,
        "disk_path": "/tmp/nonexistent",
        "created_at": "2026-05-20T10:00:00Z",
        "sha256": "deadbeef",
        "gnfd_path": "",
        "extracted_text": "Hello from the test file. " * 50,
    }
    defaults.update(kwargs)
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO uploads (file_id, user_id, name, mime, size_bytes,
                                 disk_path, created_at, sha256, gnfd_path,
                                 extracted_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id, user_id, defaults["name"], defaults["mime"],
                defaults["size_bytes"], defaults["disk_path"],
                defaults["created_at"], defaults["sha256"],
                defaults["gnfd_path"], defaults["extracted_text"],
            ),
        )
        conn.commit()
    return file_id


# ─────────────────────────────────────────────────────────────────────


def test_list_files_empty(client):
    token = _register(client)
    resp = client.get("/api/v1/files/list", headers=_h(token))
    assert resp.status_code == 200
    assert resp.json() == {"files": [], "total": 0}


def test_list_files_returns_newest_first(client):
    token = _register(client)
    # Resolve user_id from the JWT
    me = client.get("/api/v1/user/profile", headers=_h(token)).json()
    uid = me["user_id"]

    _seed_upload(uid, file_id="old", name="a.txt",
                 created_at="2026-01-01T00:00:00Z")
    _seed_upload(uid, file_id="mid", name="b.txt",
                 created_at="2026-03-01T00:00:00Z")
    _seed_upload(uid, file_id="new", name="c.txt",
                 created_at="2026-05-01T00:00:00Z")

    resp = client.get("/api/v1/files/list", headers=_h(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert [f["file_id"] for f in body["files"]] == ["new", "mid", "old"]


def test_list_files_includes_excerpt_and_has_text(client):
    token = _register(client)
    uid = client.get("/api/v1/user/profile", headers=_h(token)).json()["user_id"]

    _seed_upload(uid, file_id="rich",
                 extracted_text="The agentic commerce on BNB Chain " * 20)
    _seed_upload(uid, file_id="empty", extracted_text="")

    body = client.get("/api/v1/files/list", headers=_h(token)).json()
    by_id = {f["file_id"]: f for f in body["files"]}
    assert by_id["rich"]["has_text"] is True
    assert "agentic commerce" in by_id["rich"]["excerpt"].lower()
    assert by_id["empty"]["has_text"] is False
    assert by_id["empty"]["excerpt"] == ""


def test_list_files_scoped_to_user(client):
    token_a = _register(client)
    uid_a = client.get("/api/v1/user/profile", headers=_h(token_a)).json()["user_id"]
    _seed_upload(uid_a, file_id="for-a")
    # Different user — files MUST NOT leak.
    _seed_upload("some-other-uid", file_id="for-b")

    body = client.get("/api/v1/files/list", headers=_h(token_a)).json()
    assert [f["file_id"] for f in body["files"]] == ["for-a"]


def test_preview_returns_full_extracted_text(client):
    token = _register(client)
    uid = client.get("/api/v1/user/profile", headers=_h(token)).json()["user_id"]
    text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
    _seed_upload(uid, file_id="preview-target", extracted_text=text)

    resp = client.get(
        "/api/v1/files/preview-target/preview", headers=_h(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "memo.docx"
    assert body["extracted_text"] == text
    assert body["has_text"] is True
    assert body["text_truncated"] is False


def test_preview_truncates_very_long_text(client):
    token = _register(client)
    uid = client.get("/api/v1/user/profile", headers=_h(token)).json()["user_id"]
    # Force above the 100 KB cap
    long_text = "abcde" * 25_000  # 125 KB
    _seed_upload(uid, file_id="huge", extracted_text=long_text)

    body = client.get("/api/v1/files/huge/preview", headers=_h(token)).json()
    assert body["text_truncated"] is True
    assert len(body["extracted_text"]) <= 100 * 1024


def test_preview_404_for_other_users_file(client):
    token = _register(client)
    _seed_upload("some-other-uid", file_id="not-yours")
    resp = client.get("/api/v1/files/not-yours/preview", headers=_h(token))
    assert resp.status_code == 404


def test_delete_removes_file(client):
    token = _register(client)
    uid = client.get("/api/v1/user/profile", headers=_h(token)).json()["user_id"]
    _seed_upload(uid, file_id="toast")

    resp = client.delete("/api/v1/files/toast", headers=_h(token))
    assert resp.status_code == 204

    # List should now be empty
    after = client.get("/api/v1/files/list", headers=_h(token)).json()
    assert after["total"] == 0


def test_delete_404_for_unknown_id(client):
    token = _register(client)
    resp = client.delete("/api/v1/files/nope", headers=_h(token))
    assert resp.status_code == 404


def test_list_requires_auth(client):
    resp = client.get("/api/v1/files/list")
    assert resp.status_code in (401, 403)
