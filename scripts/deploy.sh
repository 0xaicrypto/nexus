#!/usr/bin/env bash
# Deploy script — run on VPS via GitHub Actions SSH
set -e

cd ~/heurion || { git clone https://github.com/0xaicrypto/heurion.git ~/heurion && cd ~/heurion; }
git pull origin main

cd packages/server-ts

[ -f .env ] || cat > .env << ENVEOF
DATABASE_URL="file:./nexus_server.db"
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

sleep 3
curl -fsS http://localhost:8001/healthz && echo "OK" || echo "FAIL"
