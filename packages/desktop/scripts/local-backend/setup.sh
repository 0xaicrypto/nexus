#!/usr/bin/env bash
# setup.sh — one-time local backend setup for the macOS Nexus desktop app.
#
# Idempotent. Safe to re-run; only does work if a step is missing or stale.
#
# Side effects
# ────────────
#   * Creates ~/Library/Application Support/RuneProtocol/{venv,node_modules,...}
#   * Installs Python (via brew) if 3.11+ not found
#   * Installs Node (via brew) if 20+ not found
#   * Creates Python venv and pip-installs nexus_server + transitive deps
#   * Writes ~/Library/Application Support/RuneProtocol/.setup_complete_v1
#
# Inputs
# ──────
#   $1 (optional)  — absolute path to the rune-protocol repo root.
#                    Required when running from a packaged .app bundle
#                    (so we know where packages/{server,sdk} live).
#                    If omitted, infers from script location (dev mode).
#
# Exit codes
# ──────────
#   0   ok
#   2   brew missing (we tell the user to install Homebrew and bail)
#   3   python install / venv failure
#   4   pip install failure
#   5   (reserved — was: npm install failure)
#
# Why not just bundle Python via PyInstaller?
# ──────────────────────────────────────────
# We will, in Phase 2. For the radiology demo path we agreed: user has
# brew installed, this script handles the rest. Switching to bundled
# binaries later only replaces the *spawn* path — this setup script can
# go away once Phase 2 lands.

set -euo pipefail

# ── Resolve repo root ─────────────────────────────────────────────────
if [[ $# -ge 1 ]]; then
  REPO_ROOT="$1"
else
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
fi

if [[ ! -d "$REPO_ROOT/packages/server" ]] || [[ ! -d "$REPO_ROOT/packages/sdk" ]]; then
  echo "✗ Cannot find packages/server or packages/sdk under $REPO_ROOT" >&2
  exit 1
fi

# ── Where state lives ─────────────────────────────────────────────────
RUNE_HOME="$HOME/Library/Application Support/RuneProtocol"
mkdir -p "$RUNE_HOME"

VENV_DIR="$RUNE_HOME/venv"
SETUP_MARKER="$RUNE_HOME/.setup_complete_v1"
LOG="$RUNE_HOME/setup.log"

# Re-tee everything (stdout + stderr) to a log file so the desktop UI
# can stream progress AND we still have a forensic trail if something
# fails inside `pip` 30 minutes later.
exec > >(tee -a "$LOG") 2>&1
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  Nexus local backend setup — $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "  Home: $RUNE_HOME"
echo "  Repo: $REPO_ROOT"
echo "════════════════════════════════════════════════════════════════"

# ── 1. Homebrew (must be pre-installed; we don't auto-install brew) ──
if ! command -v brew >/dev/null; then
  echo ""
  echo "✗ Homebrew not found."
  echo ""
  echo "Please install Homebrew first:"
  echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  exit 2
fi
echo "✓ brew @ $(brew --prefix)"

# ── 2. Python 3.11+ ───────────────────────────────────────────────────
PY=""
for cand in python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null; then
    ver="$("$cand" -c 'import sys; print("{}.{}".format(*sys.version_info[:2]))' 2>/dev/null || echo "")"
    # Require 3.11 or 3.12 (matches pyproject.toml requires-python).
    if [[ "$ver" == "3.11" ]] || [[ "$ver" == "3.12" ]]; then
      PY="$(command -v "$cand")"
      break
    fi
  fi
done

if [[ -z "$PY" ]]; then
  echo "→ Python 3.11+ not found — installing via brew (this may take a few minutes)"
  brew install python@3.11
  PY="$(brew --prefix)/opt/python@3.11/bin/python3.11"
fi
echo "✓ python @ $PY ($("$PY" -V))"

# ── 3. Node 20+ ───────────────────────────────────────────────────────
if ! command -v node >/dev/null || ! node -e 'process.exit(parseInt(process.versions.node) >= 20 ? 0 : 1)' 2>/dev/null; then
  echo "→ Node 20+ not found — installing via brew"
  brew install node@20
  # brew node@20 is keg-only, prepend it
  export PATH="$(brew --prefix)/opt/node@20/bin:$PATH"
fi
echo "✓ node @ $(command -v node) ($(node -v))"

# ── 3.5. Tesseract OCR binary ─────────────────────────────────────────
# #126 — pytesseract (python wrapper) is shipped via pip as a hard
# dep, but tesseract itself is a C++ binary that needs to be present
# on PATH. Without it, the ocr_image tool returns "binary not found"
# at chat time and the agent falls back to vision-only.
#
# We use brew because:
#   * Apple's CommandLineTools doesn't ship tesseract.
#   * Bundling a precompiled tesseract inside the .app would bloat
#     the .dmg by ~50 MB (libleptonica + 30 language packs).
#   * brew handles the architecture-correct build (Apple Silicon
#     vs Intel) without us needing two binaries in the bundle.
#
# Language packs: brew's default install gives English. For
# Chinese radiology reports + general bilingual UI we also pull
# chi_sim (simplified) and chi_tra (traditional). These add ~25 MB
# to disk but the OCR call hits "lang not installed" errors
# otherwise.
if ! command -v tesseract >/dev/null 2>&1; then
  echo "→ Tesseract OCR not found — installing via brew (one-time, ~30s)"
  brew install tesseract tesseract-lang \
    || { echo "✗ tesseract install failed — OCR tool will run with vision-only fallback"; }
else
  # Check that the Chinese language data is present — if a user
  # had a stale "tesseract base only" install we want to upgrade.
  if ! tesseract --list-langs 2>/dev/null | grep -q "chi_sim"; then
    echo "→ Tesseract present but no Chinese language pack — installing tesseract-lang"
    brew install tesseract-lang \
      || echo "  ⚠ tesseract-lang install failed; OCR will work for English only"
  fi
fi
if command -v tesseract >/dev/null 2>&1; then
  echo "✓ tesseract @ $(command -v tesseract) ($(tesseract --version 2>&1 | head -1))"
fi

# ── 4. Python venv + dependencies ─────────────────────────────────────
if [[ ! -d "$VENV_DIR" ]] || [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "→ Creating Python venv at $VENV_DIR"
  rm -rf "$VENV_DIR"
  "$PY" -m venv "$VENV_DIR" || { echo "✗ venv creation failed"; exit 3; }
fi

VENV_PY="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

echo "→ Upgrading pip / wheel inside the venv (silently if up to date)"
"$VENV_PIP" install --quiet --upgrade pip wheel

# When REPO_ROOT lives inside an .app bundle (Contents/Resources/…)
# the source is read-only and `pip install -e` would fail trying to
# write *.egg-info next to setup.py. In that case install non-editably;
# pip will copy the source into venv/site-packages where we have write
# access. In dev mode we keep -e for fast iterative development.
EDITABLE_FLAG="-e"
case "$REPO_ROOT" in
  */Contents/Resources/*) EDITABLE_FLAG="" ;;
esac

echo "→ Installing nexus packages into the venv (flag: '${EDITABLE_FLAG:-non-editable}')"
# Order matters: sdk (nexus_core) → nexus → server. They reference each
# other via package name in pyproject.toml; pip resolves intra-deps
# locally because all three paths appear in the same install command.
#
# --force-reinstall --no-deps is REQUIRED for the in-bundle path:
# pip checks the installed package's version (0.1.0, never bumped on
# patch updates) and if it matches what's in the venv, skips
# re-install — which means any change to source files, package_data,
# or new modules WON'T land in the venv until the version string
# bumps. Force-reinstall picks up file-level changes (e.g. the new
# starter_packs/ assets). --no-deps avoids re-resolving fastapi /
# uvicorn / pyjwt etc. on every launch (slow + offline-unfriendly).
"$VENV_PIP" install --quiet --force-reinstall --no-deps \
  $EDITABLE_FLAG "$REPO_ROOT/packages/sdk" \
  $EDITABLE_FLAG "$REPO_ROOT/packages/nexus" \
  $EDITABLE_FLAG "$REPO_ROOT/packages/server" \
  || { echo "✗ pip install failed (see $LOG)"; exit 4; }

# First-run also needs the transitive deps (fastapi, stripe, etc.).
# Detect that by checking for fastapi specifically — if it's not in
# the venv, run a full deps-included install once.
if ! "$VENV_PY" -c "import fastapi" 2>/dev/null; then
  echo "→ First-run: installing transitive dependencies"
  "$VENV_PIP" install --quiet \
    "$REPO_ROOT/packages/sdk" \
    "$REPO_ROOT/packages/nexus" \
    "$REPO_ROOT/packages/server" \
    || { echo "✗ deps install failed (see $LOG)"; exit 4; }
fi

# Quick import smoke check — pip exit code can be 0 even when something
# silently failed to register an entry point. Hit the import directly.
# Capture stderr (don't 2>/dev/null) so the actual traceback lands in
# setup.log — diagnosing "import fails after install" was impossible
# previously because the real error was being silently discarded.
if ! IMPORT_ERR="$("$VENV_PY" -c "import nexus_server.main" 2>&1)"; then
  echo "✗ nexus_server failed to import after install. Real error:"
  echo "$IMPORT_ERR" | sed 's/^/  /'
  echo "(full pip + import log: $LOG)"
  exit 4
fi

# Also confirm the console script wrapper landed in $VENV/bin —
# start.sh invokes it by path, not via `python -m`.
if [[ ! -x "$VENV_DIR/bin/nexus-server" ]]; then
  echo "✗ pip didn't install the nexus-server console script wrapper"
  echo "  expected at: $VENV_DIR/bin/nexus-server"
  echo "  check [project.scripts] in packages/server/pyproject.toml"
  exit 4
fi

echo "✓ Python deps installed"

# ── 4.5. Seed user-level .env from bundle ────────────────────────────
#
# nexus_server's `_load_dotenv()` walks paths starting from the
# installed package location (venv/site-packages/nexus_server/.env)
# which never exists in non-editable installs. We solve this by
# materialising ONE merged .env in $RUNE_HOME — start.sh will source
# it before spawning the server, so env vars hit os.environ directly
# and bypass the broken path-walking entirely.
#
# Merge order (later overrides earlier):
#   1. packages/sdk/.env       — network / contract defaults
#   2. packages/nexus/.env     — framework defaults
#   3. packages/server/.env    — server-specific (JWT, API keys, …)
#
# Idempotent: only writes $RUNE_HOME/.env if it doesn't exist yet.
# Users can later edit it directly to swap API keys without re-running
# setup.sh.
USER_ENV="$RUNE_HOME/.env"

# #117 upgrade-safe merge: seed user .env from bundle on first install,
# AND on subsequent runs add any keys present in the bundle that the
# user .env doesn't have yet — without touching keys the user has
# already set locally. This lets new .dmg builds push new keys
# (e.g. NEXUS_RELAY_URL added in #116) into existing installs
# without clobbering custom API keys / overrides.
#
# Algorithm:
#   1. If user .env doesn't exist → full seed (old behaviour).
#   2. If it exists → collect every KEY= line from the bundle .envs
#      (sdk + nexus + server), and for each KEY not already present
#      in the user .env, append it (with a comment header noting it
#      was a delta-merge).
if [[ ! -f "$USER_ENV" ]]; then
  echo "→ Seeding $USER_ENV from bundled defaults (first install)"
  {
    echo "# Nexus runtime config — merged on first setup."
    echo "# Edit this file directly to override (e.g. swap GEMINI_API_KEY)."
    echo "# Re-run setup.sh after deleting this file to re-seed from bundle."
    echo ""
    for src in \
        "$REPO_ROOT/packages/sdk/.env" \
        "$REPO_ROOT/packages/nexus/.env" \
        "$REPO_ROOT/packages/server/.env"; do
      if [[ -f "$src" ]]; then
        echo "# ── from $(basename "$(dirname "$src")")/.env ─────────────"
        grep -v '^\s*#' "$src" | grep -v '^\s*$' || true
        echo ""
      fi
    done
  } > "$USER_ENV"
  chmod 600 "$USER_ENV"
  echo "  wrote $(wc -l < "$USER_ENV") lines → $USER_ENV"
else
  echo "→ $USER_ENV exists; merging any new keys from bundle (preserving user values)"
  added_count=0
  added_lines=()
  for src in \
      "$REPO_ROOT/packages/sdk/.env" \
      "$REPO_ROOT/packages/nexus/.env" \
      "$REPO_ROOT/packages/server/.env"; do
    [[ -f "$src" ]] || continue
    # Walk every KEY=... line in the bundle .env. If the user's .env
    # doesn't already have that KEY (commented OR uncommented), append.
    while IFS='' read -r line || [[ -n "$line" ]]; do
      [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
      [[ "$line" != *"="* ]] && continue
      key="${line%%=*}"
      key="${key#"${key%%[![:space:]]*}"}"
      key="${key%"${key##*[![:space:]]}"}"
      # Match `KEY=` at start-of-line, allowing a leading `#` for
      # comments — we don't want to re-add a key the user has
      # intentionally commented out.
      if ! grep -qE "^\s*#?\s*${key}=" "$USER_ENV"; then
        added_lines+=("$line")
        added_count=$((added_count + 1))
      fi
    done < "$src"
  done
  if (( added_count > 0 )); then
    {
      echo ""
      echo "# ── Bundle merge $(date -u +"%Y-%m-%dT%H:%M:%SZ") — added $added_count new key(s) ─"
      printf '%s\n' "${added_lines[@]}"
    } >> "$USER_ENV"
    echo "  appended $added_count new key(s) to $USER_ENV"
  else
    echo "  no new keys in bundle; user .env unchanged"
  fi
fi

# ── 6. Marker file ───────────────────────────────────────────────────
cat > "$SETUP_MARKER" <<EOF
{
  "version": 1,
  "completed_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "repo_root": "$REPO_ROOT",
  "venv": "$VENV_DIR",
  "python": "$VENV_PY"
}
EOF

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  ✓ Setup complete. Marker: $SETUP_MARKER"
echo "════════════════════════════════════════════════════════════════"
