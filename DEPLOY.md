# Deploying Nexus to a remote server

Step-by-step guide to deploy the Nexus server to a Linux VPS (Docker + Caddy + automatic HTTPS) and point the desktop client at it.

The end state: agents run on your VPS, persist their state across container rebuilds, can install new skills + MCP servers at chat time without redeploys, and the desktop talks to them over HTTPS.

---

## Prerequisites

- A VPS with a public IP and root or sudo access. **Tested:** DigitalOcean ($6/mo droplet), Hetzner CX11 (€4.5/mo), AWS t3.small. Anything with ≥ 1 GB RAM, ≥ 10 GB disk, Ubuntu 22.04+ works.
- Open ports **80** (ACME challenge) and **443** (HTTPS) on the VPS firewall. The desktop never connects to port 8001 directly — Caddy fronts everything.
- A Gemini API key (free tier from [aistudio.google.com](https://aistudio.google.com/apikey)). Anthropic / OpenAI optional.
- The desktop client built locally (`cd packages/desktop-v2 && pnpm install && pnpm tauri:dev` for hot-reload, or `./scripts/build-macos.sh` to install the bundled `.app`). The legacy Avalonia client at git tag `legacy/avalonia-final` is no longer maintained.

---

## Why nip.io for HTTPS without a domain

Remote deployments should serve auth traffic over HTTPS. If you don't own a domain, the canonical workaround is **nip.io**: a free DNS service that resolves `1-2-3-4.nip.io` to `1.2.3.4`. Let's Encrypt issues real certificates for nip.io subdomains, so Caddy gets you proper HTTPS automatically.

The alternative — self-signed certs — works but the desktop's embedded WebView treats them as untrusted, and recovering from that is a worse user experience than just using nip.io.

If you later get a real domain, change `HOSTNAME` in `.env.production`, re-run `docker compose up -d`, and Caddy provisions a new cert.

---

## One-shot deploy

```bash
# On the VPS:
git clone <your-fork-of-this-repo> /opt/nexus
cd /opt/nexus
./scripts/deploy_setup.sh
```

The script:
1. Verifies docker + docker compose are installed (errors out with install instructions if not)
2. Auto-detects the VPS public IP and writes `HOSTNAME=<ip-with-dashes>.nip.io` to `.env.production`
3. Generates a 32-byte hex `SERVER_SECRET` for JWT signing
4. Prompts for `GEMINI_API_KEY`
5. Runs `docker compose build && docker compose up -d`
6. Prints the public HTTPS URL you'll plug into the desktop

First run takes ~5 minutes (image build + Caddy ACME negotiation). Subsequent restarts are seconds.

---

## Manual step-by-step (if you don't want the script)

### 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
newgrp docker   # or log out + back in
```

### 2. Clone + configure

```bash
git clone <your-fork> /opt/nexus
cd /opt/nexus
cp .env.production.example .env.production
nano .env.production
```

Change at minimum:
- `HOSTNAME` → `<your-ip-with-dashes>.nip.io` (e.g. `203-0-113-7.nip.io`)
- `CORS_ALLOW_ORIGINS` → `https://<hostname>`
- `SERVER_SECRET` → output of `openssl rand -hex 32`
- `GEMINI_API_KEY` → your key

### 3. Build + run

```bash
docker compose build
docker compose up -d
docker compose logs -f
```

Watch the Caddy logs for `certificate obtained successfully` — if you see `acme: error` your DNS isn't pointing at this box (port 80 must be open and reachable).

### 4. Verify

```bash
curl -fsSL https://<your-hostname>/healthz
# → HTTP 200 with a JSON HealthCheckResponse body (see main.py).
```

---

## Pointing the desktop at the remote server

Desktop v2 reads the server base URL from the `VITE_NEXUS_API`
build-time env. Two ways to point it at a remote VPS:

**Option A — rebuild with the remote baked in:**
```bash
cd packages/desktop-v2
VITE_NEXUS_API=https://1-2-3-4.nip.io pnpm tauri:build
```
The resulting `.app` (or `.dmg`) talks to that URL exclusively. Use this
when distributing to other users.

**Option B — runtime override via the `.env` file Tauri reads at boot:**
On macOS the bundled `.app` reads `~/Library/Application Support/RuneProtocol/.env`
on startup. Add:
```
VITE_NEXUS_API=https://1-2-3-4.nip.io
```
then restart the app. The same file is where `GEMINI_API_KEY` and the
other LLM keys live (Tauri injects them into the sidecar's environment
on spawn — see `packages/desktop-v2/src-tauri/src/lib.rs`).

The first user to register on the remote becomes their own agent.

---

## Layout & persistence

```
/opt/nexus/                   # checked-out repo
├── Dockerfile
├── docker-compose.yml
├── Caddyfile
├── .env.production           # (you create this — never commit)
└── …

Docker volumes (host paths shown by `docker volume inspect`):
  nexus-data        ← /data inside the container
    ├── db/                   # rune_server.db (SQLite)
    ├── twins/<user_id>/      # per-user EventLog, persona, skills, etc
    ├── uploads/<user_id>/    # uploaded files
    └── cache/                # chain identity cache, ABI cache
  caddy-data        ← Let's Encrypt account + cert storage
  caddy-config      ← Caddy config autosaves
```

**Backup the volume periodically** — that's the agent's whole memory. Easiest:

```bash
docker run --rm -v nexus-data:/d -v "$PWD":/b alpine \
  tar czf /b/nexus-backup-$(date +%F).tgz -C /d .
```

Restore:
```bash
docker run --rm -v nexus-data:/d -v "$PWD":/b alpine \
  tar xzf /b/nexus-backup-2026-05-02.tgz -C /d
```

---

## Agent installs new tools at runtime — how it survives Docker

The agent uses two tools to install capabilities at chat time **without code changes**:

- `manage_skill(action='install', identifier='anthropic:pdf')` — installs an Anthropic-style skill (clones a repo, drops `SKILL.md` into `/data/twins/<user>/skills/<name>/`)
- `manage_mcp(action='install', identifier='lobehub:slack-mcp')` — installs an MCP server (resolves to `npx -y <package>`, registers it as a function-callable tool)

Both write under `/data` which is the persistent volume — so installs survive container rebuilds. Both shell out to `npx` (Node 20 is baked into the runtime image), so the agent doesn't need outbound `apt-get install` to get tools.

This means: deploy once, then let the agent grow itself. No `docker compose up --build` needed when it learns a new skill.

---

## Common ops

```bash
# Tail server logs
docker compose logs -f nexus-server

# Tail Caddy logs (HTTPS / cert renewal stuff)
docker compose logs -f caddy

# Update to a new code version
git pull
docker compose up -d --build

# Rotate the JWT secret (forces all users to re-login)
sed -i "s|^SERVER_SECRET=.*|SERVER_SECRET=$(openssl rand -hex 32)|" .env.production
docker compose restart nexus-server

# Wipe everything (DESTRUCTIVE — deletes all agent state)
docker compose down -v

# Open a shell inside the running container (debug only)
docker compose exec nexus-server bash
```

---

## Security notes

- The container runs as a **non-root user** (`nexus`, UID 1000). Nothing in the image needs root post-build.
- `SERVER_SECRET` is the JWT signing key — treat it like a password. Don't commit `.env.production`.
- Caddy uses Let's Encrypt's prod ACME endpoint. If you're testing repeatedly, switch to staging in the Caddyfile to avoid rate limits.
- The `/llm/chat` endpoint is per-user rate-limited via `RATE_LIMIT_LLM_REQUESTS_PER_MINUTE`. Tune for your traffic.
- **CORS** is locked to your hostname; the desktop client itself is non-browser HTTP so it doesn't matter for desktop, but any browser-based access does need it.
- BSC chain integration is **opt-in** — set `SERVER_PRIVATE_KEY` + `CHAIN_RPC_URL` to enable. Without those the server runs in local mode (no on-chain anchoring).

---

## Troubleshooting

**Caddy keeps spamming `acme: error: 403`**  
Port 80 isn't reachable from the public internet. Check VPS firewall + cloud provider ingress rules.

**Browser/desktop says "Your connection is not private"**  
Likely you set `HOSTNAME` to something nip.io can't resolve (typo) or your VPS IP changed and the cert is for the old one. `docker compose down && rm -rf <caddy-data-volume>/* && docker compose up -d` to force re-issuance.

**Agent says "npx not found" when installing an MCP server**  
You're on an old Docker image. `docker compose build --no-cache` to rebuild from scratch — Node 20 is in the runtime stage.

**SQLite saying `database is locked`**  
You exceeded SQLite's write throughput (rare with < 50 concurrent users). Migrate to Postgres: change `DATABASE_URL` to `postgresql://…`, add a Postgres service to `docker-compose.yml`. The schema is portable.

---

## What's NOT covered yet

- **Multi-host scaling.** Single-VPS only — the SQLite DB and the per-user file storage are local. For multi-instance you'd need Postgres + S3-compatible blob storage (the S3 mirror is a separate planned task).
- **Automated backups.** The volume backup command above is manual. Wire it into a cron + offsite copy if the agent state matters.
- **Log shipping.** Container logs go to docker's default driver. For production add `loki` / `vector` / `papertrail` / etc.

---

Questions or stuck? Open a server shell and start reading `docker compose logs nexus-server` — most issues are visible in the first few hundred lines.
