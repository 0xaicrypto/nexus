# syntax=docker/dockerfile:1.6
#
# Nexus server — single-image deploy (Python + Node).
#
# Two stages:
#   1. builder  — installs Python deps via uv into a venv. Pre-cached
#                 in CI layers so most rebuilds skip dependency resolve.
#   2. runtime  — slim Debian + Python 3.11 + Node 20 (for MCP installs)
#                 + a non-root nexus user. App code is copied last so
#                 source changes don't bust the heavier dep layer.
#
# Why Node lives in the runtime image: the agent is supposed to install
# new MCP servers + skills at chat time via manage_skill / manage_mcp,
# both of which shell out to `npx`. If Node isn't here, those tools
# fail with "npx not found" and the user has to redeploy. We ship Node
# so the "agent installs its own tools without code changes" promise
# actually holds.

# ── Stage 1: build (Python deps via uv) ───────────────────────────────
FROM python:3.11-slim-bookworm AS builder

# uv is the fastest known installer for the FastAPI + web3 + google-genai
# tree (the previous Poetry-based image took ~6 min to lock; uv does it
# in <30 s and the locked manifest is reproducible).
RUN pip install --no-cache-dir uv==0.4.30

WORKDIR /build

# Copy ONLY pyproject + lock first so dep resolution is cached
# independently of source code changes. (Per-package READMEs were
# removed — root README.md is the single source of doc truth, and
# pip is happy as long as pyproject.toml's readme field doesn't
# point at a missing file.)
COPY packages/sdk/pyproject.toml      packages/sdk/pyproject.toml
COPY packages/nexus/pyproject.toml    packages/nexus/pyproject.toml
COPY packages/server/pyproject.toml   packages/server/pyproject.toml
COPY README.md                        README.md

# Install all three packages editable into a single venv.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip wheel

# Source code is needed for the editable install to work — copy now.
COPY packages/sdk    packages/sdk
COPY packages/nexus  packages/nexus
COPY packages/server packages/server

RUN /opt/venv/bin/pip install --no-cache-dir \
        ./packages/sdk \
        ./packages/nexus \
        ./packages/server
# NOTE: deliberately NOT using `pip install -e ...` (editable) here.
# Editable installs write the source-tree absolute path into a `.pth`
# file inside the venv. In multi-stage Docker builds the builder stage's
# WORKDIR (`/build/...`) doesn't exist in the runtime stage (which only
# has `/app/...` after the COPY --from=builder step), so the .pth path
# resolves to nothing and Python can't find `nexus_server` at runtime
# (`ModuleNotFoundError: No module named 'nexus_server'`).
# Plain (non-editable) install copies the package contents into the
# venv's site-packages directly, decoupling import resolution from the
# source-tree path. The downside (no live edits without rebuild) is a
# non-issue for production images.

# ── Stage 2: runtime (slim + Node + non-root user) ───────────────────
FROM python:3.11-slim-bookworm AS runtime

# Node 22 LTS ("Jod" — LTS through April 2027). Two purposes:
#   1. MCP server installs (agent calls `npx -y mcp-...` at runtime).
#   2. The `manage_skill` tool's marketplace search/install path — it
#      shells out to `npx -y @lobehub/market-cli ...`. That CLI's
#      `package.json` declares `engines.node: ">=22.0.0"`, and on
#      Node 20 its underlying `@lobehub/market-sdk` ESM imports of
#      `jose` blow up with `ERR_MODULE_NOT_FOUND` because Node 20's
#      ESM resolver doesn't honour the `exports` field shape `jose`
#      uses. So Node 20 = manage_skill search ALWAYS errors out and
#      the user can't install pdf/xlsx/etc. Skills hub just won't
#      work without bumping Node, full stop.
#
# curl + ca-certificates for the NodeSource bootstrap and any outbound
# HTTPS the agent needs (BSC RPC, Gemini, Tavily).
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && apt-get purge -y --auto-remove gnupg \
 && rm -rf /var/lib/apt/lists/* \
 && node --version && npm --version

# Non-root user — important for the volume mounts below: skills /
# uploads / db get written under /data with this UID, so a host-side
# `chown -R 1000:1000 /var/lib/nexus` is sufficient.
RUN useradd --create-home --uid 1000 --shell /bin/bash nexus

# Copy the venv from the builder stage.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# App code (already inside the venv as editable installs).
COPY --from=builder --chown=nexus:nexus /build /app
WORKDIR /app

# Persistent state lives under /data — this is the ONLY directory the
# host needs to back up. Layout:
#   /data/db/                — rune_server.db (SQLite)
#   /data/twins/<user_id>/   — per-user event log, skills, persona, etc.
#   /data/uploads/<user_id>/ — file uploads
#   /data/cache/             — NEXUS_CACHE_DIR (chain identity, ABI cache)
RUN mkdir -p /data/db /data/twins /data/uploads /data/cache \
 && chown -R nexus:nexus /data
VOLUME ["/data"]

# Defaults wired so the volume above gets all the right things.
ENV NEXUS_TWIN_BASE_DIR=/data/twins \
    UPLOAD_DIR=/data/uploads \
    NEXUS_CACHE_DIR=/data/cache \
    DATABASE_URL=sqlite:////data/db/rune_server.db \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8001

USER nexus

# Healthcheck: hit the FastAPI /healthz that main.py exposes. If the
# server doesn't define one, swap to /docs which always 200s.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8001/healthz || \
        curl --fail --silent http://127.0.0.1:8001/docs || exit 1

EXPOSE 8001

# uvicorn directly, not the `nexus-server` console script — gives us
# explicit log config + worker count control via env vars.
# `nexus_server.main` exposes `create_app()` (an app factory), NOT a
# top-level `app` variable. uvicorn needs --factory to know to call it.
# An earlier rev pointed at `:app` and crash-looped with
# `Attribute "app" not found in module "nexus_server.main"` because
# create_app is a callable, not a module attribute named "app".
CMD ["uvicorn", "nexus_server.main:create_app", \
     "--host", "0.0.0.0", \
     "--port", "8001", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--factory"]
