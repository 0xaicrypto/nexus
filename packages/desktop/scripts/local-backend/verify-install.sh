#!/usr/bin/env bash
# verify-install.sh — sanity-check that the Nexus local backend is
# correctly set up on the user's mac.
#
# Run AFTER first-launching Nexus.app once (so setup.sh has seeded
# .env, venv exists, runtime.json points at a live server).
#
# Output: a tidy ✓/✗ list with all key VALUES redacted. Safe to paste
# back to support / send a screenshot of.
#
# Usage:
#   bash /Applications/Nexus.app/Contents/Resources/backend-source/packages/desktop/scripts/local-backend/verify-install.sh
#
# Or with the .app open and running, just:
#   ~/Library/Application\ Support/RuneProtocol/verify.sh
#   (we copy this script there during setup for convenience — TODO)

set -u
RUNE_HOME="$HOME/Library/Application Support/RuneProtocol"

pass=0
fail=0
warn=0

ok()   { echo "  ✓ $1";       pass=$((pass+1)); }
bad()  { echo "  ✗ $1";       fail=$((fail+1)); }
note() { echo "  ⚠ $1";       warn=$((warn+1)); }

print_header() {
  echo ""
  echo "── $1 ──"
}

# ── 1. Filesystem layout ─────────────────────────────────────────────
print_header "Files in \$RUNE_HOME"
[[ -d "$RUNE_HOME" ]] && ok "$RUNE_HOME exists" || { bad "$RUNE_HOME missing"; exit 1; }
[[ -f "$RUNE_HOME/.setup_complete_v1" ]] && ok ".setup_complete_v1 marker present" || bad ".setup_complete_v1 missing — launch Nexus.app once to run setup"
[[ -d "$RUNE_HOME/venv/bin" ]] && ok "venv/ present" || bad "venv/ missing — re-run setup"
[[ -f "$RUNE_HOME/.env" ]]     && ok ".env present"   || bad ".env missing — re-run setup (rm marker)"

# ── 2. .env content sanity (value-redacted) ──────────────────────────
print_header "Required keys in \$RUNE_HOME/.env (values hidden)"
if [[ -f "$RUNE_HOME/.env" ]]; then
  required=(
    GEMINI_API_KEY
    SERVER_PRIVATE_KEY
    NEXUS_PRIVATE_KEY
    NEXUS_TESTNET_RPC
    NEXUS_TESTNET_AGENT_STATE_ADDRESS
    NEXUS_TESTNET_IDENTITY_REGISTRY
  )
  for k in "${required[@]}"; do
    # Take the LAST occurrence (matches start.sh's "last export wins")
    val="$(grep -E "^${k}=" "$RUNE_HOME/.env" | tail -1 | sed 's/^[^=]*=//')"
    if [[ -z "$val" ]]; then
      bad "$k is missing or empty"
    else
      # Show length to differentiate empty-string from placeholder
      ok "$k present (len=${#val})"
    fi
  done

  # Optional but common
  for k in DEFAULT_LLM_PROVIDER DEFAULT_LLM_MODEL NEXUS_NETWORK; do
    val="$(grep -E "^${k}=" "$RUNE_HOME/.env" | tail -1 | sed 's/^[^=]*=//')"
    if [[ -n "$val" ]]; then
      ok "$k = $val"   # OK to show — these are non-secret config
    else
      note "$k not set (will use code default)"
    fi
  done
fi

# ── 2.5. External binaries (brew-managed dependencies) ──────────────
# #171 — OCR depends on the tesseract system binary. We installed
# it in setup.sh, but the medic might have removed brew packages
# since. Surface the state here so support requests get a fast
# answer to "why isn't OCR working".
print_header "External binaries"
if command -v tesseract >/dev/null 2>&1; then
  ver="$(tesseract --version 2>&1 | head -1 | sed 's/^tesseract //')"
  ok "tesseract present (v${ver})"
  if tesseract --list-langs 2>/dev/null | grep -q "chi_sim"; then
    ok "tesseract has Chinese language pack (chi_sim)"
  else
    note "tesseract missing chi_sim — OCR will be English-only. Fix: brew install tesseract-lang"
  fi
else
  bad "tesseract not installed — ocr_image tool will fail. Fix: brew install tesseract tesseract-lang"
fi
if command -v brew >/dev/null 2>&1; then
  ok "brew @ $(brew --prefix)"
else
  note "brew not on PATH — setup.sh's package installs won't run on next launch"
fi

# ── 3. Backend process actually running ──────────────────────────────
print_header "Backend process state"
if [[ -f "$RUNE_HOME/runtime.json" ]]; then
  ok "runtime.json present"
  PORT="$(/usr/bin/python3 -c "import json; print(json.load(open('$RUNE_HOME/runtime.json')).get('port',''))" 2>/dev/null)"
  SERVER_PID="$(/usr/bin/python3 -c "import json; print(json.load(open('$RUNE_HOME/runtime.json')).get('server_pid',''))" 2>/dev/null)"
  DAEMON_PID="$(/usr/bin/python3 -c "import json; print(json.load(open('$RUNE_HOME/runtime.json')).get('daemon_pid',''))" 2>/dev/null)"
  [[ -n "$PORT" ]] && ok "port = $PORT" || bad "port missing in runtime.json"

  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    ok "server pid $SERVER_PID alive"
  else
    bad "server pid $SERVER_PID not alive"
  fi

  # Helper daemon was removed along with the decentralised
  # object-storage data plane — daemon_pid stays null by design.
else
  bad "runtime.json missing — Nexus.app not started or backend failed to boot"
  PORT=""; SERVER_PID=""; DAEMON_PID=""
fi

# ── 4. Server actually inherited the keys we exported ────────────────
# `ps eww` dumps the process's env — most reliable way to confirm
# start.sh's export reached the spawned server.
print_header "Server process env (confirms start.sh exported the keys)"
if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
  env_dump="$(ps eww "$SERVER_PID" 2>/dev/null | tr ' ' '\n')"
  for k in GEMINI_API_KEY SERVER_PRIVATE_KEY NEXUS_PRIVATE_KEY; do
    if echo "$env_dump" | grep -q "^${k}="; then
      ok "$k visible in server process env"
    else
      bad "$k NOT in server process env (start.sh didn't export it)"
    fi
  done
else
  note "skipped — server not running"
fi

# ── 5. HTTP healthz ──────────────────────────────────────────────────
print_header "HTTP /healthz"
if [[ -n "${PORT:-}" ]]; then
  if curl -sf --max-time 3 "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
    ok "GET http://localhost:$PORT/healthz returned 200"
  else
    bad "healthz unreachable on port $PORT — check server.log"
  fi
fi

# ── 6. LLM end-to-end (cheap test) ───────────────────────────────────
# Hit a public LLM endpoint to confirm the Gemini key actually works.
# This is the gold standard — if this fails, chat will 502.
print_header "LLM key validity (Gemini)"
gem_key="$(grep -E '^GEMINI_API_KEY=' "$RUNE_HOME/.env" 2>/dev/null | tail -1 | sed 's/^[^=]*=//')"
if [[ -n "$gem_key" ]]; then
  # The cheapest call: list models. Doesn't burn quota meaningfully.
  http_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    "https://generativelanguage.googleapis.com/v1beta/models?key=${gem_key}" 2>/dev/null)"
  case "$http_code" in
    200) ok "Gemini API accepts the key (200)" ;;
    400) bad "Gemini API rejected key shape (400) — malformed key?" ;;
    401|403) bad "Gemini API rejected auth ($http_code) — key invalid or revoked" ;;
    429) note "Gemini API rate-limited (429) — key works but throttled" ;;
    "")  bad "couldn't reach Gemini API (network / DNS issue)" ;;
    *)   note "Gemini API returned unexpected $http_code" ;;
  esac
else
  bad "no GEMINI_API_KEY in .env, can't test"
fi

# ── 7. Summary ───────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Summary:  $pass passed   $fail failed   $warn warnings"
echo "═══════════════════════════════════════════════════════════════"
if (( fail == 0 )); then
  echo "  ✓ Everything looks good. Open Nexus.app and chat."
  exit 0
else
  echo "  ✗ Something's wrong. Common fixes:"
  echo "    • Marker / .env missing → quit Nexus, rm -rf venv .setup_complete_v1, relaunch"
  echo "    • Keys missing in .env  → bundle is stale; rebuild .dmg"
  echo "    • Key visible in .env but not in server env → start.sh didn't load it; check start.log"
  echo "    • LLM 401/403 → swap GEMINI_API_KEY for a valid one"
  echo ""
  echo "  Logs to send back: ~/Library/Application Support/RuneProtocol/{setup,start,server,daemon}.log"
  exit 1
fi
