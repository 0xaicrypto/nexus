#!/usr/bin/env python3
"""Entry point for the PyInstaller-bundled Nexus backend.

When packaged via Tauri sidecar, this gets launched on app startup and
binds the FastAPI server to 127.0.0.1:8001. Logs go to
``~/Library/Logs/Nexus/server.log``.

If you want to run the backend without Tauri (development), use
``uvicorn nexus_server.main:create_app --factory --port 8001`` — this
script exists for the bundled-binary path only.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys


def setup_logging(build_id: str) -> None:
    log_dir = pathlib.Path.home() / "Library" / "Logs" / "Nexus"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Embed build_id into every log line: format becomes
    #   2026-06-13 16:12:28 [INFO] [v0.1.0+20260613.1612.abc1234] nexus.entry: msg
    # so future "which build emitted this" questions are obvious from any
    # log excerpt — no need to correlate timestamps with .dmg artefacts.
    fmt = (
        f"%(asctime)s [%(levelname)s] [v{build_id}] %(name)s: %(message)s"
    )
    handler = logging.FileHandler(log_dir / "server.log")
    handler.setFormatter(logging.Formatter(fmt))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # Echo to stderr too — Tauri captures sidecar stdout/stderr.
    logging.basicConfig(level=logging.INFO, format=fmt)


def main() -> int:
    # If launched by PyInstaller bundle, the runtime tempdir holds our
    # data files. PyInstaller exposes sys._MEIPASS.
    bundled = hasattr(sys, "_MEIPASS")
    if bundled:
        os.environ.setdefault("NEXUS_BUNDLE_ROOT", sys._MEIPASS)

        # When launched from a bundled .app (Tauri sidecar), CWD is "/"
        # (Apple's app-launch convention). Defaults of "./nexus_server.db",
        # "./key_image/" etc. would try to write to root → permission
        # denied. Pin all data paths to a writable per-user directory
        # under ~/Library/Application Support/Nexus/.
        #
        # Devs running `uvicorn nexus_server.main:create_app` from the
        # repo are unaffected — `bundled=False` short-circuits this.
        data_dir = pathlib.Path.home() / "Library" / "Application Support" / "Nexus"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "key_image").mkdir(exist_ok=True)
        (data_dir / "files").mkdir(exist_ok=True)

        os.environ.setdefault("DATABASE_URL", f"sqlite:///{data_dir / 'nexus_server.db'}")
        os.environ.setdefault("NEXUS_KEY_IMAGE_DIR", str(data_dir / "key_image"))
        os.environ.setdefault("NEXUS_FILES_DIR", str(data_dir / "files"))
        os.environ.setdefault("NEXUS_DATA_DIR", str(data_dir))

        # chdir into the data dir as a safety net. nexus_server has a
        # handful of legacy code paths (file storage, OHIF bridge, demo
        # seed) that use relative paths like ``files/<id>/...`` or
        # ``./key_image/...`` — without chdir those land in CWD ("/"
        # for Tauri-launched sidecars) → permission denied at runtime.
        # Wrapping the chdir in a try guards against odd FS states
        # (read-only home, missing dir after race).
        try:
            os.chdir(data_dir)
        except OSError:
            pass

    # Resolve build identity early — must happen before setup_logging so
    # the build_id can be embedded in the log format. Falls back to
    # "unknown" if the generated module is missing (defensive — this
    # would only happen if someone deleted __build_info__.py outright).
    try:
        from nexus_server.__build_info__ import BUILD_ID, BUILD_TIME, GIT_SHA, VERSION
    except Exception:
        BUILD_ID = "unknown"
        BUILD_TIME = "unknown"
        GIT_SHA = "unknown"
        VERSION = "unknown"

    setup_logging(BUILD_ID)
    log = logging.getLogger("nexus.entry")
    log.info("─" * 70)
    log.info("Nexus backend v%s starting (bundled=%s)", BUILD_ID, bundled)
    log.info("  version:   %s", VERSION)
    log.info("  build:     %s", BUILD_TIME)
    log.info("  git:       %s", GIT_SHA)
    log.info("  python:    %s", sys.version.split()[0])
    if bundled:
        log.info("  data dir:  %s", os.environ.get("NEXUS_DATA_DIR"))
        log.info("  database:  %s", os.environ.get("DATABASE_URL"))
    log.info("─" * 70)

    import uvicorn
    # nexus_server.main exposes a `create_app()` factory rather than a
    # module-level `app` (the latter would invoke FastAPI app + DB +
    # router setup at import time, breaking `pytest` / linting). For
    # the bundled binary we call the factory once here.
    from nexus_server.main import create_app
    fastapi_app = create_app()

    # F-bind-127: server binds to ``127.0.0.1`` (deterministic IPv4
    # loopback), NOT the DNS name ``localhost``. On macOS / many Linux
    # configs ``localhost`` resolves to BOTH ``127.0.0.1`` and ``::1``;
    # uvicorn binds to only one of them (whichever resolves first),
    # and if the browser tries the other we get "Backend unreachable".
    #
    # The frontend's baseUrl is still ``http://localhost:8001`` — the
    # browser's DNS resolver picks ``127.0.0.1`` for ``localhost``
    # (universal default), so it lands on the IPv4 socket we just
    # bound. This combo keeps browser-friendliness (origin = DNS
    # name) AND deterministic server binding.
    host = os.environ.get("NEXUS_HOST", "127.0.0.1")
    port = int(os.environ.get("NEXUS_PORT", "8001"))
    log.info("listening on %s:%d", host, port)
    uvicorn.run(fastapi_app, host=host, port=port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
