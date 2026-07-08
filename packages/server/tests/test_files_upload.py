"""Regression for the desktop's file upload path.

The /api/v1/files/upload endpoint silently went 404 in production
because nexus_server.main forgot to ``include_router(files.router)``
during a rename — the route was reachable in tests that imported the
router directly but the live FastAPI app didn't know it existed. The
desktop showed users a confusing ``Skipped: foo.pdf (Response status
code does not indicate success: 404 (Not Found).)`` for every drag-
dropped file.

These tests guard the wiring + the basic upload contract so that
regression can't happen quietly again.
"""

from __future__ import annotations

import io

import pytest


def _register(client) -> str:
    reg = client.post("/api/v1/auth/register", json={"display_name": "Uploader"})
    assert reg.status_code in (200, 201), reg.text
    return reg.json()["jwt_token"]


def test_files_upload_route_is_mounted(app):
    """The route MUST be reachable on the live FastAPI app — the bug
    that broke the desktop was main.create_app forgetting to
    include_router(files.router). A direct import of files would have
    masked it. This test fails iff the wiring is missing."""
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/v1/files/upload" in paths, (
        "/api/v1/files/upload not registered — desktop attachment "
        "uploads will all 404. Check nexus_server/main.py "
        "include_router(files.router)."
    )


def test_upload_round_trip(client):
    """End-to-end: authenticated user POSTs a small file, gets back
    a file_id + mime + size_bytes."""
    token = _register(client)
    body = b"%PDF-1.4 fake content for test\n%%EOF\n"
    files = {"file": ("test.pdf", io.BytesIO(body), "application/pdf")}
    resp = client.post(
        "/api/v1/files/upload",
        files=files,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body_json = resp.json()
    assert body_json["file_id"]
    assert body_json["name"] == "test.pdf"
    assert body_json["mime"].startswith("application/pdf")
    assert body_json["size_bytes"] == len(body)


def test_upload_requires_auth(client):
    """Anonymous upload → 401/403, never silently accepts."""
    resp = client.post(
        "/api/v1/files/upload",
        files={"file": ("anon.txt", io.BytesIO(b"xx"), "text/plain")},
    )
    assert resp.status_code in (401, 403)


def test_upload_rejects_oversize(client, monkeypatch):
    """Bytes over the per-request cap → 413, not a partial write."""
    from nexus_server import files as files_mod
    # Cap at 8 bytes for this test so we don't actually allocate MB.
    monkeypatch.setattr(files_mod, "MAX_FILE_BYTES", 8, raising=False)

    token = _register(client)
    payload = b"x" * 64
    resp = client.post(
        "/api/v1/files/upload",
        files={"file": ("big.bin", io.BytesIO(payload), "application/octet-stream")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 413


def test_resolve_files_returns_uploaded_bytes(client):
    """Server-side resolve_files() must return the actual on-disk
    bytes for a freshly-uploaded file id. This is the contract the
    chat handler depends on when the desktop sends an attachment by
    file_id; if it ever returns empty / wrong-user / missing rows,
    the LLM sees an empty payload and tells the user "your PDF is
    empty"."""
    from nexus_server import files as files_mod
    token = _register(client)
    body = b"a brief PDF-shaped payload\n%%EOF\n"
    resp = client.post(
        "/api/v1/files/upload",
        files={"file": ("paper.pdf", io.BytesIO(body), "application/pdf")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    file_id = resp.json()["file_id"]

    # Discover the uploading user's id from /chain/me — we don't
    # decode the JWT here.
    me = client.get(
        "/api/v1/chain/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    user_id = me.json()["user_id"]

    rows = files_mod.resolve_files(user_id, [file_id])
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "paper.pdf"
    assert row["mime"].startswith("application/pdf")
    assert row["size_bytes"] == len(body)

    raw = files_mod.read_file_bytes(row["disk_path"])
    assert raw == body, "On-disk bytes must equal what the client uploaded"


def test_resolve_files_scopes_to_owner(client):
    """A user must NOT be able to resolve someone else's file_id.
    Belt-and-braces against a desktop bug accidentally leaking
    another user's attachment by guessing an id."""
    from nexus_server import files as files_mod
    # User A uploads.
    tok_a = _register(client)
    resp = client.post(
        "/api/v1/files/upload",
        files={"file": ("a.txt", io.BytesIO(b"private"), "text/plain")},
        headers={"Authorization": f"Bearer {tok_a}"},
    )
    file_id = resp.json()["file_id"]

    # User B looks it up. resolve_files must come back empty.
    tok_b = _register(client)
    me_b = client.get(
        "/api/v1/chain/me",
        headers={"Authorization": f"Bearer {tok_b}"},
    )
    user_b = me_b.json()["user_id"]

    rows = files_mod.resolve_files(user_b, [file_id])
    assert rows == [], (
        "resolve_files must scope to the owner; a different user "
        "should not be able to read someone else's upload"
    )


def test_upload_persists_sha256_and_extracted_text_columns(client):
    """The file store relies on columns on the uploads table:
    ``sha256`` (integrity hash) and ``extracted_text`` (cache for the
    text projection); ``gnfd_path`` survives as a legacy column. This
    test confirms a fresh upload populates ``sha256`` immediately and
    leaves ``extracted_text`` empty until first read.
    """
    from nexus_server.database import get_db_connection
    token = _register(client)
    body = b"hello three layer storage"
    resp = client.post(
        "/api/v1/files/upload",
        files={"file": ("note.txt", io.BytesIO(body), "text/plain")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    file_id = resp.json()["file_id"]
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT sha256, gnfd_path, extracted_text FROM uploads "
            "WHERE file_id = ?",
            (file_id,),
        ).fetchone()
    assert row is not None
    sha256, gnfd_path, extracted_text = row
    assert len(sha256) == 64, "sha256 hex must be 64 chars"
    # extracted_text only fills on the first read_uploaded_file call.
    assert extracted_text == ""
    # gnfd_path is a legacy column — always empty for new rows, but it
    # must exist and round-trip without crashing.
    assert isinstance(gnfd_path, str)


@pytest.mark.asyncio
async def test_resolve_file_text_round_trip_via_layer_2(client):
    """End-to-end Layer 2 hit: upload a text file, then call the
    resolver — the SDK ReadUploadedFileTool delegates here, and this
    is what makes "summarise the paper I uploaded earlier" work
    across turns / twin eviction. The first call extracts from disk;
    a second call must hit the Layer 1 SQL cache (extracted_text)
    instead of re-reading disk.
    """
    from nexus_server import files as files_mod
    token = _register(client)
    body = b"The quick brown fox jumps over the lazy dog.\n" * 20
    resp = client.post(
        "/api/v1/files/upload",
        files={"file": ("essay.txt", io.BytesIO(body), "text/plain")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    me = client.get(
        "/api/v1/chain/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    user_id = me.json()["user_id"]

    # First call: text isn't cached yet → Layer 2 (disk) extracts.
    hit1 = await files_mod.resolve_file_text(user_id, "essay.txt")
    assert hit1 is not None, "Layer 2 must extract from disk on first call"
    name1, text1 = hit1
    assert name1 == "essay.txt"
    assert "quick brown fox" in text1

    # SQL row should now have extracted_text cached.
    from nexus_server.database import get_db_connection
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT extracted_text FROM uploads WHERE user_id = ? "
            "AND name = ?",
            (user_id, "essay.txt"),
        ).fetchone()
    assert row is not None and row[0], (
        "first resolve must populate extracted_text (Layer 1 cache) "
        "for O(1) reads on subsequent turns"
    )

    # Second call: must hit Layer 1.
    hit2 = await files_mod.resolve_file_text(user_id, "essay.txt")
    assert hit2 is not None
    assert hit2[1] == text1


@pytest.mark.asyncio
async def test_resolve_file_text_substring_match(client):
    """The tool surface accepts partial filenames (the LLM often
    omits extensions). Resolver must still find the row.
    """
    from nexus_server import files as files_mod
    token = _register(client)
    resp = client.post(
        "/api/v1/files/upload",
        files={"file": (
            "2024-q4-revenue-report.txt",
            io.BytesIO(b"revenue numbers"),
            "text/plain",
        )},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    me = client.get(
        "/api/v1/chain/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    user_id = me.json()["user_id"]

    # User asks for "revenue report" — substring of the real name.
    hit = await files_mod.resolve_file_text(user_id, "revenue-report")
    assert hit is not None
    assert hit[0] == "2024-q4-revenue-report.txt"


@pytest.mark.asyncio
async def test_resolve_file_text_returns_none_for_missing(client):
    """Unknown filename → None so the tool can format a helpful
    error listing what IS available."""
    from nexus_server import files as files_mod
    token = _register(client)
    me = client.get(
        "/api/v1/chain/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    user_id = me.json()["user_id"]
    hit = await files_mod.resolve_file_text(user_id, "nope.pdf")
    assert hit is None


def test_list_user_files_after_upload(client):
    """The file_reader's "list available files" surface must reflect
    what's actually in the SQL store — the LLM uses this to suggest
    candidate filenames when its first guess misses."""
    from nexus_server import files as files_mod
    token = _register(client)
    client.post(
        "/api/v1/files/upload",
        files={"file": ("a.txt", io.BytesIO(b"hi"), "text/plain")},
        headers={"Authorization": f"Bearer {token}"},
    )
    client.post(
        "/api/v1/files/upload",
        files={"file": ("b.txt", io.BytesIO(b"there"), "text/plain")},
        headers={"Authorization": f"Bearer {token}"},
    )
    me = client.get(
        "/api/v1/chain/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    user_id = me.json()["user_id"]
    listing = files_mod.list_user_files(user_id)
    assert "a.txt" in listing
    assert "b.txt" in listing


@pytest.mark.asyncio
async def test_resolve_scopes_to_owner(client):
    """User B must not be able to read User A's file via the
    resolver — same security guarantee as resolve_files() but on
    the new path."""
    from nexus_server import files as files_mod
    tok_a = _register(client)
    client.post(
        "/api/v1/files/upload",
        files={"file": ("private.txt", io.BytesIO(b"secret"), "text/plain")},
        headers={"Authorization": f"Bearer {tok_a}"},
    )

    tok_b = _register(client)
    me_b = client.get(
        "/api/v1/chain/me",
        headers={"Authorization": f"Bearer {tok_b}"},
    )
    user_b = me_b.json()["user_id"]

    hit = await files_mod.resolve_file_text(user_b, "private.txt")
    assert hit is None, (
        "resolve_file_text must scope by user_id — a different user "
        "must not be able to read someone else's upload by guessing "
        "the filename"
    )


def test_chat_request_with_file_id_is_well_formed(client):
    """Light contract check: the desktop's chat payload includes
    file_id (not just inline bytes), and the server's Pydantic model
    accepts it. Catches the desktop's regression where SendChatAsync
    forgot to thread FileId through into the wire payload — that
    silently dropped the reference, server's resolve_files() came
    back empty, and the LLM saw an empty attachment."""
    from nexus_server.llm_gateway import LLMChatRequest
    payload = {
        "messages": [{"role": "user", "content": "summarise"}],
        "attachments": [{
            "name": "paper.pdf",
            "mime": "application/pdf",
            "size_bytes": 1234,
            "file_id": "abc123",
            "content_text": None,
            "content_base64": None,
        }],
    }
    parsed = LLMChatRequest.model_validate(payload)
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].file_id == "abc123", (
        "file_id MUST round-trip through the request schema — if it "
        "gets dropped here, the desktop's attachment-by-id flow is broken"
    )
