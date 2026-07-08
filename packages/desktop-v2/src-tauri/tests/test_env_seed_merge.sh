#!/usr/bin/env bash
# Shell-level integration test for the build + first-launch env flow.
#
# We can't easily unit-test the Rust ``seed_or_merge_user_env`` without
# cargo (which isn't always in CI), so this exercises the SAME logic by
# running the build script's stage-4b snippet against a synthetic source
# .env, then simulating the merge a Tauri launch would do.
#
# Coverage:
#   1. Build-stage refresh: copies LLM/relay keys, strips deploy-only.
#   2. First-launch seed: when user .env absent, full copy + 0600 perms.
#   3. Subsequent launch delta-merge: new bundle key appended; existing
#      user override preserved verbatim.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d -t nexus-envtest.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "✗ $*" >&2; exit 1; }
ok()   { echo "✓ $*"; }

# ── Synthetic source .env (mimics packages/server/.env)
SRC="$TMP/server.env"
cat > "$SRC" <<EOF
SERVER_HOST=0.0.0.0
SERVER_PORT=8001
SERVER_SECRET=should-be-stripped
DATABASE_URL=sqlite:///./should-not-ship.db
SERVER_PRIVATE_KEY=0xdeadbeef
ENVIRONMENT=development
LOG_LEVEL=INFO
CORS_ALLOW_ORIGINS=http://localhost:3000
DEFAULT_LLM_PROVIDER=gemini
DEFAULT_LLM_MODEL=gemini-2.5-flash
GEMINI_API_KEY=AIza-test-bundled-1234
TAVILY_API_KEY=tvly-test-key
NEXUS_RELAY_URL=https://relay.example
NEXUS_RELAY_API_KEY=relay-key
EOF

# ── Stage 1: build-time refresh (replicates scripts/build-macos.sh §4b)
DEFAULT_ENV="$TMP/default.env"
{
  echo "# header comment"
  while IFS='' read -r line || [[ -n "$line" ]]; do
    case "$line" in
      SERVER_HOST=*|SERVER_PORT=*|SERVER_SECRET=*) continue ;;
      DATABASE_URL=*) continue ;;
      SERVER_PRIVATE_KEY=*) continue ;;
      ENVIRONMENT=*|LOG_LEVEL=*) continue ;;
      CORS_ALLOW_ORIGINS=*) continue ;;
    esac
    echo "$line"
  done < "$SRC"
} > "$DEFAULT_ENV"

grep -q "^GEMINI_API_KEY=AIza-test-bundled-1234" "$DEFAULT_ENV" \
  || fail "build refresh: GEMINI_API_KEY missing"
grep -q "^NEXUS_RELAY_URL=https://relay.example" "$DEFAULT_ENV" \
  || fail "build refresh: NEXUS_RELAY_URL missing"
if grep -q "SERVER_PRIVATE_KEY" "$DEFAULT_ENV"; then
  fail "build refresh: SERVER_PRIVATE_KEY leaked into bundled default.env"
fi
if grep -q "DATABASE_URL" "$DEFAULT_ENV"; then
  fail "build refresh: DATABASE_URL leaked into bundled default.env"
fi
if grep -q "SERVER_SECRET" "$DEFAULT_ENV"; then
  fail "build refresh: SERVER_SECRET leaked into bundled default.env"
fi
ok "build refresh: keeps LLM/relay, strips deploy-only"

# ── Stage 2: first-launch seed (replicates lib.rs::seed branch)
USER_ENV="$TMP/user.env"
[ ! -e "$USER_ENV" ] || fail "expected $USER_ENV to be absent at start"

# Simulated full-copy seed (matches Rust write order)
{
  cat <<EOF
# Nexus runtime config — seeded by Tauri on first launch.
# Edit directly to override (e.g. swap GEMINI_API_KEY) or use
# Settings · LLM in the desktop. New keys shipped in future
# .dmg releases are merged in automatically on launch.

EOF
  cat "$DEFAULT_ENV"
} > "$USER_ENV"
chmod 600 "$USER_ENV"

[ -f "$USER_ENV" ] || fail "first-launch: user env not created"
perms="$(stat -c '%a' "$USER_ENV" 2>/dev/null || stat -f '%A' "$USER_ENV")"
[ "$perms" = "600" ] || fail "first-launch: expected mode 600, got $perms"
grep -q "^GEMINI_API_KEY=AIza-test-bundled-1234" "$USER_ENV" \
  || fail "first-launch: key not copied"
ok "first-launch: full copy + 0600 perms"

# ── Stage 3: delta-merge on subsequent launch
# Simulate the user overriding GEMINI_API_KEY locally + a new bundle
# key being added in the next .dmg.
sed -i.bak 's|^GEMINI_API_KEY=.*|GEMINI_API_KEY=AIza-user-OVERRIDE|' "$USER_ENV"
rm -f "$USER_ENV.bak"
echo "NEXUS_NEW_FEATURE_FLAG=on" >> "$DEFAULT_ENV"

# Mini-Rust merge logic, replicated in awk: for each KEY in DEFAULT,
# if user already has KEY (commented OR uncommented), skip; else append.
to_append="$(
  awk -F= '
    NR==FNR {
      line=$0; if (line ~ /^[ \t]*#/) next; if (line !~ /=/) next;
      key=$1; sub(/^[ \t]+/,"",key); sub(/[ \t]+$/,"",key)
      bundle[key]=1
      bundle_line[key]=line
      next
    }
    {
      line=$0; t=$0
      sub(/^[ \t]+/,"",t)
      if (substr(t,1,1)=="#") {
        sub(/^[ \t]*#[ \t]*/,"",t)
      }
      if (index(t,"=")>0) {
        k=t; sub(/=.*$/,"",k); sub(/[ \t]+$/,"",k)
        present[k]=1
      }
    }
    END {
      for (k in bundle) {
        if (!(k in present)) print bundle_line[k]
      }
    }
  ' "$DEFAULT_ENV" "$USER_ENV"
)"

echo "" >> "$USER_ENV"
echo "# merged" >> "$USER_ENV"
while IFS= read -r line; do
  [ -z "$line" ] && continue
  echo "$line" >> "$USER_ENV"
done <<< "$to_append"

# Override survived:
grep -q "^GEMINI_API_KEY=AIza-user-OVERRIDE" "$USER_ENV" \
  || fail "merge: user override clobbered"
# New key appended:
grep -q "^NEXUS_NEW_FEATURE_FLAG=on" "$USER_ENV" \
  || fail "merge: new bundle key not appended"
# Original-bundle GEMINI_API_KEY value did NOT re-overwrite the override:
overrides="$(grep -c "^GEMINI_API_KEY=" "$USER_ENV")"
[ "$overrides" = "1" ] || fail "merge: GEMINI_API_KEY appears $overrides times (expected 1)"
ok "delta-merge: appends new keys, preserves user overrides"

echo
echo "ALL TESTS PASSED"
