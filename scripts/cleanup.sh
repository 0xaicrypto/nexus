#!/usr/bin/env bash
# Post-build cleanup — run AFTER ./packages/desktop-v2/scripts/build-macos.sh
# completes and the .dmg is safely in your hands.
#
# Tiers:
#   --safe      : delete only obviously-safe items (build artifacts, caches)
#   --aggressive: also delete the top-level .venv (Python 3.14 leftover —
#                 the actual build uses packages/server/.venv on 3.12).
#                 The legacy packages/desktop/ Avalonia tree has been
#                 removed from the repo; recover it from git tag
#                 legacy/avalonia-final if you ever need it.
#
# Default: --safe
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="${1:---safe}"

say() { printf "\033[1;34m▶\033[0m %s\n" "$*"; }
ok()  { printf "\033[1;32m✓\033[0m %s\n" "$*"; }
warn(){ printf "\033[1;33m⚠\033[0m %s\n" "$*"; }

# ── Sanity checks ────────────────────────────────────────────────────
if pgrep -f "pyinstaller\|vite\|tauri\|cargo build" >/dev/null 2>&1; then
  warn "Detected a running build process. Aborting to avoid corruption."
  warn "Wait for build to finish, then re-run this script."
  exit 1
fi

# ── Tier A: System garbage (always safe) ─────────────────────────────
say "Removing .DS_Store + empty placeholder dirs"
find . -type f -name ".DS_Store" -delete 2>/dev/null || true
[ -d .git_ignored_test_dir ] && rmdir .git_ignored_test_dir 2>/dev/null
[ -d .test_mv_b ] && rmdir .test_mv_b 2>/dev/null
ok "Tier A done"

# ── Tier B: Build artifacts (regenerated next build) ─────────────────
say "Removing PyInstaller intermediates + egg-info"
rm -rf packages/server/build/
rm -rf packages/server/dist/
find packages -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
ok "Tier B done"

# ── Tier C: Python bytecode (regenerated on import) ──────────────────
say "Removing __pycache__ outside .venv and node_modules"
find . -type d -name "__pycache__" \
  -not -path "*/.venv/*" \
  -not -path "*/node_modules/*" \
  -not -path "*/target/*" \
  -exec rm -rf {} + 2>/dev/null || true
ok "Tier C done"

if [ "$MODE" = "--aggressive" ]; then
  # ── Tier D: Stale workspaces ───────────────────────────────────────
  say "Tier D: Removing stale workspaces (--aggressive)"

  if [ -d .venv ] && [ -L .venv/bin/python ]; then
    py_link="$(readlink .venv/bin/python)"
    case "$py_link" in
      *python3.14*)
        warn "Top-level .venv (Python 3.14) — build uses packages/server/.venv (3.12)"
        warn "Deleting top-level .venv"
        rm -rf .venv
        ok "top-level .venv removed"
        ;;
      *)
        warn "Top-level .venv points to $py_link — not auto-removing"
        ;;
    esac
  fi

  ok "Tier D done"
fi

# ── Report ───────────────────────────────────────────────────────────
echo
say "Project size after cleanup:"
du -sh . 2>/dev/null
du -sh packages/* 2>/dev/null | sort -h
