#!/usr/bin/env bash
# Deploy script — run on VPS via GitHub Actions SSH
set -e

cd ~/heurion || { git clone https://github.com/0xaicrypto/heurion.git ~/heurion && cd ~/heurion; }
# Ensure VPS matches origin/main exactly; any local changes are usually
# leftover from a previous failed deploy and should not block updates.
git fetch origin main
git reset --hard origin/main

cd packages/server-ts

[ -f .env ] || cat > .env << ENVEOF
DATABASE_URL="file:./nexus_server.db"
SERVER_HOST=0.0.0.0
SERVER_PORT=8001
SERVER_SECRET=$(openssl rand -hex 32)
DEEPSEEK_API_KEY=${DEEPSEEK_KEY:-sk-edc3839a3dd44babaf33dc16d0761dc3}
CORS_ALLOW_ORIGINS=*
ENVEOF

which pnpm || npm install -g pnpm@10
pnpm install --frozen-lockfile
npx prisma generate
npx prisma db push --accept-data-loss
which pm2 || npm install -g pm2
pm2 restart heurion 2>/dev/null || pm2 start npx --name heurion -- tsx src/main.ts
pm2 save

# Robust health check: retry instead of a single attempt.
HEALTH_URL="http://localhost:8001/healthz"
MAX_RETRIES=15
RETRY_DELAY=2

for i in $(seq 1 $MAX_RETRIES); do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    echo "OK"
    break
  fi
  echo "  health check attempt $i/$MAX_RETRIES failed, retrying in ${RETRY_DELAY}s..."
  sleep $RETRY_DELAY
done

if ! curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
  echo ""
  echo "❌ Production health check failed after ${MAX_RETRIES} attempts."
  echo "--- PM2 logs for heurion ---"
  pm2 logs heurion --lines 100 --nostream || true
  echo "--- Process status ---"
  pm2 describe heurion || true
  exit 1
fi

# Build web frontend + serve via nginx
cd ~/heurion/packages/web
pnpm install --frozen-lockfile 2>/dev/null || pnpm install
pnpm build
chmod -R +rx dist
chmod +rx /root /root/heurion /root/heurion/packages /root/heurion/packages/web 2>/dev/null || true

# Ensure nginx is running
which nginx || { apt-get update -qq && apt-get install -y -qq nginx; }
systemctl reload nginx 2>/dev/null || nginx
