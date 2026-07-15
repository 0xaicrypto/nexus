/**
 * BootGate — block the desktop UI until the backend sidecar is healthy.
 *
 * Why this exists
 * ───────────────
 * Before this gate, the app rendered LoginView the instant Tauri
 * finished webview init — typically 200 ms after spawn. But the bundled
 * FastAPI sidecar takes 3–15 s to come up: Python interpreter cold
 * start (~1 s) → import of FastAPI / SQLAlchemy / Alembic / etc.
 * (~2 s) → Alembic ``upgrade head`` (1–10 s depending on # of pending
 * migrations and DB size) → uvicorn ``bind 127.0.0.1:8001`` (~50 ms).
 *
 * During those 3–15 s the user could already be filling in the login
 * form. Clicking Sign in fired a ``fetch /auth/register`` against a
 * closed port → ``TypeError: Failed to fetch`` → red "Cannot reach
 * server" error → the medic thought Nexus had crashed.
 *
 * Boot policy
 * ───────────
 *  - Poll ``GET /healthz`` every 500 ms with a 2 s per-request timeout.
 *  - While probing AND under the soft deadline (15 s): render a centred
 *    "Starting backend…" splash. After 3 s, also reveal the live
 *    sidecar diag tail so the medic sees progress (alembic upgrade
 *    lines etc.) — without this the splash feels dead.
 *  - Once /healthz returns 200: switch to ``children`` (the normal
 *    Login → MainShell tree). No flicker — the splash and the
 *    LoginView share the same dark background.
 *  - If the sidecar exits (``diag.alive === false && diag.pid != null``)
 *    OR we pass the 15 s soft deadline without /healthz: render
 *    children anyway. LoginView's own diag panel takes over from there
 *    and lets the user try "Continue without server" or restart the
 *    sidecar — i.e. we don't strand the user behind the gate forever.
 *
 * Outside Tauri (``pnpm dev`` in a browser tab), there's no sidecar to
 * gate on. We detect this by the IPC returning null on the first call
 * and immediately render children — the dev-mode escape hatch in
 * LoginView ("Continue without server") still works.
 */

import { useEffect, useState, type ReactNode } from 'react';
import { api, setApiBaseUrl, CLIENT_API_VERSION, type SidecarDiagnostics } from './lib/api-client';
import { useAppState } from './store';
import { SidecarDiagPanel, summariseDiag } from './components/sidecar-diag-panel';
import { ServerSetupModal } from './components/server-setup-modal';

/** Soft deadline after which we let the UI through even if /healthz
 *  is still failing — bias toward not stranding the user. The diag
 *  panel below makes the failure mode obvious. */
const SOFT_DEADLINE_MS = 15_000;

/** How often we poll /healthz. 500 ms means the median user
 *  experiences ~250 ms of splash post-readiness — barely perceptible. */
const POLL_INTERVAL_MS = 500;

/** When to start showing the live diag tail inside the splash. Hiding
 *  it for the first few seconds keeps the boot screen calm in the
 *  happy path; revealing it after 3 s gives the medic a sign of life
 *  when migrations are slow. */
const SHOW_DIAG_AFTER_MS = 3_000;

type Phase = 'probing' | 'ready' | 'soft_timeout';


export function BootGate({ children }: { children: ReactNode }) {
  const [phase, setPhase]       = useState<Phase>('probing');
  const [elapsed, setElapsed]   = useState(0);                       // ms since mount
  const [diag, setDiag]         = useState<SidecarDiagnostics | null>(null);
  // True once we've successfully called the diag IPC at least once —
  // distinguishes "Tauri present, sidecar still booting" (we wait) from
  // "no Tauri (pnpm dev)" (let through immediately).
  const [tauriDetected, setTauriDetected] = useState(false);
  // Phase-0: server mode init (async, runs before healthz polling).
  const [serverInitDone, setServerInitDone] = useState(false);

  const needsServerSetup = useAppState((s) => s.needsServerSetup);
  const { setServerMode, setNeedsServerSetup, setServerApiCompatible } = useAppState.getState();

  // Phase 0 — read server mode from Tauri IPC and set API base URL.
  // This must complete before healthz polling starts so the poll hits
  // the right host. If we're not in Tauri (pnpm dev / browser), the
  // IPC returns null and we proceed immediately with localhost:8001.
  useEffect(() => {
    api.getServerMode().then((result) => {
      if (result) {
        if (result.mode === 'remote') {
          if (result.base_url) {
            setApiBaseUrl(result.base_url);
            setServerMode('remote', result.base_url);
          } else {
            setServerMode('remote', undefined);
            setNeedsServerSetup(true);
          }
        } else {
          setServerMode('local');
        }
      }
      setServerInitDone(true);
    }).catch(() => {
      setServerInitDone(true); // not in Tauri — proceed normally
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Phase 1 — healthz polling. Waits for phase-0 to finish so the
  // base URL is set before the first probe.
  useEffect(() => {
    if (!serverInitDone) return;
    // If the user hasn't configured a remote URL yet, skip polling —
    // show the setup modal instead.
    if (needsServerSetup) return;

    let cancelled = false;
    const start = Date.now();

    const tick = async () => {
      if (cancelled) return;
      const now = Date.now() - start;
      setElapsed(now);

      // 1) Probe /healthz. On first success also check API version.
      const healthz = await api.fetchHealthz();
      if (cancelled) return;
      if (healthz) {
        // Version compatibility check — warn but don't block.
        const serverApiVer = healthz.api_version ?? 1;
        const minClientVer = healthz.min_client_api_version ?? 1;
        const compatible = CLIENT_API_VERSION >= minClientVer;
        setServerApiCompatible(compatible, serverApiVer);
        setPhase('ready');
        return;
      }

      // 2) Pull diag for the splash. Detect Tauri-vs-browser based on
      //    whether the IPC ever returns non-null.
      const d = await api.getSidecarDiagnostics();
      if (cancelled) return;
      if (d) {
        setDiag(d);
        setTauriDetected(true);
      } else if (now > 1_500) {
        // No Tauri IPC available after 1.5 s and /healthz also down →
        // we're in browser-only dev OR Tauri's IPC is broken. Either
        // way, stop blocking — let LoginView handle it (the dev-mode
        // "Continue without server" link is in there).
        setPhase('soft_timeout');
        return;
      }

      // 3) Hard exits — sidecar declared dead by Tauri, OR soft
      //    deadline passed. Both fall through to children with diag.
      if (d && d.pid != null && !d.alive) {
        setPhase('soft_timeout');
        return;
      }
      if (now > SOFT_DEADLINE_MS) {
        setPhase('soft_timeout');
        return;
      }

      setTimeout(tick, POLL_INTERVAL_MS);
    };
    tick();
    return () => { cancelled = true; };
  }, [serverInitDone, needsServerSetup]); // eslint-disable-line react-hooks/exhaustive-deps

  // Remote-server setup required — user hasn't configured a URL yet.
  if (needsServerSetup) {
    return <ServerSetupModal />;
  }

  if (phase === 'ready' || phase === 'soft_timeout') {
    // Let the rest of the app take over. LoginView's own diag panel
    // surfaces sidecar status from here on, so we don't double-render.
    return <>{children}</>;
  }

  // Splash. Branding stays minimal so the eventual transition to
  // LoginView is one-frame; the only thing that changes is the centre
  // card content.
  return (
    <div className="flex min-h-screen items-center justify-center bg-bg">
      <div className="w-full max-w-md px-6 py-12">
        <div className="mb-10 text-center">
          <h1 className="font-display text-display text-text-primary">Nexus</h1>
          <p className="mt-2 text-body text-text-secondary">
            Clinical workflow agent
          </p>
        </div>

        <div className="rounded-sm border border-border bg-surface-1 px-4 py-6 text-center">
          <div className="flex items-center justify-center gap-3">
            <Spinner />
            <span className="text-body text-text-secondary">
              {serverInitDone ? 'Starting backend…' : 'Initialising…'}
            </span>
          </div>
          <p className="mt-2 text-caption text-text-tertiary">
            {serverInitDone && (
              <>
                {Math.floor(elapsed / 1000)}s elapsed
                {tauriDetected && diag && (
                  <>{' · '}{summariseDiag(diag)}</>
                )}
              </>
            )}
          </p>
          {/* DB migrations + lifespan startup can take 5–10s on first
              launch. Showing the diag tail after 3s avoids "is it
              frozen?" panic without cluttering the happy path. */}
          {elapsed > SHOW_DIAG_AFTER_MS && diag && (
            <div className="mt-4 text-left">
              <SidecarDiagPanel diag={diag} />
            </div>
          )}
        </div>

        <p className="mt-6 text-center text-caption text-text-tertiary">
          The first launch can take longer while database migrations run.
          {elapsed > 8_000 && ' If this hangs past 15s, the login screen will appear with diagnostics.'}
        </p>
      </div>
    </div>
  );
}

function Spinner() {
  return (
    <svg
      className="h-4 w-4 animate-spin text-text-secondary"
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <circle
        cx="12"
        cy="12"
        r="9"
        stroke="currentColor"
        strokeOpacity="0.25"
        strokeWidth="3"
      />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  );
}
