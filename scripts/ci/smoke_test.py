#!/usr/bin/env python3
"""Smoke import tests — fail fast on packaging / wiring regressions.

Every check here corresponds to a real production bug we hit during
the v0 deploy. Cost: ~50 ms per check, ~1 s total. Worth it.

Bugs this catches (chronologically, with the commit that introduced
each lesson):

  1. setuptools `package-data` missing → ABI JSONs dropped from wheel
     → BSCClient init fails → every user falls into local-only mode.
     (The bug that motivated this whole CICD pass.)

  2. setuptools `packages = ["nexus_server"]` instead of `find:` →
     subpackages (nexus_server.auth, nexus_server.passkey_page) not
     installed → ModuleNotFoundError at server startup.

  3. Dockerfile `pip install -e ./packages/sdk` → editable install
     writes /build/... .pth path that doesn't exist in runtime stage
     → ModuleNotFoundError: nexus_server.

  4. uvicorn entrypoint `nexus_server.main:app` → no module attribute
     `app` exists; the module exposes `create_app()` factory →
     uvicorn refuses to start.

  5. NEXUS_NETWORK accepts `bsc_testnet` (underscore) silently and
     falls back to testnet via substring match → operator can't tell
     mainnet from testnet → wrong-chain anchoring risk.

  6. /healthz vs /health drift between Dockerfile HEALTHCHECK and
     server route table → container marked unhealthy → restart loop.

If you're adding a new "must work or production breaks" invariant,
add a check here. Don't push it down to a unit test that nobody
runs in CI — this file IS the CI gate.

Usage:
    python scripts/ci/smoke_test.py
    # exits 0 on success, 1 on any failure with a clear message.
"""
from __future__ import annotations

import os
import pathlib
import sys
import traceback


# ── Tiny test harness — no pytest dep here so this script can run
# before deps are installed in some CI orderings.

_failures: list[str] = []


def check(name: str, fn) -> None:
    """Run a check. Print PASS/FAIL with the failure message inline."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        _failures.append(f"{name}: {e}")
        print(f"  FAIL  {name}")
        print(f"        {e}")
        if os.getenv("SMOKE_VERBOSE"):
            traceback.print_exc()
    else:
        print(f"  ok    {name}")


# ── 1. nexus_core ABI JSONs are inside the wheel ─────────────────────

def _abi_files_present() -> None:
    import nexus_core  # noqa: PLC0415  — has to be lazy
    abi_dir = pathlib.Path(nexus_core.__file__).parent / "abi"
    required = (
        "AgentStateExtension.json",
        "IIdentityRegistry.json",
        "TaskStateManager.json",
    )
    missing = [name for name in required if not (abi_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"ABI files missing from installed wheel at {abi_dir}: {missing}. "
            f"Check [tool.setuptools.package-data] in packages/sdk/pyproject.toml."
        )


# ── 2. curated MCP catalog also bundled ──────────────────────────────

def _curated_catalog_present() -> None:
    import nexus_core  # noqa: PLC0415
    catalog = (
        pathlib.Path(nexus_core.__file__).parent / "skills" / "curated_mcp.json"
    )
    if not catalog.is_file():
        raise FileNotFoundError(
            f"curated_mcp.json missing at {catalog} — bundle it via "
            f"`\"nexus_core.skills\" = [\"*.json\"]` in package-data."
        )


# ── 3. nexus_server top-level + every subpackage importable ──────────

def _server_subpackages_importable() -> None:
    # Covers regression #2 (setuptools find: not configured) and #3
    # (editable-install path mismatch). Listing the subpackages
    # explicitly because `from nexus_server import *` doesn't actually
    # import submodules.
    import nexus_server  # noqa: F401, PLC0415
    from nexus_server import config  # noqa: F401, PLC0415
    from nexus_server import database  # noqa: F401, PLC0415
    from nexus_server import twin_manager  # noqa: F401, PLC0415
    from nexus_server import chain_proxy  # noqa: F401, PLC0415
    from nexus_server.auth import routes  # noqa: F401, PLC0415
    # If any of the above fail, the import error here is enough.


# ── 4. uvicorn factory works (the :app vs :create_app bug) ───────────

def _create_app_returns_app() -> None:
    # NEXUS_NETWORK has to be valid for create_app() to succeed past
    # config.validate() if we ever wire it into the factory. Keeping the
    # smoke env hermetic so this check works in PR builds too.
    os.environ.setdefault("NEXUS_NETWORK", "bsc-testnet")
    os.environ.setdefault("ENVIRONMENT", "test")
    from nexus_server.main import create_app  # noqa: PLC0415
    app = create_app()
    if app is None:
        raise RuntimeError("create_app() returned None")
    # Cheap smoke that the FastAPI app actually has routes on it (not
    # an empty shell) — covers a router-import-failure regression we
    # haven't hit yet but plausibly could.
    route_paths = {getattr(r, "path", "") for r in app.routes}
    for required in ("/health", "/healthz"):
        if required not in route_paths:
            raise RuntimeError(
                f"FastAPI app missing required route {required}. "
                f"Got {sorted(route_paths)[:10]}..."
            )


# ── 5. NEXUS_NETWORK validation rejects common typos ─────────────────

def _network_validation_rejects_underscore() -> None:
    from nexus_server.config import ServerConfig  # noqa: PLC0415
    cfg = ServerConfig()
    cfg.NEXUS_NETWORK = "bsc_testnet"  # underscore typo → must reject
    cfg.ENVIRONMENT = "development"
    try:
        cfg.validate()
    except ValueError:
        return  # expected
    raise AssertionError(
        "config.validate() should have raised for NEXUS_NETWORK=bsc_testnet "
        "(underscore typo) but it accepted it. Check valid_networks set "
        "in nexus_server/config.py."
    )


# ── 6. chain_proxy._get_chain_client behaves on missing config ───────

def _chain_client_returns_none_when_unconfigured() -> None:
    # If SERVER_PRIVATE_KEY is unset, _get_chain_client() must return
    # None cleanly — not raise. Tests the local-mode fallback path that
    # every PR build relies on (CI doesn't have a real signing key).
    os.environ.pop("SERVER_PRIVATE_KEY", None)
    # Reset the module-level singleton so this check is hermetic.
    from nexus_server import chain_proxy  # noqa: PLC0415
    chain_proxy._chain_client = None
    client = chain_proxy._get_chain_client()
    if client is not None:
        raise AssertionError(
            "Without SERVER_PRIVATE_KEY, _get_chain_client() should "
            f"return None; got {type(client).__name__} instead."
        )


# ── 7. chain_health_snapshot exposes the observability fields ────────

def _chain_health_snapshot_has_new_fields() -> None:
    """Make sure the BSC observability fields stay in chain_health_snapshot.

    A future refactor of chain.py could easily drop these — and the
    desktop's "bsc_failure_active / last_bsc_anchor_error" cards would
    silently revert to misleading green. This is a compile-time-style
    invariant the integration tests would only catch with a real chain
    backend.
    """
    # Walk a synthetic instance — chain.py's ChainBackend has a heavy
    # __init__ that needs BSC config. We only want the schema check,
    # so stub the slot-level state via __new__.
    from nexus_core.backends import chain as chain_mod  # noqa: PLC0415
    backend = chain_mod.ChainBackend.__new__(chain_mod.ChainBackend)
    backend._chain_client = None
    backend._last_bsc_anchor_failure_at = None
    backend._last_bsc_anchor_error = None

    snap = backend.chain_health_snapshot()

    required = {
        "bsc_ready",
        "bsc_failure_active", "last_bsc_anchor_error",
    }
    missing = required - set(snap.keys())
    if missing:
        raise AssertionError(
            f"chain_health_snapshot() missing fields: {sorted(missing)}. "
            f"Got: {sorted(snap.keys())}. The desktop's degraded-state UI "
            f"depends on these — don't drop them."
        )


# ── Driver ────────────────────────────────────────────────────────────


def main() -> int:
    print("Smoke tests for nexus-server packaging + wiring")
    print("─" * 60)
    check("nexus_core ABI JSONs in wheel",            _abi_files_present)
    check("curated_mcp.json catalog in wheel",        _curated_catalog_present)
    check("nexus_server subpackages importable",      _server_subpackages_importable)
    check("create_app() returns wired FastAPI app",   _create_app_returns_app)
    check("NEXUS_NETWORK rejects underscore typo",    _network_validation_rejects_underscore)
    check("chain_proxy returns None when unconfig.",  _chain_client_returns_none_when_unconfigured)
    check("chain_health_snapshot has degraded fields", _chain_health_snapshot_has_new_fields)
    print("─" * 60)
    if _failures:
        print(f"FAILED: {len(_failures)} check(s)")
        for f in _failures:
            print(f"  • {f}")
        return 1
    print("All smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
