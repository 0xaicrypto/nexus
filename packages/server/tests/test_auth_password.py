"""Password auth redesign (2026-07) — contract tests.

Covers:
  * register: first user = admin, later users = user, unique username,
    password policy
  * login: happy path, wrong password, unknown user, disabled account
  * claim: one-time password set on legacy (password_hash NULL) accounts
  * admin: list / disable / enable / reset-password, non-admin 403,
    self-disable blocked
  * get_current_user: JWTs of disabled users rejected
  * rate limiting: 5/min per (ip, username) on login
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

PW = "Str0ng-Pass-123"


def _register(client, name, password=PW, expect=201):
    r = client.post("/api/v1/auth/register",
                    json={"username": name, "password": password})
    assert r.status_code == expect, r.text
    return r.json()


def _login(client, name, password=PW):
    return client.post("/api/v1/auth/login",
                       json={"username": name, "password": password})


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _insert_legacy_user(name):
    """Simulate a pre-redesign account: no password_hash."""
    from nexus_server.database import get_db_connection
    user_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO users (id, display_name, jwt_secret, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, name, str(uuid.uuid4()), now, now),
        )
        conn.commit()
    return user_id


def _error_code(resp):
    body = resp.json()
    err = body.get("error", body.get("detail"))
    if isinstance(err, dict):
        return err.get("code")
    return None


# ─────────────────────────────────────────────────────────────────────
# Register
# ─────────────────────────────────────────────────────────────────────


def test_first_user_is_admin_second_is_user(client):
    a = _register(client, "alice")
    b = _register(client, "bob")
    assert a["role"] == "admin"
    assert b["role"] == "user"
    assert a["user_id"] != b["user_id"]
    assert a["jwt_token"] and b["jwt_token"]


def test_register_duplicate_username_409(client):
    _register(client, "alice")
    r = client.post("/api/v1/auth/register",
                    json={"username": "alice", "password": PW})
    assert r.status_code == 409
    assert _error_code(r) == "username_taken"
    # Case-insensitive uniqueness.
    r2 = client.post("/api/v1/auth/register",
                     json={"username": "ALICE", "password": PW})
    assert r2.status_code == 409


def test_register_password_policy(client):
    # Too short.
    r = client.post("/api/v1/auth/register",
                    json={"username": "u1", "password": "short"})
    assert r.status_code == 422
    # Common password.
    r = client.post("/api/v1/auth/register",
                    json={"username": "u1", "password": "password123"})
    assert r.status_code == 422
    # Whitespace-only.
    r = client.post("/api/v1/auth/register",
                    json={"username": "u1", "password": "        "})
    assert r.status_code == 422
    # Missing entirely.
    r = client.post("/api/v1/auth/register", json={"username": "u1"})
    assert r.status_code == 422


def test_register_token_works_on_protected_endpoint(client):
    a = _register(client, "alice")
    r = client.get("/api/v1/sessions", headers=_auth(a["jwt_token"]))
    assert r.status_code == 200, r.text


# ─────────────────────────────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────────────────────────────


def test_login_ok(client):
    _register(client, "alice")
    r = _login(client, "alice")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jwt_token"]
    assert body["role"] == "admin"
    assert body["expires_in_seconds"] > 0


def test_login_wrong_password_401(client):
    _register(client, "alice")
    r = _login(client, "alice", "Wrong-Pass-999")
    assert r.status_code == 401
    assert _error_code(r) == "invalid_credentials"


def test_login_unknown_user_401(client):
    r = _login(client, "nobody")
    assert r.status_code == 401
    assert _error_code(r) == "invalid_credentials"


def test_login_case_insensitive_username(client):
    a = _register(client, "Doctor Jin")
    r = _login(client, "doctor jin")
    assert r.status_code == 200
    assert r.json()["user_id"] == a["user_id"]


# ─────────────────────────────────────────────────────────────────────
# Claim (legacy passwordless accounts)
# ─────────────────────────────────────────────────────────────────────


def test_login_legacy_user_409_claim_required(client):
    _insert_legacy_user("金医生")
    r = _login(client, "金医生")
    assert r.status_code == 409
    assert _error_code(r) == "claim_required"


def test_claim_flow(client):
    uid = _insert_legacy_user("金医生")
    r = client.post("/api/v1/auth/claim",
                    json={"username": "金医生", "password": PW})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"] == uid
    assert body["jwt_token"]
    # Token works.
    me = client.get("/api/v1/sessions", headers=_auth(body["jwt_token"]))
    assert me.status_code == 200
    # Now login works too.
    assert _login(client, "金医生").status_code == 200
    # Second claim is rejected.
    r2 = client.post("/api/v1/auth/claim",
                     json={"username": "金医生", "password": PW})
    assert r2.status_code == 409
    assert _error_code(r2) == "already_claimed"


def test_claim_unknown_user_404(client):
    r = client.post("/api/v1/auth/claim",
                    json={"username": "ghost", "password": PW})
    assert r.status_code == 404


def test_claim_on_password_account_409(client):
    _register(client, "alice")
    r = client.post("/api/v1/auth/claim",
                    json={"username": "alice", "password": PW})
    assert r.status_code == 409
    assert _error_code(r) == "already_claimed"


# ─────────────────────────────────────────────────────────────────────
# Admin endpoints
# ─────────────────────────────────────────────────────────────────────


def test_admin_list_users(client):
    a = _register(client, "admin1")
    _register(client, "bob")
    _insert_legacy_user("legacy")
    r = client.get("/api/v1/admin/users", headers=_auth(a["jwt_token"]))
    assert r.status_code == 200, r.text
    users = {u["username"]: u for u in r.json()["users"]}
    assert users["admin1"]["role"] == "admin"
    assert users["bob"]["role"] == "user"
    assert users["admin1"]["has_password"] is True
    assert users["legacy"]["has_password"] is False
    assert users["admin1"]["last_login_at"] is not None
    assert users["bob"]["disabled_at"] is None


def test_non_admin_gets_403(client):
    _register(client, "admin1")
    b = _register(client, "bob")
    hdrs = _auth(b["jwt_token"])
    assert client.get("/api/v1/admin/users", headers=hdrs).status_code == 403
    assert client.post(
        f"/api/v1/admin/users/{b['user_id']}/disable", headers=hdrs,
    ).status_code == 403
    assert client.post(
        f"/api/v1/admin/users/{b['user_id']}/reset-password",
        json={"new_password": PW}, headers=hdrs,
    ).status_code == 403


def test_admin_endpoints_require_auth(client):
    assert client.get("/api/v1/admin/users").status_code == 401


def test_disable_enable_flow(client):
    a = _register(client, "admin1")
    b = _register(client, "bob")
    hdrs = _auth(a["jwt_token"])
    bob_hdrs = _auth(b["jwt_token"])

    # Bob works before disable.
    assert client.get("/api/v1/sessions",
                      headers=bob_hdrs).status_code == 200

    r = client.post(f"/api/v1/admin/users/{b['user_id']}/disable",
                    headers=hdrs)
    assert r.status_code == 200, r.text
    assert r.json()["disabled_at"] is not None

    # Disabled: existing JWT rejected, login rejected.
    assert client.get("/api/v1/sessions",
                      headers=bob_hdrs).status_code == 403
    lr = _login(client, "bob")
    assert lr.status_code == 403
    assert _error_code(lr) == "account_disabled"

    # Re-enable → both work again.
    r = client.post(f"/api/v1/admin/users/{b['user_id']}/enable",
                    headers=hdrs)
    assert r.status_code == 200
    assert client.get("/api/v1/sessions",
                      headers=bob_hdrs).status_code == 200
    assert _login(client, "bob").status_code == 200


def test_admin_cannot_disable_self(client):
    a = _register(client, "admin1")
    r = client.post(f"/api/v1/admin/users/{a['user_id']}/disable",
                    headers=_auth(a["jwt_token"]))
    assert r.status_code == 400
    assert _error_code(r) == "cannot_disable_self"


def test_admin_reset_password(client):
    a = _register(client, "admin1")
    b = _register(client, "bob")
    new_pw = "N3w-Pass-456!"
    r = client.post(f"/api/v1/admin/users/{b['user_id']}/reset-password",
                    json={"new_password": new_pw},
                    headers=_auth(a["jwt_token"]))
    assert r.status_code == 200, r.text
    assert _login(client, "bob", PW).status_code == 401
    assert _login(client, "bob", new_pw).status_code == 200
    # Weak replacement rejected.
    r = client.post(f"/api/v1/admin/users/{b['user_id']}/reset-password",
                    json={"new_password": "short"},
                    headers=_auth(a["jwt_token"]))
    assert r.status_code == 422


def test_admin_actions_on_unknown_user_404(client):
    a = _register(client, "admin1")
    hdrs = _auth(a["jwt_token"])
    assert client.post("/api/v1/admin/users/nope/disable",
                       headers=hdrs).status_code == 404
    assert client.post("/api/v1/admin/users/nope/enable",
                       headers=hdrs).status_code == 404
    assert client.post("/api/v1/admin/users/nope/reset-password",
                       json={"new_password": PW},
                       headers=hdrs).status_code == 404


# ─────────────────────────────────────────────────────────────────────
# Removed legacy endpoints
# ─────────────────────────────────────────────────────────────────────


def test_legacy_passwordless_endpoints_are_gone(client):
    assert client.post("/api/v1/auth/login-by-name",
                       json={"display_name": "x"}).status_code == 404
    assert client.post("/api/v1/auth/local-bootstrap").status_code == 404
    # GET /identities (read-only picker list) survives, so POST on the
    # same path is 405; the JWT-minting create endpoint is gone.
    assert client.post("/api/v1/auth/identities",
                       json={"display_name": "x"}).status_code in (404, 405)
    assert client.post(
        "/api/v1/auth/identities/some-id/activate").status_code == 404
    # Old passwordless register shape no longer accepted.
    assert client.post("/api/v1/auth/register",
                       json={"display_name": "x"}).status_code == 422
    # Old user_id-based login shape no longer accepted.
    assert client.post("/api/v1/auth/login",
                       json={"user_id": "abc"}).status_code == 422


# ─────────────────────────────────────────────────────────────────────
# Rate limiting
# ─────────────────────────────────────────────────────────────────────


def test_login_rate_limited(client, monkeypatch):
    from nexus_server.auth import routes as auth_routes
    _register(client, "alice")
    monkeypatch.delenv("NEXUS_AUTH_RATELIMIT_DISABLED", raising=False)
    auth_routes._AUTH_ATTEMPTS.clear()
    try:
        codes = [
            _login(client, "alice", "Wrong-Pass-999").status_code
            for _ in range(6)
        ]
        assert codes[:5] == [401] * 5
        assert codes[5] == 429
        # Other usernames are unaffected (per-username bucket).
        r = _login(client, "bob")
        assert r.status_code != 429
    finally:
        auth_routes._AUTH_ATTEMPTS.clear()
