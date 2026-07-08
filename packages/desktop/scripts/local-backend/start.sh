#!/usr/bin/env bash
# start.sh — boot the local nexus_server as
# background processes, wait for healthz, write runtime.json.
#
# This is what the desktop calls every launch (after setup.sh has run
# once). Idempotent: if a previous runtime.json is still pointing at a
# live server, we reuse it instead of spawning a duplicate.
#
# Exit codes
# ──────────
#   0  ok — runtime.json is current, healthz passed
#   1  setup not done (marker missing)
#   2  port acquisition failed
#   3  python server didn't come up within timeout
#   4  (reserved — was: helper daemon didn't come up within timeout)
#   5  already-running stale state was unrecoverable
#
# Output on stdout (last line):
#   PORT=<int>
# Desktop reads this to know which port to talk to. Also reflected in
# runtime.json for cross-process discovery.

set -euo pipefail

RUNE_HOME="$HOME/Library/Application Support/RuneProtocol"
SETUP_MARKER="$RUNE_HOME/.setup_complete_v1"
RUNTIME_JSON="$RUNE_HOME/runtime.json"
LOG_SERVER="$RUNE_HOME/server.log"
LOG_DAEMON="$RUNE_HOME/daemon.log"
START_LOG="$RUNE_HOME/start.log"

exec > >(tee -a "$START_LOG") 2>&1
echo ""
echo "── start.sh @ $(date -u +"%Y-%m-%dT%H:%M:%SZ") ──"

# ── 1. Setup must have completed ─────────────────────────────────────
if [[ ! -f "$SETUP_MARKER" ]]; then
  echo "✗ Setup marker missing at $SETUP_MARKER — run setup.sh first."
  exit 1
fi

# Pull venv path + repo root from the marker (set during setup)
REPO_ROOT="$(python3 -c "import json; print(json.load(open('$SETUP_MARKER'))['repo_root'])")"
VENV_PY="$(python3 -c "import json; print(json.load(open('$SETUP_MARKER'))['python'])")"
# Derive VENV_DIR from VENV_PY: $VENV_PY = $VENV_DIR/bin/python → VENV_DIR is two-dirs-up.
VENV_DIR="$(dirname "$(dirname "$VENV_PY")")"
echo "  venv python: $VENV_PY"
echo "  venv dir   : $VENV_DIR"
echo "  repo root  : $REPO_ROOT"

# ── 1b. Detect source vs venv drift ────────────────────────────────────
# setup.sh writes the marker ONCE on first install. Subsequent .app
# updates (new dmg with new source) would reuse the existing venv —
# meaning new files added to nexus_server/ (e.g. new submodules,
# starter_packs/ assets) never landed in site-packages, and the running
# server kept loading the old code. Symptom: new routes 404, new
# modules ImportError, new resources missing.
#
# Fix: hash the bundled server source tree + compare to the hash we
# recorded last time we installed it. Mismatch → quick pip
# --force-reinstall --no-deps (5-10s, fast enough to do on launch).
BUNDLE_HASH_FILE="$RUNE_HOME/.bundle_hash"
# #154 — extended the file-type list to include .html / .css / .js /
# .toml / .txt. The Cornerstone3D viewer page (#143) and pyproject.toml's
# package-data rules live in those extensions; without them, edits to
# the viewer HTML or to the package-data globs (e.g. shipping static/)
# DON'T bump the hash → start.sh skips reinstall → venv keeps the old
# (or missing) assets and the mounted route 404s.
CURRENT_HASH="$(find \
  "$REPO_ROOT/packages/server/nexus_server" \
  "$REPO_ROOT/packages/sdk/nexus_core" \
  "$REPO_ROOT/packages/nexus" \
  "$REPO_ROOT/packages/server" \
  -type f \( \
    -name '*.py'   -o -name '*.json' -o -name '*.md'   -o -name '*.yaml' \
    -o -name '*.html' -o -name '*.css' -o -name '*.js' \
    -o -name '*.toml' -o -name '*.txt' \) \
  -not -path '*/node_modules/*' -not -path '*/__pycache__/*' \
  -exec stat -f '%m %z %N' {} \; 2>/dev/null \
  | sort | shasum | cut -c1-12)"
INSTALLED_HASH="$(cat "$BUNDLE_HASH_FILE" 2>/dev/null || echo "")"

BUNDLE_CHANGED=0
if [[ -n "$CURRENT_HASH" ]] && [[ "$CURRENT_HASH" != "$INSTALLED_HASH" ]]; then
  BUNDLE_CHANGED=1
  echo "→ bundle source changed ($INSTALLED_HASH → $CURRENT_HASH); reinstalling nexus packages"
  VENV_PIP="$(dirname "$VENV_PY")/pip"
  if "$VENV_PIP" install --quiet --force-reinstall --no-deps \
       "$REPO_ROOT/packages/sdk" \
       "$REPO_ROOT/packages/nexus" \
       "$REPO_ROOT/packages/server" 2>"$RUNE_HOME/reinstall.log"; then
    echo "$CURRENT_HASH" > "$BUNDLE_HASH_FILE"
    echo "  ✓ reinstall ok"
    # Wipe Python bytecode caches so the next server import compiles
    # from the freshly-installed .py files. Without this, .pyc files
    # whose source-file mtime matches the cached header get reused,
    # and any module the new install removed (e.g. workflow_rescue.py
    # in #91) still resolves from the leftover .pyc.
    find "$VENV_DIR" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
  else
    echo "  ⚠ reinstall failed (see $RUNE_HOME/reinstall.log); running with stale venv"
  fi
fi

# ── 2. Check for an existing live runtime ────────────────────────────
# If runtime.json points at a live server AND the bundle source hasn't
# changed since the running process was spawned, reuse — don't double-
# spawn. If the bundle DID change (line 69-81 above), the running
# server is still holding the OLD bytecode in memory (Python doesn't
# hot-reload), so we MUST force a restart even if it's healthy.
if [[ "$BUNDLE_CHANGED" == "1" ]] && [[ -f "$RUNTIME_JSON" ]]; then
  echo "  bundle changed → forcing restart of running server (old bytecode in memory)"
  bash "$(dirname "${BASH_SOURCE[0]}")/stop.sh" || true
  rm -f "$RUNTIME_JSON"
fi

# ── #171: defensive tesseract OCR binary check ────────────────────────
# setup.sh installs tesseract on first run, but a medic might
# `brew uninstall tesseract` later (or restore a Time Machine backup
# from before the install). Cheap check on every launch keeps OCR
# working without forcing a full setup.sh re-run. Brew install is
# idempotent and fast when already present (~50 ms).
if ! command -v tesseract >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "→ tesseract missing, restoring via brew (background, won't block launch)"
    (brew install tesseract tesseract-lang >/dev/null 2>&1 || true) &
  else
    echo "⚠ tesseract missing AND brew unavailable — ocr_image tool will return fallback errors"
  fi
fi
if [[ -f "$RUNTIME_JSON" ]]; then
  EXISTING_PORT="$(python3 -c "import json; print(json.load(open('$RUNTIME_JSON')).get('port',''))" 2>/dev/null || true)"
  EXISTING_SERVER_PID="$(python3 -c "import json; print(json.load(open('$RUNTIME_JSON')).get('server_pid',''))" 2>/dev/null || true)"
  if [[ -n "$EXISTING_PORT" ]] && [[ -n "$EXISTING_SERVER_PID" ]]; then
    if kill -0 "$EXISTING_SERVER_PID" 2>/dev/null \
       && curl -sf --max-time 2 "http://localhost:$EXISTING_PORT/healthz" >/dev/null 2>&1; then
      echo "✓ existing server alive @ port $EXISTING_PORT (pid $EXISTING_SERVER_PID); reusing"
      echo "PORT=$EXISTING_PORT"
      exit 0
    fi
    # Stale runtime.json — clean it up before spawning new procs so we
    # don't end up with two competing nexus_servers on the loopback.
    echo "  stale runtime.json (server pid $EXISTING_SERVER_PID not alive or unhealthy) — replacing"
    bash "$(dirname "${BASH_SOURCE[0]}")/stop.sh" || true
  fi
fi

# ── 3. Pick a free port ──────────────────────────────────────────────
PORT=$("$VENV_PY" - <<'PY' 2>/dev/null
import socket
s = socket.socket(); s.bind(('localhost', 0))
print(s.getsockname()[1])
s.close()
PY
) || { echo "✗ failed to acquire a free port"; exit 2; }
echo "  picked port: $PORT"

# ── 4. Spawn the Python server ───────────────────────────────────────
# We use the `nexus-server` console script that pyproject.toml's
# [project.scripts] declares — it's just a thin wrapper around
# `nexus_server.main:run_server`, but invoking it by path is more
# robust than `python -m nexus_server` (the package has no
# __main__.py and never will — main lives in main.py).
#
# We disown so the server survives even if start.sh's parent dies
# unexpectedly. The C# wrapper will SIGTERM by PID at shutdown.
VENV_BIN="$(dirname "$VENV_PY")"
SERVER_CMD="$VENV_BIN/nexus-server"
if [[ ! -x "$SERVER_CMD" ]]; then
  echo "✗ nexus-server console script missing at $SERVER_CMD"
  echo "  setup.sh should have installed it via pip; re-run setup.sh:"
  echo "    rm '$SETUP_MARKER' && ./setup.sh"
  exit 3
fi

# ── Merge new bundle keys into user .env (#120) ──────────────────────
# setup.sh's merge logic only runs on first install. When a user
# upgrades the .dmg, setup.sh is skipped (marker exists), so new
# keys added to packages/server/.env (e.g. NEXUS_RELAY_URL) never
# propagate. Mirror the merge here so EVERY launch checks the bundle
# and appends missing keys to the user .env — idempotent, preserves
# all user overrides.
USER_ENV="$RUNE_HOME/.env"
if [[ -f "$USER_ENV" ]]; then
  bundle_added_count=0
  bundle_added_lines=()
  for src in \
      "$REPO_ROOT/packages/sdk/.env" \
      "$REPO_ROOT/packages/nexus/.env" \
      "$REPO_ROOT/packages/server/.env"; do
    [[ -f "$src" ]] || continue
    while IFS='' read -r line || [[ -n "$line" ]]; do
      [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
      [[ "$line" != *"="* ]] && continue
      key="${line%%=*}"
      key="${key#"${key%%[![:space:]]*}"}"
      key="${key%"${key##*[![:space:]]}"}"
      if ! grep -qE "^[[:space:]]*#?[[:space:]]*${key}=" "$USER_ENV"; then
        bundle_added_lines+=("$line")
        bundle_added_count=$((bundle_added_count + 1))
      fi
    done < "$src"
  done
  if (( bundle_added_count > 0 )); then
    {
      echo ""
      echo "# ── Bundle merge $(date -u +"%Y-%m-%dT%H:%M:%SZ") (start.sh) — added $bundle_added_count key(s) ─"
      printf '%s\n' "${bundle_added_lines[@]}"
    } >> "$USER_ENV"
    echo "  appended $bundle_added_count new bundle key(s) to user .env"
  fi
fi

# ── Load the user-level .env (seeded by setup.sh, merged on launch) ─
# nexus_server's _load_dotenv path-walks from its install location,
# which doesn't work in non-editable bundle mode. We instead inject
# every KEY=VALUE pair into our shell env BEFORE spawning the server.
#
# We can't just `source` the .env: dotenv files routinely contain
# unquoted values with spaces (e.g. WEBAUTHN_RP_NAME=Nexus Desktop)
# which bash splits into "command not found" errors. Parse it line by
# line instead, splitting on the FIRST '=' only, and `export` each
# pair so the literal value (spaces, equals signs, anything) is
# preserved verbatim into the child process's os.environ.
if [[ -f "$USER_ENV" ]]; then
  echo "  loading $USER_ENV"
  while IFS='' read -r line || [[ -n "$line" ]]; do
    # Skip blanks and comments.
    [[ -z "$line" ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    # Must contain '='; otherwise it's malformed — log and skip.
    if [[ "$line" != *"="* ]]; then
      echo "    ⚠ skipping malformed line: ${line:0:60}"
      continue
    fi
    key="${line%%=*}"
    val="${line#*=}"
    # Trim surrounding whitespace on key (don't touch value — spaces
    # in values are intentional).
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    # Strip a matching pair of surrounding quotes from the value, if
    # the user wrote KEY="foo" or KEY='foo'.
    if [[ "$val" =~ ^\".*\"$ ]] || [[ "$val" =~ ^\'.*\'$ ]]; then
      val="${val:1:${#val}-2}"
    fi
    export "$key=$val"
  done < "$USER_ENV"
else
  echo "  ⚠ no $USER_ENV found; server will run with defaults only"
  echo "    (run setup.sh once to seed it from the bundled .env files)"
fi

echo "→ launching nexus_server"
# cwd → $RUNE_HOME so any future relative-path bug in server / deps
# writes to user data, not the read-only .app bundle.
cd "$RUNE_HOME"
cd "$RUNE_HOME"
RUNE_HOME_EXPORT="$RUNE_HOME" \
NEXUS_LOG_DIR="$RUNE_HOME" \
NEXUS_ALLOW_ORPHAN_RECOVERY=1 \
nohup "$SERVER_CMD" \
  --port "$PORT" \
  --host localhost \
  > "$LOG_SERVER" 2>&1 &
SERVER_PID=$!
disown $SERVER_PID
echo "  server pid: $SERVER_PID"

# ── 5. (removed) helper daemon ───────────────────────────────────────
# The decentralised object-storage daemon was removed along with its
# data plane; runtime.json keeps a null daemon_pid for shape compat.
DAEMON_PID=""

# ── 6. Wait for healthz ──────────────────────────────────────────────
echo -n "→ waiting for healthz "
deadline=$(( $(date +%s) + 30 ))
while (( $(date +%s) < deadline )); do
  if curl -sf --max-time 2 "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
    echo " ✓ ready"
    break
  fi
  # Quick sanity: if the server already died, fail fast instead of
  # waiting the full 30s timeout.
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo ""
    echo "✗ nexus_server died during startup. Tail of $LOG_SERVER:"
    tail -n 30 "$LOG_SERVER"
    exit 3
  fi
  echo -n "."
  sleep 0.5
done

if ! curl -sf --max-time 2 "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
  echo ""
  echo "✗ healthz never returned within 30s. Tail of $LOG_SERVER:"
  tail -n 30 "$LOG_SERVER" || true
  # Best-effort kill the spawned procs to avoid orphans.
  [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null || true
  [[ -n "$DAEMON_PID" ]] && kill "$DAEMON_PID" 2>/dev/null || true
  exit 3
fi

# ── 7. Write runtime.json ────────────────────────────────────────────
cat > "$RUNTIME_JSON" <<EOF
{
  "version": 1,
  "started_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "url": "http://localhost:$PORT",
  "port": $PORT,
  "server_pid": $SERVER_PID,
  "daemon_pid": ${DAEMON_PID:-null},
  "server_log": "$LOG_SERVER",
  "daemon_log": "$LOG_DAEMON"
}
EOF
echo "  wrote $RUNTIME_JSON"

echo ""
echo "✓ backend running"
echo "PORT=$PORT"
