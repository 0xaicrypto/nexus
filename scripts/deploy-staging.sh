#!/usr/bin/env bash
# Deploy to staging (port 8002)
set -e
cd ~/heurion
# Ensure VPS matches origin/main exactly; any local changes are usually
# leftover from a previous failed deploy and should not block updates.
git fetch origin main 2>/dev/null || { sleep 3; git fetch origin main; }
git reset --hard origin/main
echo "Deploying: $(git log -1 --oneline)"
cd packages/server-ts

# Always overwrite .env so staging uses the correct port/config.
cp -f .env.staging .env 2>/dev/null || cat > .env << ENVEOF
DATABASE_URL="file:./staging.db"
SERVER_HOST=0.0.0.0
SERVER_PORT=8002
SERVER_SECRET=staging-secret
DEEPSEEK_API_KEY=${DEEPSEEK_KEY}
GEMINI_API_KEY=${GEMINI_KEY}
CORS_ALLOW_ORIGINS=*
TWIN_BASE_DIR=.nexus/staging-twins
ENVEOF

# Force fresh Prisma Client install/generation
rm -rf node_modules
pnpm store prune --force 2>/dev/null || true
pnpm install
npx prisma generate
# Force-recreate staging database to avoid FK constraint issues from old data
rm -f staging.db staging.db-journal 2>/dev/null || true
npx prisma db push --accept-data-loss

pm2 delete heurion-staging 2>/dev/null || true
SERVER_PORT=8002 pm2 start npx --name heurion-staging -- tsx src/main.ts
pm2 save

# Ensure HZ user exists in fresh staging DB
sleep 3
npx tsx scripts/set-admin.ts 2>/dev/null || true

# Pre-install Playwright browser one-time for E2E tests
npx playwright install chromium 2>/dev/null || true

# Build web frontend for staging UI (Nginx serves dist/ for staging.heurion.org)
cd ~/heurion/packages/web
pnpm install --frozen-lockfile 2>/dev/null || pnpm install
pnpm build
chmod -R +rx dist
chmod +rx /root /root/heurion /root/heurion/packages /root/heurion/packages/web 2>/dev/null || true
cd ~/heurion/packages/server-ts

echo "Staging ready on port 8002 (web via Nginx)"

# Robust health check: retry instead of a single attempt.
HEALTH_URL="http://localhost:8002/healthz"
MAX_RETRIES=15
RETRY_DELAY=2

for i in $(seq 1 $MAX_RETRIES); do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    echo " STAGING OK"
    exit 0
  fi
  echo "  health check attempt $i/$MAX_RETRIES failed, retrying in ${RETRY_DELAY}s..."
  sleep $RETRY_DELAY
done

echo ""
echo "❌ STAGING health check failed after ${MAX_RETRIES} attempts."
echo "--- PM2 logs for heurion-staging ---"
pm2 logs heurion-staging --lines 100 --nostream || true
echo "--- Process status ---"
pm2 describe heurion-staging || true
exit 1
