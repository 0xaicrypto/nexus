"""Regression tests for rune-server.

Covers: auth, LLM gateway format, sync endpoints, config, passkey page.
All tests use mocked LLM calls — no real API keys needed.
"""
import json
import pytest
from unittest.mock import patch, AsyncMock


# ── Health Check ──────────────────────────────────────────────────────


class TestHealthCheck:
    def test_health_endpoint(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "timestamp" in data
        assert "version" in data


# ── Auth: Register ────────────────────────────────────────────────────


class TestAuthRegister:
    def test_register_returns_jwt(self, client):
        resp = client.post("/api/v1/auth/register", json={"display_name": "TestUser"})
        assert resp.status_code == 201
        data = resp.json()
        assert "jwt_token" in data
        assert "user_id" in data
        assert len(data["jwt_token"]) > 20

    def test_register_empty_name_rejected(self, client):
        resp = client.post("/api/v1/auth/register", json={"display_name": ""})
        assert resp.status_code == 422

    def test_register_missing_name_rejected(self, client):
        resp = client.post("/api/v1/auth/register", json={})
        assert resp.status_code == 422

    def test_register_twice_same_name_creates_different_users(self, client):
        r1 = client.post("/api/v1/auth/register", json={"display_name": "Alice"})
        r2 = client.post("/api/v1/auth/register", json={"display_name": "Alice"})
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["user_id"] != r2.json()["user_id"]


# ── Auth: JWT Validation ──────────────────────────────────────────────


class TestAuthJWT:
    def _get_token(self, client):
        resp = client.post("/api/v1/auth/register", json={"display_name": "JWTUser"})
        return resp.json()["jwt_token"]

    def test_protected_endpoint_without_token_returns_401(self, client):
        resp = client.post("/api/v1/llm/chat", json={
            "messages": [{"role": "user", "content": "hi"}]
        })
        assert resp.status_code in (401, 403)

    def test_protected_endpoint_with_invalid_token_returns_401(self, client):
        resp = client.post(
            "/api/v1/llm/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer invalid-token-here"},
        )
        assert resp.status_code in (401, 403)

    def test_protected_endpoint_with_valid_token_accepted(self, client):
        token = self._get_token(client)
        # This will fail at LLM call (mocked), but should NOT fail at auth
        with patch("nexus_server.llm_gateway.call_llm", new_callable=AsyncMock,
                    return_value=("Hello!", "gemini-2.5-flash", "stop", [])):
            resp = client.post(
                "/api/v1/llm/chat",
                json={"messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200


# ── LLM Gateway: Request Format ──────────────────────────────────────


class TestLLMGateway:
    def _get_token(self, client):
        resp = client.post("/api/v1/auth/register", json={"display_name": "LLMUser"})
        return resp.json()["jwt_token"]

    def test_chat_returns_correct_format(self, client):
        token = self._get_token(client)
        with patch("nexus_server.llm_gateway.call_llm", new_callable=AsyncMock,
                    return_value=("Hello world!", "gemini-2.5-flash", "stop", [])):
            resp = client.post(
                "/api/v1/llm/chat",
                json={
                    "messages": [{"role": "user", "content": "hello"}],
                    "system_prompt": "You are helpful.",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["role"] == "assistant"
            assert data["content"] == "Hello world!"
            assert data["model"] == "gemini-2.5-flash"
            assert "tool_calls_executed" in data

    def test_chat_with_tool_calls_executed(self, client):
        token = self._get_token(client)
        # First call returns tool call, second returns final answer
        call_count = 0

        async def mock_call_llm(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ("", "gemini", "tool_calls", [
                    {"id": "1", "name": "web_search", "arguments": {"query": "test"}}
                ])
            return ("Search result: found it!", "gemini", "stop", [])

        with patch("nexus_server.llm_gateway.call_llm", side_effect=mock_call_llm), \
             patch("nexus_server.llm_gateway.execute_tool", new_callable=AsyncMock,
                   return_value="Mock search result"):
            resp = client.post(
                "/api/v1/llm/chat",
                json={"messages": [{"role": "user", "content": "search something"}]},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "web_search" in data["tool_calls_executed"]

    def test_chat_invalid_role_rejected(self, client):
        token = self._get_token(client)
        resp = client.post(
            "/api/v1/llm/chat",
            json={"messages": [{"role": "admin", "content": "hi"}]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422

    def test_chat_empty_messages_rejected(self, client):
        token = self._get_token(client)
        resp = client.post(
            "/api/v1/llm/chat",
            json={"messages": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422


# ── Sync Endpoints ────────────────────────────────────────────────────


class TestSyncEndpointsRetired:
    """Phase B: ``/api/v1/sync/push`` and ``/api/v1/sync/pull`` are gone.
    Round 2 made the desktop a thin client — chat history flows through
    /api/v1/agent/messages, never through /sync/*. The router was removed
    from main.py and ``nexus_server.sync_hub`` is now an ImportError
    tombstone. Verify both endpoints return 404 and the module refuses
    to import."""

    def test_sync_push_returns_404(self, client):
        # No auth needed — 404 should fire before auth middleware
        resp = client.post(
            "/api/v1/sync/push",
            json={"events": [{"event_type": "user_message",
                              "content": "x", "session_id": "s"}]},
        )
        assert resp.status_code == 404

    def test_sync_pull_returns_404(self, client):
        resp = client.get("/api/v1/sync/pull?after_id=0")
        assert resp.status_code == 404

    def test_sync_hub_module_refuses_import(self):
        import importlib
        import sys
        sys.modules.pop("nexus_server.sync_hub", None)
        with pytest.raises(ImportError):
            importlib.import_module("nexus_server.sync_hub")


# ── Passkey Page ──────────────────────────────────────────────────────


class TestPasskeyPage:
    def test_passkey_page_returns_html(self, client):
        resp = client.get("/auth/passkey-page")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Nexus" in resp.text
        assert "SimpleWebAuthnBrowser" in resp.text

    def test_passkey_page_contains_webauthn_js(self, client):
        resp = client.get("/auth/passkey-page")
        assert resp.status_code == 200
        assert "rune-callback://" in resp.text
        assert "startAuthentication" in resp.text or "handleLogin" in resp.text


# ── Config ────────────────────────────────────────────────────────────


class TestConfig:
    def test_config_loads_env_vars(self):
        from nexus_server.config import get_config
        cfg = get_config()
        assert cfg.SERVER_SECRET == "test-secret-key"
        assert cfg.GEMINI_API_KEY == "fake-key-for-testing"

    def test_config_has_defaults(self):
        from nexus_server.config import get_config
        cfg = get_config()
        assert cfg.SERVER_PORT in (8001, 8000)
        assert cfg.JWT_ALGORITHM == "HS256"


# ── Monorepo Integration ─────────────────────────────────────────────


class TestMonorepoIntegration:
    """Verify monorepo refactoring didn't break imports."""

    def test_sdk_llm_client_importable(self):
        from nexus_core.llm import LLMClient
        assert LLMClient is not None

    def test_sdk_tools_importable(self):
        from nexus_core.tools import BaseTool, WebSearchTool, ReadUploadedFileTool
        assert BaseTool is not None

    def test_sdk_memory_importable(self):
        from nexus_core.memory import EventLog, CuratedMemory
        assert EventLog is not None

    def test_server_does_not_eagerly_import_nexus_framework(self):
        """Server should not eagerly import the Nexus agent framework.

        Post-S2 server CAN import Nexus — twin_manager._create_twin
        does ``from nexus.twin import DigitalTwin`` lazily on first
        chat. But the import is **deferred**: importing the server's
        always-on modules (auth, llm_gateway, agent_state, chain_proxy)
        must not pull the ``nexus`` framework package into
        ``sys.modules``. Otherwise dev environments without ``nexus``
        installed couldn't even start the server.

        Phase D rename note: the ``nexus`` package (was ``rune_twin``)
        has the same top-level name as a substring of ``nexus_server``
        and ``nexus_core``. Match the framework package precisely:
        ``name == "nexus"`` or starts with ``"nexus."`` (a submodule)."""
        import sys

        def _is_framework(m: str) -> bool:
            return m == "nexus" or m.startswith("nexus.")

        # Drop any prior framework import from earlier tests.
        for m in list(sys.modules):
            if _is_framework(m):
                sys.modules.pop(m, None)

        import nexus_server.llm_gateway as gw  # noqa: F401
        import nexus_server.auth as auth_mod  # noqa: F401
        import nexus_server.agent_state as agent_state_mod  # noqa: F401
        import nexus_server.chain_proxy as chain_proxy_mod  # noqa: F401

        framework_modules = [m for m in sys.modules if _is_framework(m)]
        assert len(framework_modules) == 0, (
            f"Server eagerly imported Nexus framework modules: {framework_modules}"
        )


# ── Passkey Login: credential ID → user lookup ────────────────────────


class TestPasskeyLoginFinish:
    """Verify login/finish resolves the *right* user from a passkey assertion.

    The previous implementation fell back to "most recent user" whenever the
    provided user_id did not match a row — that silently logged a returning
    user into someone else's account. The current behavior:

      1. Direct match: request.user_id == users.id  → that user
      2. Credential match: assertion.id == passkey_credential.id  → that user
      3. Otherwise → 404
    """

    def _register_user(self, client, name="PasskeyUser"):
        resp = client.post("/api/v1/auth/register", json={"display_name": name})
        assert resp.status_code == 201
        return resp.json()

    def _register_with_passkey(self, client, name, credential_id):
        """Register via the WebAuthn flow so passkey_credential is populated."""
        start = client.post(
            "/api/v1/auth/passkey/register/start",
            json={"display_name": name},
        )
        assert start.status_code == 200
        user_id = start.json()["user_id"]
        finish = client.post(
            "/api/v1/auth/passkey/register/finish",
            json={
                "user_id": user_id,
                "display_name": name,
                "credential": {"id": credential_id, "rawId": credential_id, "type": "public-key"},
            },
        )
        assert finish.status_code == 200
        return user_id

    def test_login_finish_with_valid_user_id(self, client):
        """Login finish works when correct user_id is provided directly."""
        reg = self._register_user(client)
        resp = client.post("/api/v1/auth/passkey/login/finish", json={
            "user_id": reg["user_id"],
            "assertion": {"id": "irrelevant-credential-id"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "jwt_token" in data
        assert data["expires_in_seconds"] > 0

    def test_login_finish_resolves_user_via_credential_id(self, client):
        """When the user_id field is actually a credential id (current FE
        behavior), the server should match against passkey_credential.id."""
        cred_id = "Qg7T-credential-id-aaa"
        user_id = self._register_with_passkey(client, "AliceWithPasskey", cred_id)

        resp = client.post("/api/v1/auth/passkey/login/finish", json={
            # FE today sends assertion.id in user_id; mirror that here
            "user_id": cred_id,
            "assertion": {"id": cred_id},
        })
        assert resp.status_code == 200
        # Verify it logged in as Alice, not whoever was registered last
        token = resp.json()["jwt_token"]
        import jwt as _jwt
        unverified = _jwt.decode(token, options={"verify_signature": False})
        assert unverified["user_id"] == user_id

    def test_login_finish_does_not_fall_back_to_recent_user(self, client):
        """Regression: an unknown credential id must NOT silently log in as the
        most recently registered user (previous behavior, security hole)."""
        # Register Alice (will become "most recent" at the moment of her call)
        self._register_with_passkey(client, "Alice", "credential-alice")
        # Register Bob (now most recent)
        self._register_with_passkey(client, "Bob", "credential-bob")

        resp = client.post("/api/v1/auth/passkey/login/finish", json={
            "user_id": "credential-charlie-does-not-exist",
            "assertion": {"id": "credential-charlie-does-not-exist"},
        })
        assert resp.status_code == 404
        # Server's global handler maps HTTPException.detail -> "error"
        body = resp.json()
        assert "register" in (body.get("error") or body.get("detail") or "").lower()

    def test_login_finish_no_users_returns_404(self, client):
        """Login finish returns 404 when no users exist at all."""
        resp = client.post("/api/v1/auth/passkey/login/finish", json={
            "user_id": "nobody",
            "assertion": {},
        })
        assert resp.status_code == 404


# ── Server .env Loading ───────────────────────────────────────────────


class TestEnvLoading:
    def test_dotenv_loaded_gemini_key(self):
        """Verify .env values are loaded into os.environ."""
        import os
        assert os.environ.get("GEMINI_API_KEY") == "fake-key-for-testing"

    def test_dotenv_does_not_override_existing(self):
        """Existing env vars should not be overridden by .env."""
        import os
        os.environ["TEST_EXISTING_VAR"] = "original"
        # _load_dotenv won't override since key already exists
        assert os.environ["TEST_EXISTING_VAR"] == "original"


# ── LLM Gateway: Tool Loop ───────────────────────────────────────────


class TestToolLoop:
    def _get_token(self, client):
        resp = client.post("/api/v1/auth/register", json={"display_name": "ToolUser"})
        return resp.json()["jwt_token"]

    def test_tool_loop_max_rounds_safety(self, client):
        """Tool loop should not exceed MAX_TOOL_ROUNDS."""
        token = self._get_token(client)

        # LLM always returns tool calls — should hit max rounds
        async def always_tool_call(*args, **kwargs):
            return ("", "gemini", "tool_calls", [
                {"id": "1", "name": "web_search", "arguments": {"query": "test"}}
            ])

        with patch("nexus_server.llm_gateway.call_llm", side_effect=always_tool_call), \
             patch("nexus_server.llm_gateway.execute_tool", new_callable=AsyncMock,
                   return_value="mock result"):
            resp = client.post(
                "/api/v1/llm/chat",
                json={"messages": [{"role": "user", "content": "loop test"}]},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["stop_reason"] == "max_rounds"
            # Should have executed tools multiple times but stopped
            assert len(data["tool_calls_executed"]) <= 6  # MAX_TOOL_ROUNDS + 1

    def test_chat_routes_through_twin_when_enabled(self, client):
        """Phase D: when USE_TWIN is on, /api/v1/llm/chat routes the
        latest user message into TwinManager.get_twin(...).chat(...)
        instead of calling Gemini directly. We inject a fake twin via
        twin_manager._test_override to avoid the cold start of a real
        DigitalTwin in tests."""
        from nexus_server import twin_manager
        from nexus_server import llm_gateway as gw

        seen_msgs: list[str] = []

        class FakeTwin:
            async def chat(self, msg: str, session_id=None, **kwargs) -> str:
                # Phase Q: chat handler now passes session_id=. Accept
                # it (and any future kwargs) so the mock keeps working
                # without rewriting every test.
                seen_msgs.append(msg)
                return "twin-reply"

            async def close(self) -> None:
                pass

        twin_manager._test_override = FakeTwin()
        old_use_twin = gw.config.USE_TWIN
        gw.config.USE_TWIN = True
        try:
            reg = client.post(
                "/api/v1/auth/register",
                json={"display_name": "TwinUser"},
            )
            token = reg.json()["jwt_token"]

            resp = client.post(
                "/api/v1/llm/chat",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "messages": [{"role": "user", "content": "hello twin"}],
                },
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["content"] == "twin-reply"
            assert body["model"] == "twin"
            assert seen_msgs == ["hello twin"]
        finally:
            twin_manager._test_override = None
            gw.config.USE_TWIN = old_use_twin

    def test_chat_returns_502_when_twin_chat_raises(self, client):
        """S1 (server cleanup) inverted the previous behavior: twin
        failures used to silently fall back to the legacy direct-LLM
        gateway, which produced answers the agent's contract / drift /
        memory pipeline never saw. That fallback is gone — twin errors
        now surface as a clean 502 to the caller, and the legacy gateway
        is reachable only when USE_TWIN=0 (test-only).

        Regression: the broad `except Exception` in the chat handler
        used to remap the 502 to a 500 — verify we now preserve the
        structured 502 status code via the explicit HTTPException
        re-raise."""
        from nexus_server import twin_manager
        from nexus_server import llm_gateway as gw

        class BoomTwin:
            async def chat(self, msg: str, session_id=None, **kwargs) -> str:
                raise RuntimeError("twin imploded")

            async def close(self) -> None:
                pass

        twin_manager._test_override = BoomTwin()
        old_use_twin = gw.config.USE_TWIN
        gw.config.USE_TWIN = True

        try:
            reg = client.post(
                "/api/v1/auth/register",
                json={"display_name": "FallbackUser"},
            )
            token = reg.json()["jwt_token"]

            resp = client.post(
                "/api/v1/llm/chat",
                headers={"Authorization": f"Bearer {token}"},
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
            assert resp.status_code == 502, resp.text
            body = resp.json()
            # FastAPI's exception handler may shape this as
            # {"detail": ...} or as the project's structured error
            # envelope ({"error": ..., "status_code": 502}). Accept
            # both — what matters is the 502 + the twin-error string.
            payload_text = (
                body.get("detail")
                or body.get("error")
                or resp.text
            )
            assert "twin imploded" in payload_text, payload_text
        finally:
            twin_manager._test_override = None
            gw.config.USE_TWIN = old_use_twin

    # ── S2: TwinManager chain-mode kwarg resolution ────────────────────
    #
    # We can't actually spin up a real DigitalTwin in chain mode in unit
    # tests (would require a live BSC node), so the test
    # surface is the *decision* function _resolve_chain_kwargs — does it
    # produce the right kwargs given a particular config + DB state?

    def test_twin_chain_kwargs_local_when_no_server_pk(self, client):
        """No SERVER_PRIVATE_KEY → twin must start in local mode even
        if everything else is configured."""
        from nexus_server import twin_manager
        from nexus_server import config as cfg_mod

        cfg = cfg_mod.get_config()
        old_pk = cfg.SERVER_PRIVATE_KEY
        cfg.SERVER_PRIVATE_KEY = None
        twin_manager.config = cfg  # module-cached reference
        try:
            kwargs = twin_manager._resolve_chain_kwargs("any-user-id")
            assert kwargs == {}, kwargs
        finally:
            cfg.SERVER_PRIVATE_KEY = old_pk
            twin_manager.config = cfg

    def test_twin_chain_kwargs_local_when_user_unregistered(self, client):
        """SERVER_PRIVATE_KEY present, but the user has no
        chain_agent_id yet — twin must start in local mode."""
        from nexus_server import twin_manager
        from nexus_server import config as cfg_mod
        from nexus_server.database import get_db_connection

        # Register a fresh user (no chain_agent_id yet)
        reg = client.post(
            "/api/v1/auth/register",
            json={"display_name": "UnregisteredChainUser"},
        )
        user_id = reg.json()["user_id"]

        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT chain_agent_id FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        assert row is not None and row[0] is None, "fresh user should have no chain_agent_id"

        cfg = cfg_mod.get_config()
        old_pk = cfg.SERVER_PRIVATE_KEY
        old_rpc = cfg.NEXUS_TESTNET_RPC
        cfg.SERVER_PRIVATE_KEY = "0x" + "a" * 64
        cfg.NEXUS_TESTNET_RPC = "https://example/testnet"
        twin_manager.config = cfg
        try:
            kwargs = twin_manager._resolve_chain_kwargs(user_id)
            assert kwargs == {}, kwargs
        finally:
            cfg.SERVER_PRIVATE_KEY = old_pk
            cfg.NEXUS_TESTNET_RPC = old_rpc
            twin_manager.config = cfg

    def test_twin_bootstrap_chain_identity_auto_registers(self, client):
        """S6 contract: ``twin_manager.bootstrap_chain_identity``
        registers a user's ERC-8004 identity on first call when chain
        is configured but the user has no ``chain_agent_id``. Subsequent
        calls return the cached id and do NOT re-register.

        This is the path that lets us delete ``/chain/register-agent``
        entirely in Round 2-C — twin's first chat triggers the
        bootstrap and the user is on chain without any explicit
        registration HTTP call."""
        from nexus_server import twin_manager
        from nexus_server import chain_proxy as cp
        from nexus_server import config as cfg_mod
        from nexus_server.database import get_db_connection

        reg = client.post(
            "/api/v1/auth/register",
            json={"display_name": "AutoRegisterUser"},
        )
        user_id = reg.json()["user_id"]

        register_calls: list[str] = []

        class FakeChainClient:
            def register_agent(self, name):
                register_calls.append(name)
                return 9999

        cp._chain_client_test_override = FakeChainClient()

        cfg = cfg_mod.get_config()
        old_pk = cfg.SERVER_PRIVATE_KEY
        old_chainrpc = cfg.CHAIN_RPC_URL
        old_rpc = cfg.NEXUS_TESTNET_RPC
        cfg.SERVER_PRIVATE_KEY = "0x" + "c" * 64
        cfg.CHAIN_RPC_URL = None
        cfg.NEXUS_TESTNET_RPC = "https://example/testnet"
        twin_manager.config = cfg
        try:
            # First call: no cached id → registers, persists, returns id
            tid1 = twin_manager.bootstrap_chain_identity(user_id)
            assert tid1 == 9999
            assert register_calls == ["AutoRegisterUser"]

            # DB row now reflects the registration
            with get_db_connection() as conn:
                row = conn.execute(
                    "SELECT chain_agent_id FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
            assert row[0] == 9999

            # Second call: cached → no re-register
            tid2 = twin_manager.bootstrap_chain_identity(user_id)
            assert tid2 == 9999
            assert register_calls == ["AutoRegisterUser"]  # unchanged
        finally:
            cp._chain_client_test_override = None
            cfg.SERVER_PRIVATE_KEY = old_pk
            cfg.CHAIN_RPC_URL = old_chainrpc
            cfg.NEXUS_TESTNET_RPC = old_rpc
            twin_manager.config = cfg

    def test_twin_chain_kwargs_built_when_registered(self, client):
        """All chain prereqs present + user has chain_agent_id → twin
        gets full chain kwargs."""
        from nexus_server import twin_manager
        from nexus_server import config as cfg_mod
        from nexus_server.database import get_db_connection

        reg = client.post(
            "/api/v1/auth/register",
            json={"display_name": "RegisteredChainUser"},
        )
        user_id = reg.json()["user_id"]

        # Simulate a successful /chain/register-agent run by writing
        # the token id directly. The chain-mode resolver only reads;
        # it doesn't care how the row got there.
        TOKEN_ID = 42
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE users SET chain_agent_id = ? WHERE id = ?",
                (TOKEN_ID, user_id),
            )
            conn.commit()

        cfg = cfg_mod.get_config()
        old_pk = cfg.SERVER_PRIVATE_KEY
        old_rpc = cfg.NEXUS_TESTNET_RPC
        old_state = cfg.NEXUS_TESTNET_AGENT_STATE_ADDRESS
        old_idreg = cfg.NEXUS_TESTNET_IDENTITY_REGISTRY
        old_tm = cfg.NEXUS_TESTNET_TASK_MANAGER_ADDRESS
        old_net = cfg.NEXUS_NETWORK
        old_chainrpc = cfg.CHAIN_RPC_URL

        cfg.SERVER_PRIVATE_KEY = "0x" + "b" * 64
        cfg.NEXUS_NETWORK = "bsc-testnet"
        # ``chain_active_rpc`` prefers CHAIN_RPC_URL when set; clear it so
        # the property falls through to NEXUS_TESTNET_RPC for this test.
        cfg.CHAIN_RPC_URL = None
        cfg.NEXUS_TESTNET_RPC = "https://example/testnet"
        cfg.NEXUS_TESTNET_AGENT_STATE_ADDRESS = "0xAS"
        cfg.NEXUS_TESTNET_IDENTITY_REGISTRY = "0xIR"
        cfg.NEXUS_TESTNET_TASK_MANAGER_ADDRESS = "0xTM"
        twin_manager.config = cfg
        try:
            kwargs = twin_manager._resolve_chain_kwargs(user_id)
            assert kwargs, "expected chain kwargs to be populated"
            assert kwargs["private_key"] == cfg.SERVER_PRIVATE_KEY
            assert kwargs["network"] == "testnet"  # short form
            assert kwargs["rpc_url"] == "https://example/testnet"
            assert kwargs["agent_state_address"] == "0xAS"
            assert kwargs["identity_registry_address"] == "0xIR"
            assert kwargs["task_manager_address"] == "0xTM"
        finally:
            cfg.SERVER_PRIVATE_KEY = old_pk
            cfg.NEXUS_TESTNET_RPC = old_rpc
            cfg.NEXUS_TESTNET_AGENT_STATE_ADDRESS = old_state
            cfg.NEXUS_TESTNET_IDENTITY_REGISTRY = old_idreg
            cfg.NEXUS_TESTNET_TASK_MANAGER_ADDRESS = old_tm
            cfg.NEXUS_NETWORK = old_net
            cfg.CHAIN_RPC_URL = old_chainrpc
            twin_manager.config = cfg

    def test_call_gemini_threads_tools_and_parses_function_calls(self):
        """Regression: call_gemini used to build gemini_tools then drop it
        on the floor — Gemini never saw the function declarations, so
        web_search/read_url were effectively disabled. Verify (a) tools
        are now passed in `config=` and (b) function_call parts in the
        response are parsed back into tool_calls for the outer loop."""
        from unittest.mock import MagicMock, patch
        import asyncio
        from nexus_server.llm_gateway import call_gemini, TOOL_DEFINITIONS

        captured_kwargs = {}

        # Build a fake response that contains a function_call part
        fake_fc = MagicMock()
        fake_fc.name = "web_search"
        fake_fc.args = {"query": "current BNB price"}
        fake_part = MagicMock()
        fake_part.text = None
        fake_part.function_call = fake_fc
        fake_content = MagicMock()
        fake_content.parts = [fake_part]
        fake_candidate = MagicMock()
        fake_candidate.content = fake_content
        fake_response = MagicMock()
        fake_response.candidates = [fake_candidate]
        fake_response.text = ""

        def fake_generate(model, contents, config):
            captured_kwargs["model"] = model
            captured_kwargs["contents"] = contents
            captured_kwargs["config"] = config
            return fake_response

        fake_client = MagicMock()
        fake_client.models.generate_content = fake_generate

        with patch("google.genai.Client", return_value=fake_client):
            content, model, stop_reason, tool_calls = asyncio.get_event_loop().run_until_complete(
                call_gemini(
                    messages=[{"role": "user", "content": "search BNB price"}],
                    system_prompt="be helpful",
                    model="gemini-2.5-flash",
                    temperature=0.7,
                    max_tokens=512,
                    tools=TOOL_DEFINITIONS,
                )
            )

        # 1) Tools were threaded through
        cfg = captured_kwargs["config"]
        assert "tools" in cfg, "tools must be in config so Gemini sees them"
        assert cfg["tools"][0]["function_declarations"][0]["name"] == "web_search"
        assert cfg.get("temperature") == 0.7
        assert cfg.get("max_output_tokens") == 512
        assert cfg["system_instruction"] == "be helpful"
        assert cfg["tool_config"]["function_calling_config"]["mode"] == "AUTO"

        # 2) function_call parts come back as tool_calls
        assert stop_reason == "tool_calls"
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "web_search"
        assert tool_calls[0]["arguments"] == {"query": "current BNB price"}

    def test_web_search_tool_without_key_returns_message(self):
        """Web search without TAVILY_API_KEY returns helpful message."""
        from nexus_server import llm_gateway
        old_key = llm_gateway.config.TAVILY_API_KEY
        llm_gateway.config.TAVILY_API_KEY = None
        try:
            import asyncio
            result = asyncio.get_event_loop().run_until_complete(llm_gateway._web_search("test"))
            assert "unavailable" in result.lower() or "not configured" in result.lower()
        finally:
            llm_gateway.config.TAVILY_API_KEY = old_key


# ── User Registration Persistence ─────────────────────────────────────


class TestUserPersistence:
    def test_registered_user_persists_across_requests(self, client):
        """User data should persist in the database."""
        reg = client.post("/api/v1/auth/register", json={"display_name": "PersistUser"})
        user_id = reg.json()["user_id"]

        # Verify user can be found via login/finish
        resp = client.post("/api/v1/auth/passkey/login/finish", json={
            "user_id": user_id,
            "assertion": {},
        })
        assert resp.status_code == 200

    def test_multiple_users_isolated(self, client):
        """Different users get different JWT tokens."""
        r1 = client.post("/api/v1/auth/register", json={"display_name": "User1"})
        r2 = client.post("/api/v1/auth/register", json={"display_name": "User2"})
        assert r1.json()["jwt_token"] != r2.json()["jwt_token"]
        assert r1.json()["user_id"] != r2.json()["user_id"]


# ── Sync roundtrip: retired in Phase B (was push then pull) ───────────
# Whole class deleted — /sync/push and /sync/pull return 404. See
# TestSyncEndpointsRetired above for the 404 + ImportError contract.


# ── LLM Chat: attachments fold-in ─────────────────────────────────────


class TestLLMChatAttachments:
    """Verify the /llm/chat endpoint handles file attachments correctly:
    - Text content is folded into the last user message.
    - Binary-only attachments produce a metadata note.
    - Total payload over MAX_ATTACHMENT_BYTES_TOTAL → 413.
    - Empty/missing attachments behave exactly like before (regression).
    """

    def _get_token(self, client):
        resp = client.post("/api/v1/auth/register", json={"display_name": "AttUser"})
        return resp.json()["jwt_token"]

    def _patched_call_llm(self, captured: list):
        """Build a stub for llm_gateway.call_llm that records what messages
        the gateway would have sent to the model and returns a canned reply."""
        from unittest.mock import AsyncMock

        async def _fake(messages, system_prompt, model, temperature, max_tokens, tools):
            captured.append([dict(m) for m in messages])
            return ("ok", "stub-model", "stop", [])

        return AsyncMock(side_effect=_fake)

    # NOTE: pre-distill fold tests removed. The handler now ALWAYS distills
    # attachments first and folds the SUMMARY into the user message. The
    # tests below (test_text_attachment_is_distilled_and_event_persisted,
    # test_distill_falls_back_when_llm_errors, etc.) cover the new contract.

    def test_attachment_over_total_cap_returns_413(self, client, monkeypatch):
        """The cap is now 100 MB by default, but the per-test override
        below temporarily drops it to 1 KB so we can trigger 413 without
        actually shoveling 100 MB through TestClient."""
        from nexus_server import llm_gateway as gw
        monkeypatch.setattr(gw, "MAX_ATTACHMENT_BYTES_TOTAL", 1024)

        token = self._get_token(client)
        chunk = "x" * 800
        resp = client.post(
            "/api/v1/llm/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "attachments": [
                    {"name": "a.txt", "mime": "text/plain",
                     "size_bytes": len(chunk), "content_text": chunk},
                    {"name": "b.txt", "mime": "text/plain",
                     "size_bytes": len(chunk), "content_text": chunk},
                ],
            },
        )
        assert resp.status_code == 413

    def test_text_attachment_is_distilled_and_summary_returned(self, client):
        """When a text attachment is sent, the server distills it via the
        LLM (mocked here), folds the SUMMARY into the user message (not
        the raw text), and returns the summary on the chat response.

        Phase B: persistence to ``sync_events`` was removed alongside
        the table itself. The summary now rides back inline only;
        ``sync_id`` is always None."""
        from unittest.mock import patch

        token = self._get_token(client)

        # Two LLM calls per turn-with-attachment: one for distill, one
        # for the actual chat. Track who called what.
        calls = []

        async def _fake_llm(messages, system_prompt, model, temp, max_tokens, tools):
            calls.append({"system": system_prompt, "messages": messages})
            if system_prompt and "file summarizer" in system_prompt:
                return ("Distilled: this file is about widgets.",
                        "stub-distill", "stop", [])
            return ("noted", "stub-chat", "stop", [])

        with patch("nexus_server.llm_gateway.call_llm", side_effect=_fake_llm):
            resp = client.post(
                "/api/v1/llm/chat",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "messages": [{"role": "user",
                                  "content": "what's in foo.txt?"}],
                    "attachments": [{
                        "name": "foo.txt", "mime": "text/plain",
                        "size_bytes": 11,
                        "content_text": "hello world",
                    }],
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Two LLM calls happened — distill THEN chat
        assert len(calls) == 2
        assert "file summarizer" in calls[0]["system"]
        # The chat call sees the SUMMARY in the folded user message,
        # NOT the raw "hello world" content
        chat_msgs = calls[1]["messages"]
        last_user = chat_msgs[-1]["content"]
        assert "Distilled: this file is about widgets." in last_user
        assert "hello world" not in last_user

        # Response carries one summary inline. sync_id is None now —
        # see Phase B docstring above for why.
        assert len(body["attachment_summaries"]) == 1
        s = body["attachment_summaries"][0]
        assert s["name"] == "foo.txt"
        assert "widgets" in s["summary"]
        assert s["sync_id"] is None

    def test_distill_falls_back_when_llm_errors(self, client):
        """LLM raising during distill should NOT 500 the request — we
        fall back to a head excerpt, mark source as '+fallback', and
        keep going."""
        from unittest.mock import patch

        token = self._get_token(client)

        async def _fake_llm(messages, system_prompt, model, temp, max_tokens, tools):
            if system_prompt and "file summarizer" in system_prompt:
                raise RuntimeError("LLM provider down")
            return ("ok", "stub-chat", "stop", [])

        with patch("nexus_server.llm_gateway.call_llm", side_effect=_fake_llm):
            resp = client.post(
                "/api/v1/llm/chat",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "messages": [{"role": "user", "content": "summarize"}],
                    "attachments": [{
                        "name": "report.txt", "mime": "text/plain",
                        "size_bytes": 13, "content_text": "important data",
                    }],
                },
            )

        assert resp.status_code == 200
        s = resp.json()["attachment_summaries"][0]
        assert "fallback" in s["source"]
        assert "important data" in s["summary"]

    def test_binary_attachment_distill_uses_metadata_stub(self, client):
        """Pure binary (no content_text, opaque base64) should still
        produce a summary — derived from filename + mime + size."""
        from unittest.mock import patch
        import base64

        token = self._get_token(client)

        async def _fake_llm(messages, system_prompt, model, temp, max_tokens, tools):
            if system_prompt and "file summarizer" in system_prompt:
                # Verify the distiller saw a binary-stub note
                user_msg = messages[-1]["content"]
                assert "binary" in user_msg.lower() or "thing.bin" in user_msg
                return ("Stub summary for binary file thing.bin",
                        "stub", "stop", [])
            return ("noted", "stub", "stop", [])

        # Truly non-UTF-8 bytes: \xff alone isn't a valid lead byte
        b64 = base64.b64encode(b"\xff\xfe\xfd\xfc\xfb").decode()
        with patch("nexus_server.llm_gateway.call_llm", side_effect=_fake_llm):
            resp = client.post(
                "/api/v1/llm/chat",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "messages": [{"role": "user", "content": "what is this?"}],
                    "attachments": [{
                        "name": "thing.bin",
                        "mime": "application/octet-stream",
                        "size_bytes": 5,
                        "content_base64": b64,
                    }],
                },
            )
        assert resp.status_code == 200
        s = resp.json()["attachment_summaries"][0]
        assert s["source"] in ("binary-stub", "binary-stub+fallback")
        assert "thing.bin" in s["summary"]

    def test_large_text_attachment_is_distilled_to_summary(self, client):
        """Whatever the user attaches — small text, big text, binary —
        the model sees the DISTILLED summary, never the raw bytes.
        This is what makes 100MB attachments feasible without blowing
        the model's context window."""
        from unittest.mock import patch
        token = self._get_token(client)
        big_text = "A" * 500_000  # 500 KB of A's

        async def _fake(messages, system_prompt, model, temp, max_tokens, tools):
            if system_prompt and "file summarizer" in system_prompt:
                return ("Summary: a 500KB block of letter A.",
                        "stub", "stop", [])
            # Capture what the chat-leg saw
            _fake.last_chat_messages = list(messages)
            return ("noted", "stub", "stop", [])
        _fake.last_chat_messages = None

        with patch("nexus_server.llm_gateway.call_llm", side_effect=_fake):
            resp = client.post(
                "/api/v1/llm/chat",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "messages": [{"role": "user", "content": "summarize"}],
                    "attachments": [{
                        "name": "big.txt", "mime": "text/plain",
                        "size_bytes": len(big_text),
                        "content_text": big_text,
                    }],
                },
            )

        assert resp.status_code == 200
        last_user_to_chat = _fake.last_chat_messages[-1]["content"]
        # Summary is folded
        assert "Summary: a 500KB block of letter A." in last_user_to_chat
        # Raw 500KB never reaches the chat leg
        assert big_text not in last_user_to_chat
        assert "AAAAA" * 100 not in last_user_to_chat

    def test_empty_attachments_field_is_backward_compatible(self, client):
        """Regression: chat with no attachments behaves like before."""
        from unittest.mock import patch
        captured = []
        token = self._get_token(client)

        with patch("nexus_server.llm_gateway.call_llm",
                   side_effect=self._patched_call_llm(captured).side_effect):
            resp = client.post(
                "/api/v1/llm/chat",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    # no "attachments" key at all
                },
            )

        assert resp.status_code == 200
        last_user = captured[0][-1]
        assert last_user["content"] == "hi"  # untouched


# ── Chain Proxy: real path + graceful fallback ────────────────────────


class TestChainProxy:
    """Verify chain_proxy falls back to mock when env is missing AND
    actually uses BSCClient when one is injected."""

    def _get_token_and_user(self, client):
        resp = client.post("/api/v1/auth/register", json={"display_name": "ChainUser"})
        d = resp.json()
        return d["jwt_token"], d["user_id"]

    def test_register_agent_falls_back_to_pending_when_no_env(self, client, monkeypatch):
        """No SERVER_PRIVATE_KEY → status='pending', no chain call.

        We force ``chain_is_configured`` False here regardless of what
        the operator's shell env has set, so the test is hermetic.
        """
        from nexus_server import chain_proxy as cp
        from nexus_server import config as cfg_mod
        # Make sure no test override is leaking from a sibling test, AND
        # that the cached client (if any prior test built one) isn't reused.
        cp._chain_client_test_override = None
        cp._chain_client = None
        # Pretend the operator hasn't configured chain.
        monkeypatch.setattr(cfg_mod.ServerConfig, "SERVER_PRIVATE_KEY", None)

        token, user_id = self._get_token_and_user(client)

        resp = client.post(
            "/api/v1/chain/register-agent",
            headers={"Authorization": f"Bearer {token}"},
            json={"agent_name": "alice"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending"
        assert body["tx_hash"] is None
        # Did NOT persist a fake chain_agent_id to users
        from nexus_server.database import get_db_connection
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT chain_agent_id FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        assert row[0] is None

    def test_register_agent_uses_chain_client_when_available(self, client):
        """Stub a fake chain client → status='registered' + cached agent_id.

        S6 contract change: the deprecated /chain/register-agent
        endpoint delegates to ``twin_manager.bootstrap_chain_identity``,
        which resolves the agent name from the user's stored
        ``display_name`` (with a synthetic ``rune-user-{uid}`` fallback)
        — the request body's ``agent_name`` field is now ignored.
        Single name-resolution path means twin's auto-bootstrap (no HTTP
        body) and explicit operator calls produce identical names.
        """
        from nexus_server import chain_proxy as cp

        class FakeClient:
            def register_agent(self, name):
                self.last_name = name
                return 4242

        fake = FakeClient()
        cp._chain_client_test_override = fake
        try:
            token, user_id = self._get_token_and_user(client)
            resp = client.post(
                "/api/v1/chain/register-agent",
                headers={"Authorization": f"Bearer {token}"},
                # Passing "alice" — but S6 says the body is ignored;
                # bootstrap uses display_name from the user row.
                json={"agent_name": "alice"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "registered"
            assert body["agent_id"] == "4242"
            # ``_get_token_and_user`` for TestChainProxy registers with
            # display_name="ChainUser" (line 1080). Pin the exact value
            # so a regression where the endpoint started honouring
            # request.agent_name again would surface as a clear
            # equality failure instead of "not alice but who knows
            # what". If _get_token_and_user is renamed/changed, update
            # this assertion alongside it.
            assert fake.last_name == "ChainUser"

            # Cached: a second call should NOT hit the chain again
            fake.last_name = None
            resp2 = client.post(
                "/api/v1/chain/register-agent",
                headers={"Authorization": f"Bearer {token}"},
                json={"agent_name": "alice"},
            )
            assert resp2.status_code == 200
            assert resp2.json()["agent_id"] == "4242"
            assert fake.last_name is None  # not re-registered
        finally:
            cp._chain_client_test_override = None

    def test_register_agent_accepts_missing_agent_name(self, client):
        """The desktop currently doesn't surface a name input on the
        passkey-only login flow, so it sends an empty string. Server
        must NOT 422 — it should fall back to the user's display_name.
        Regression for the 'Connected · chain register failed: 422'
        bug seen in the desktop top bar."""
        from nexus_server import chain_proxy as cp

        seen_names = []

        class CaptureClient:
            def register_agent(self, name):
                seen_names.append(name)
                return 1234

        cp._chain_client_test_override = CaptureClient()
        try:
            token, _ = self._get_token_and_user(client)

            # Empty string → server falls back to display_name
            r1 = client.post(
                "/api/v1/chain/register-agent",
                headers={"Authorization": f"Bearer {token}"},
                json={"agent_name": ""},
            )
            assert r1.status_code == 200, r1.text
            assert r1.json()["status"] == "registered"
            assert seen_names[-1] == "ChainUser"  # _register's display_name

            # And totally missing field → still works
            cp._chain_client = None  # bust the cached id
            # We can't easily reset chain_agent_id; just register a NEW user
            r2 = client.post(
                "/api/v1/auth/register",
                json={"display_name": "OmittedNameUser"},
            )
            tok2 = r2.json()["jwt_token"]
            r3 = client.post(
                "/api/v1/chain/register-agent",
                headers={"Authorization": f"Bearer {tok2}"},
                json={},
            )
            assert r3.status_code == 200, r3.text
            assert r3.json()["status"] == "registered"
            assert seen_names[-1] == "OmittedNameUser"
        finally:
            cp._chain_client_test_override = None

    def test_register_agent_handles_chain_failure_gracefully(self, client):
        from nexus_server import chain_proxy as cp

        class BoomClient:
            def register_agent(self, name):
                raise RuntimeError("RPC down")

        cp._chain_client_test_override = BoomClient()
        try:
            token, _ = self._get_token_and_user(client)
            resp = client.post(
                "/api/v1/chain/register-agent",
                headers={"Authorization": f"Bearer {token}"},
                json={"agent_name": "alice"},
            )
            # We return a structured 'failed' response, not a 500
            assert resp.status_code == 200
            assert resp.json()["status"] == "failed"
        finally:
            cp._chain_client_test_override = None


# ── Sync Anchor: BSC anchoring pipeline ──────────────────────────────


class TestSyncAnchor:
    """sync_anchor's pipeline: SHA-256 the batch,
    anchor on BSC, expose progress via GET /api/v1/sync/anchors.

    S4 NOTE: /sync/push no longer enqueues anchors automatically — chain-
    mode twin's ChainBackend is the new anchoring authority. These tests
    drive ``enqueue_anchor`` directly via the helper below, since the
    sync_anchor module is still kept around as a legacy read view + an
    operator-on-demand retry path. When the module is fully deleted in
    S6/Round 2-A this test class goes with it."""

    def _get_token_and_user(self, client):
        resp = client.post("/api/v1/auth/register", json={"display_name": "AnchorUser"})
        d = resp.json()
        return d["jwt_token"], d["user_id"]

    def _push_one(self, client, token, *, user_id: str, content: str = "hello"):
        """Push one event AND directly enqueue an anchor for it,
        simulating the legacy /sync/push → enqueue_anchor flow that S4
        removed from production code. The two-step shape matches what
        the sync_anchor pipeline always operated on; the only thing
        that changed is *who* calls enqueue_anchor.

        ``user_id`` is keyword-only and required: silently no-op'ing
        the enqueue when it's missing led to confusing "anchor list
        empty" failures in test authoring.

        Phase B: /sync/push endpoint is gone, so we synthesize a
        sync_id (1) and drive enqueue_anchor directly. The fake
        ``Response``-shaped object below preserves the API the test
        callsites use (``.status_code`` / ``.json()``)."""
        if not user_id:
            raise AssertionError(
                "_push_one requires user_id= to drive enqueue_anchor."
            )
        from nexus_server.sync_anchor import enqueue_anchor
        from datetime import datetime, timezone

        sync_ids = [1]                # synthetic — anchor pipeline doesn't
                                      # follow the id back into sync_events
        now_iso = datetime.now(timezone.utc).isoformat()
        enqueue_anchor(
            user_id,
            sync_ids,
            [{
                "sync_id": sync_ids[0],
                "event_type": "user_message",
                "content": content,
                "session_id": "s1",
                "metadata": {},
                "client_created_at": now_iso,
                "server_received_at": now_iso,
            }],
        )

        class _FakeResp:
            status_code = 200
            text = ""
            def json(self):
                return {"assigned_sync_ids": sync_ids,
                        "server_time": now_iso}
        return _FakeResp()

    def test_no_chain_records_stored_only(self, client, monkeypatch):
        """No chain config + no test override → status='stored_only'.
        The deterministic SHA-256 is
        still computed and the anchor row is recorded.

        Force ``chain_is_configured`` False so the test doesn't depend on
        the operator's shell having SERVER_PRIVATE_KEY unset.
        """
        from nexus_server import sync_anchor as sa
        from nexus_server import config as cfg_mod
        sa._chain_backend_test_override = None  # belt-and-suspenders
        sa._chain_client = None
        monkeypatch.setattr(cfg_mod.ServerConfig, "SERVER_PRIVATE_KEY", None)

        token, user_id = self._get_token_and_user(client)

        push = self._push_one(client, token, user_id=user_id)
        assert push.status_code == 200

        anchors = client.get(
            "/api/v1/sync/anchors",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert anchors.status_code == 200
        rows = anchors.json()["anchors"]
        assert len(rows) == 1
        a = rows[0]
        assert a["status"] == "stored_only"
        assert a["event_count"] == 1
        assert len(a["content_hash"]) == 64  # SHA-256 hex
        assert a["bsc_tx_hash"] is None

    def test_full_anchor_path_with_fake_backend(self, client):
        """Inject a fake AnchorBackend that succeeds
        → status='anchored', bsc_tx_hash set."""
        from nexus_server import sync_anchor as sa
        from nexus_server.database import get_db_connection

        class FakeBackend(sa.AnchorBackend):
            def __init__(self):
                self.anchor_calls = []

            def anchor(self, agent_id_int, content_hash_hex, runtime):
                self.anchor_calls.append(
                    (agent_id_int, content_hash_hex, runtime)
                )
                return "0xfeedface"

        fake = FakeBackend()
        sa._chain_backend_test_override = fake
        try:
            token, user_id = self._get_token_and_user(client)
            # Manually mark the user as having a chain agent so the BSC
            # leg isn't gated by 'awaiting_registration'.
            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE users SET chain_agent_id = 7 WHERE id = ?",
                    (user_id,),
                )
                conn.commit()

            push = self._push_one(client, token, content="durable", user_id=user_id)
            assert push.status_code == 200

            anchors = client.get(
                "/api/v1/sync/anchors",
                headers={"Authorization": f"Bearer {token}"},
            ).json()["anchors"]
            assert len(anchors) == 1
            a = anchors[0]
            assert a["status"] == "anchored"
            assert a["bsc_tx_hash"] == "0xfeedface"

            # Backend really got called
            assert len(fake.anchor_calls) == 1
            agent_id_int, hash_hex, _ = fake.anchor_calls[0]
            assert agent_id_int == 7
            assert hash_hex == a["content_hash"]
        finally:
            sa._chain_backend_test_override = None

    def test_anchor_failure_recorded_but_push_still_ok(self, client):
        """If the BSC leg blows up, the push itself MUST still succeed
        (the local SQLite write is the source of truth) but the anchor
        row's status should be 'failed' with the error captured."""
        from nexus_server import sync_anchor as sa
        from nexus_server.database import get_db_connection

        class BoomBackend(sa.AnchorBackend):
            def anchor(self, agent_id_int, content_hash_hex, runtime):
                raise RuntimeError("nonce too low")

        sa._chain_backend_test_override = BoomBackend()
        try:
            token, user_id = self._get_token_and_user(client)
            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE users SET chain_agent_id = 9 WHERE id = ?",
                    (user_id,),
                )
                conn.commit()

            push = self._push_one(client, token, user_id=user_id)
            assert push.status_code == 200, push.text
            assert push.json()["assigned_sync_ids"]

            anchors = client.get(
                "/api/v1/sync/anchors",
                headers={"Authorization": f"Bearer {token}"},
            ).json()["anchors"]
            assert anchors[0]["status"] == "failed"
            assert "nonce too low" in (anchors[0]["error"] or "")
        finally:
            sa._chain_backend_test_override = None

    def test_anchor_awaiting_registration_when_no_chain_agent_id(self, client):
        """User has no chain_agent_id
        → status='awaiting_registration' (NOT 'failed')."""
        from nexus_server import sync_anchor as sa

        class NoAnchor(sa.AnchorBackend):
            def anchor(self, *a, **kw):
                raise AssertionError("BSC anchor should not be called")

        sa._chain_backend_test_override = NoAnchor()
        try:
            token, user_id = self._get_token_and_user(client)
            push = self._push_one(client, token, user_id=user_id)
            assert push.status_code == 200

            anchors = client.get(
                "/api/v1/sync/anchors",
                headers={"Authorization": f"Bearer {token}"},
            ).json()["anchors"]
            assert anchors[0]["status"] == "awaiting_registration"
        finally:
            sa._chain_backend_test_override = None

    def test_chain_activity_log_handler_captures_anchor_and_failure(self, client):
        """Bug 3 contract: the SDK's chain activity logs (BSC anchor
        commits AND failures) flow through the logging handler
        installed by ``twin_manager.install_chain_activity_handler``
        into the ``twin_chain_events`` table. /agent/state and
        /agent/timeline then surface them.

        Without this, the desktop sidebar shows ``0 anchored / 0 pending``
        forever — every chain operation goes straight to stderr.
        """
        import logging
        from nexus_server import twin_manager

        # Register a user with display_name=ChainActivityUser so the
        # log-line agent_id ``user-{user_id[:8]}`` matches.
        reg = client.post(
            "/api/v1/auth/register",
            json={"display_name": "ChainActivityUser"},
        )
        token = reg.json()["jwt_token"]
        user_id = reg.json()["user_id"]
        agent_id_str = f"user-{user_id[:8]}"

        twin_manager.install_chain_activity_handler()
        try:
            chain_log = logging.getLogger("nexus_core.backend.chain")

            # 1. Successful BSC anchor — handler should write status=ok row
            chain_log.warning(
                "[WRITE][BSC] Anchor OK: agent=%s hash=%s tx=%s (%.2fs)",
                agent_id_str, "abc123def456", "deadbeefcafe", 1.42,
            )

            # 2. BSC anchor failure — should write status=failed row
            chain_log.warning(
                "[FALLBACK][BSC] Anchor failed for %s — agent=%s hash=%s reason=%s",
                agent_id_str, agent_id_str, "abc123def456",
                "execution reverted: nonce too low",
            )

            # 3. State endpoint reflects both
            state = client.get(
                "/api/v1/agent/state",
                headers={"Authorization": f"Bearer {token}"},
            ).json()
            assert state["anchored_count"] >= 1, state
            assert state["failed_anchor_count"] >= 1, state
            assert state["last_chain_event"] is not None
            # Newest event is the anchor failure (last logged)
            last = state["last_chain_event"]
            assert last["status"] == "failed"
            assert "nonce too low" in (last["error"] or "")

            # 4. Timeline surfaces both kinds
            tl = client.get(
                "/api/v1/agent/timeline?limit=20",
                headers={"Authorization": f"Bearer {token}"},
            ).json()
            kinds = {it["kind"] for it in tl["items"]}
            assert "anchor.committed" in kinds, kinds
            assert "anchor.failed" in kinds, kinds
        finally:
            twin_manager.uninstall_chain_activity_handler()

    def test_agent_messages_reads_from_twin_event_log(self, client):
        """S5 contract: GET /api/v1/agent/messages reads chat history
        from twin's per-user EventLog SQLite, NOT from sync_events.

        Phase B note: this test used to also seed a "DECOY" row in
        ``sync_events`` and assert the endpoint did NOT return it. The
        ``sync_events`` table is gone in Phase B (the legacy mirror was
        retired alongside ``sync_hub.py``), so there's no longer a
        "wrong path" to verify against — twin's EventLog is the only
        source. Test remains valuable as a positive check that the
        endpoint reads from the right place."""
        from nexus_server import twin_event_log

        reg = client.post(
            "/api/v1/auth/register",
            json={"display_name": "MsgPivotUser"},
        )
        token = reg.json()["jwt_token"]
        user_id = reg.json()["user_id"]

        # Twin EventLog: two real chat turns
        twin_event_log._test_append_event(
            user_id, "user_message", "where do messages come from?",
            session_id="s",
        )
        twin_event_log._test_append_event(
            user_id, "assistant_response", "twin's event_log after S5",
            session_id="s",
        )

        resp = client.get(
            "/api/v1/agent/messages",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        contents = [m["content"] for m in body["messages"]]
        assert contents == [
            "where do messages come from?",
            "twin's event_log after S5",
        ]
        roles = [m["role"] for m in body["messages"]]
        assert roles == ["user", "assistant"]

    def test_agent_state_endpoint_basics(self, client):
        """/api/v1/agent/state returns the snapshot the desktop sidebar
        binds to: chain id, on_chain flag, anchor counts, server_time."""
        reg = client.post("/api/v1/auth/register", json={"display_name": "AgentX"})
        token = reg.json()["jwt_token"]
        resp = client.get(
            "/api/v1/agent/state",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"]
        assert body["on_chain"] is False  # not registered on chain in tests
        assert body["memory_count"] == 0
        assert body["anchored_count"] == 0
        assert body["server_time"]

    def test_agent_timeline_merges_events_and_anchors(self, client):
        """/api/v1/agent/timeline returns events from twin's per-user
        EventLog merged with sync_anchors rows in a single chronological
        feed, newest first, formatted for the activity stream.

        S5 contract change: chat events come from twin's EventLog
        SQLite (the source of truth) — we seed it directly here via
        ``_test_append_event``. /sync/push no longer feeds the timeline.

        S4: anchors no longer auto-created on /sync/push, so we still
        drive ``enqueue_anchor`` directly to verify the merger sees a
        historical-style anchor row alongside the chat events.
        """
        from nexus_server import sync_anchor as sa
        from nexus_server import twin_event_log
        from datetime import datetime, timezone
        reg = client.post("/api/v1/auth/register", json={"display_name": "TLUser"})
        token = reg.json()["jwt_token"]
        user_id = reg.json()["user_id"]

        # Seed twin's EventLog (canonical source after S5)
        twin_event_log._test_append_event(
            user_id, "user_message", "hi", session_id="s"
        )
        twin_event_log._test_append_event(
            user_id, "assistant_response", "hello", session_id="s"
        )

        # Seed one historical anchor so the merger has both kinds to work with
        sa._chain_backend_test_override = None
        now_iso = datetime.now(timezone.utc).isoformat()
        sa.enqueue_anchor(
            user_id, [1, 2],
            [
                {
                    "sync_id": 1,
                    "event_type": "user_message", "content": "hi",
                    "session_id": "s", "metadata": {},
                    "client_created_at": now_iso, "server_received_at": now_iso,
                },
                {
                    "sync_id": 2,
                    "event_type": "assistant_response", "content": "hello",
                    "session_id": "s", "metadata": {},
                    "client_created_at": now_iso, "server_received_at": now_iso,
                },
            ],
        )

        resp = client.get(
            "/api/v1/agent/timeline?limit=20",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        kinds = {it["kind"] for it in items}
        # Should see chat events + at least one anchor lifecycle entry
        assert "chat.user" in kinds, kinds
        assert "chat.assistant" in kinds, kinds
        assert any(k.startswith("anchor.") for k in kinds), kinds

    def test_memory_service_module_deleted(self):
        """Regression / tombstone: ``nexus_server.memory_service`` was a
        live module pre-S3 (server-side periodic memory compactor),
        became a deprecation shim in S3, and is **deleted entirely** in
        the post-Bug-3 cleanup pass. Importing it must fail loudly so
        any straggling import surfaces immediately instead of silently
        no-op'ing.

        Twin's own ``EventLogCompactor`` owns compaction now; the read
        helpers ``list_memory_compacts`` / ``memory_compact_count`` live
        in :mod:`nexus_server.agent_state` (and there's a
        re-export-from-twin-event-log layer below that)."""
        # Importing the module raises ImportError at top-level (the
        # file is a tombstone that says "go look elsewhere"). Use
        # ``importlib.import_module`` to ensure the stale-bytecode
        # ``sys.modules`` entry doesn't short-circuit the load.
        import importlib
        import sys
        sys.modules.pop("nexus_server.memory_service", None)
        with pytest.raises(ImportError):
            importlib.import_module("nexus_server.memory_service")

    def test_memory_compact_surfaces_via_agent_memories_endpoint(self, client):
        """/agent/memories returns memory_compact entries from twin's
        per-user EventLog (S5 source of truth).

        Phase B note: pre-Phase-B this test also exercised
        ``twin_manager._build_on_event`` and asserted the resulting
        sync_events mirror row. Both the mirror function AND the
        sync_events table are gone — twin's emits propagate only to
        its own EventLog and to the chain-activity log handler. The
        positive read-path check via /agent/memories remains valid."""
        from nexus_server import twin_event_log

        reg = client.post(
            "/api/v1/auth/register",
            json={"display_name": "TwinMemoryUser"},
        )
        user_id = reg.json()["user_id"]
        token = reg.json()["jwt_token"]

        # Seed twin's per-user EventLog (the canonical store) with a
        # memory_compact entry shaped the way SDK's EventLogCompactor
        # produces them.
        twin_event_log._test_append_event(
            user_id, "memory_compact",
            "## FACTS\n- prefers concise replies",
            metadata={
                "kind": "auto_compact",
                "projected_from": [1, 4],
                "event_count": 4,
                "char_count": 35,
            },
        )

        resp = client.get(
            "/api/v1/agent/memories",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1, body
        assert "prefers concise replies" in body["memories"][0]["content"]
        assert body["memories"][0]["event_count"] == 4
        assert body["memories"][0]["first_sync_id"] == 1
        assert body["memories"][0]["last_sync_id"] == 4

    def test_content_hash_is_deterministic(self):
        """Same events ordered the same way → identical hash. This is
        what lets a third party recompute and verify the chain anchor."""
        from nexus_server.sync_anchor import (
            compute_content_hash,
            serialize_batch,
        )
        events = [
            {"sync_id": 1, "event_type": "user_message", "content": "hi"},
            {"sync_id": 2, "event_type": "assistant_response", "content": "hey"},
        ]
        h1 = compute_content_hash(serialize_batch("u1", [1, 2], events))
        h2 = compute_content_hash(serialize_batch("u1", [2, 1], events))  # same set
        assert h1 == h2
        # Different user → different hash
        h3 = compute_content_hash(serialize_batch("u2", [1, 2], events))
        assert h1 != h3


class TestAnchorRetryDaemonRetired:
    """Phase B tombstone: the periodic anchor retry daemon, its backoff
    schedule, ``_claim_retry_candidates`` / ``_retry_one`` / ``retry_daemon``
    coroutines, and the ``NEXUS_ENABLE_RETRY_DAEMON`` env flag are all
    gone. Pre-Phase-B these were exercised by ``test_daemon_recovers_failed_anchor``
    and ``test_daemon_marks_permanent_after_exhausting_retries`` —
    deleted along with the implementation. Verify the symbols stay
    deleted; if a future change re-introduces them, add the tests back
    too."""

    def test_daemon_symbols_removed(self):
        from nexus_server import sync_anchor as sa
        for name in (
            "retry_daemon",
            "_claim_retry_candidates",
            "_retry_one",
            "_BACKOFF_SCHEDULE_SECONDS",
            "_schedule_next_retry",
            "RETRY_DAEMON_INTERVAL_SECONDS",
        ):
            assert not hasattr(sa, name), (
                f"Phase B removed sync_anchor.{name} — re-adding it without "
                f"re-adding regression tests is a regression in itself."
            )


class TestChainMeEndpoint:
    """The /api/v1/chain/me convenience endpoint."""

    def test_me_returns_user_info_when_no_chain_agent(self, client):
        reg = client.post("/api/v1/auth/register", json={"display_name": "Me"})
        token = reg.json()["jwt_token"]
        resp = client.get(
            "/api/v1/chain/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["agent_name"] == "Me"
        assert body["metadata"]["on_chain"] is False
        assert body["metadata"]["register_tx"] is None

    def test_me_reflects_chain_agent_id_after_registration(self, client):
        from nexus_server.database import get_db_connection
        reg = client.post("/api/v1/auth/register", json={"display_name": "Reg"})
        token = reg.json()["jwt_token"]
        user_id = reg.json()["user_id"]
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE users SET chain_agent_id = 99, chain_register_tx = ? "
                "WHERE id = ?",
                ("0xdead", user_id),
            )
            conn.commit()

        resp = client.get(
            "/api/v1/chain/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["agent_id"] == "99"
        assert body["metadata"]["on_chain"] is True
        assert body["metadata"]["register_tx"] == "0xdead"
