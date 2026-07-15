# ADR-003: Web UI SaaS pivot — replacing the Tauri desktop client with a browser-first UI

**Status:** Proposed  
**Date:** 2026-07-15  
**Deciders:** JZ (architect), product owner  

## Context

Nexus currently ships a single user-facing client: `packages/desktop-v2`, a Tauri 2.0 + React + TypeScript application. It spawns the FastAPI server as a local sidecar and talks to `127.0.0.1:8001`. The backend, however, has already been deployed to a public DigitalOcean droplet (`https://188-166-214-81.nip.io`) and the default LLM provider has been moved to DeepSeek in the cloud.

This creates a tension:

- The desktop client is the only UI we have.
- The product strategy is moving toward cloud SaaS.
- Maintaining the Tauri/Rust sidecar layer (macOS signing, Windows MSI, Linux AppImage, auto-updater, sidecar lifecycle) consumes disproportionate effort for a solo/small team.
- A browser-based UI lets users access their agents from any device without installation, and aligns with the hosted backend.

We therefore need a plan to migrate from the desktop-only model to a **browser-first SaaS UI**, while not breaking existing desktop users overnight.

## Decision

1. **Create a new `packages/web` package** as the canonical SaaS UI.
2. **Keep `packages/desktop-v2` frozen** — no new features, only critical bug fixes, until `packages/web` reaches parity and we formally deprecate it.
3. **Serve the web UI from the FastAPI backend** in production (static files under `/`), so a single Docker image + domain hosts both API and UI.
4. **Reuse proven parts from `desktop-v2`** — API client logic, type definitions, state shape, and design tokens — rather than rebuilding from scratch.

## Goals

- Users can open a browser, log in, and chat with their twin without installing anything.
- The web UI shares the same visual language and interaction model as the desktop client to minimize relearning.
- Self-hosted users can still deploy the same Docker image and get a working web UI.
- The backend remains headless: no UI code leaks into `nexus_server` business logic.

## Non-goals

- Deleting `packages/desktop-v2` immediately.
- Re-implementing every desktop-only feature (e.g., local file-system deep integration, system tray) in the first web iteration.
- Supporting offline mode in the browser (PWA/service-worker caching may come later).

## Proposed architecture

### Package layout

```
packages/
  web/                  # NEW — browser-first React UI
    public/
    src/
      api/              # axios/fetch wrapper + generated types
      components/       # shared UI primitives
      routes/           # page-level components
        login.tsx
        chat.tsx
        settings.tsx
      stores/           # global state (auth, chat, twin)
      lib/
        api-client.ts   # migrated from desktop-v2
        types.ts        # migrated from desktop-v2
    index.html
    package.json
    vite.config.ts
    tailwind.config.ts
  desktop-v2/           # FROZEN — keep as-is
  server/               # FastAPI backend
  sdk/
  nexus/
```

### Technology stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Framework | React 18 + Vite | Same as desktop-v2; fast dev loop; easy static build. |
| Styling | Tailwind CSS + existing design tokens | Reuse `desktop-v2` tokens; consistent look. |
| Routing | React Router v6 | Simple, well-known, supports code-splitting. |
| State | Zustand | Already used in `desktop-v2/src/store.ts`; minimal boilerplate. |
| API client | Fetch/axios with JWT interceptor | Existing JWT auth on backend; reuse interceptor logic. |
| Build output | `dist/` | Vite produces a static bundle that FastAPI can serve. |

### Authentication

The backend already exposes JWT-based auth (`/api/v1/auth/register|login`). The web UI will:

1. Collect username/password on a `/login` route.
2. Store the returned `jwt_token` in `localStorage` (or `httpOnly` cookie later for XSS resilience).
3. Attach `Authorization: Bearer <token>` to every API call.
4. Redirect to `/login` on 401.

Future: add OAuth/GitHub/Google login if product requires it; out of scope for the first iteration.

### API integration

`packages/web/src/lib/api-client.ts` should be derived from `packages/desktop-v2/src/lib/api-client.ts`, with the following changes:

- Base URL becomes relative (`/api/v1`) so the same build works against any backend host.
- Remove Tauri-specific commands and `tauri://` origin handling.
- Keep endpoint methods, error handling, and retry logic.

The backend CORS config should be updated to allow the web production origin (`https://188-166-214-81.nip.io`) and standard local dev origins (`http://localhost:5173`). The Tauri origins (`tauri://localhost`, `asset://localhost`) can remain for backward compatibility but are no longer strategically important.

### Static hosting from FastAPI

In production the same Docker container runs the FastAPI app and serves the web build:

```python
from fastapi.staticfiles import StaticFiles

app.mount("/assets", StaticFiles(directory="/app/packages/web/dist/assets"), name="assets")
app.mount("/", StaticFiles(directory="/app/packages/web/dist", html=True), name="web")
```

- API routes remain at `/api/v1/*` and `/auth/*`.
- SPA fallback: any unknown path returns `index.html` so React Router handles deep links.
- During local development, Vite dev server proxies `/api` to the running backend.

### Docker / deployment changes

1. Multi-stage Dockerfile:
   - Stage 1: `node:22` image installs `packages/web` dependencies and runs `pnpm build`.
   - Stage 2: existing Python runtime copies the backend wheel and the built `dist/` folder.
2. `docker-compose.yml` stays unchanged; Caddy continues to terminate TLS and reverse-proxy to `nexus-server`.
3. No separate CDN required initially; the backend serves the static bundle.

### Migration strategy from desktop-v2

Rather than a big-bang rewrite, migrate feature-by-feature:

| Phase | Scope | Desktop-v2 status |
|-------|-------|-------------------|
| M0 | Login + JWT storage + basic chat | Frozen |
| M1 | Chat history, sessions, file attachments | Frozen |
| M2 | Settings · LLM, billing, user profile | Frozen |
| M3 | Modes: Imaging, Writing, Memory browser | Frozen |
| M4 | DICOM viewer, report export, advanced tools | Frozen |
| M5 | Announce deprecation, stop publishing desktop builds | Archive or remove |

For each phase, copy/adapt the relevant TypeScript code from `desktop-v2/src/` into `packages/web/src/`. Do **not** import across packages; this keeps the web package self-contained and makes future deletion of `desktop-v2` clean.

## Risks and mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Desktop users feel abandoned | Medium | Keep desktop-v2 installable; announce deprecation only after web parity. |
| UI rewrite takes too long | High | Ship incrementally; M0 chat alone unlocks SaaS usage. |
| Browser file handling is weaker than Tauri | Medium | Use backend `/api/v1/files/upload` for all files; DICOM viewer uses Cornerstone/OHIF in browser. |
| JWT in localStorage is XSS-vulnerable | Medium | Accept for M0; migrate to `httpOnly` cookies or short-lived tokens later. |
| CORS mismatch between dev and prod | Low | Use relative API paths; backend allows explicit origins. |

## Open questions

1. Should the web UI support self-hosted deployment out of the box, or is it SaaS-only at first?
2. Do we want a marketing landing page at `/` and the app at `/app`, or is the app at `/` behind login?
3. Should we keep the React code in `desktop-v2` as the source of truth and publish it as both Tauri and web, or fork it into `packages/web`?
4. What is the target browser baseline? (Recommendation: evergreen Chrome/Safari/Firefox; no IE11.)

## Consequences

- **Positive:** Faster iteration, no desktop packaging, accessible from any device, aligns with cloud backend.
- **Positive:** Single deployment artifact (Docker image) serves API + UI.
- **Negative:** Short-term maintenance of two similar React codebases until parity is reached.
- **Negative:** Loss of some native desktop integrations until rebuilt with browser APIs.
