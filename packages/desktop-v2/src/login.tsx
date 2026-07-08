/**
 * LoginView — centred single-form, Claude Desktop aesthetic.
 *
 * M0 auth model (carried over from the legacy Avalonia LoginViewModel,
 * see git tag legacy/avalonia-final):
 *   - Single field: display name
 *   - Sign-in = POST /api/v1/auth/register {display_name} → {jwt_token}
 *   - No password (passkey + persistent user_id ships U2+)
 *
 * U3.4: when the sidecar fails to start, we used to leave the user
 * staring at a useless "Cannot reach server" red box with zero way to
 * see WHY. This file now polls the Tauri ``get_sidecar_diagnostics``
 * IPC every 2 s and renders the last ~60 lines of raw sidecar output
 * inline. The panel auto-expands on the first signin error.
 *
 * The "Continue without server" escape hatch stays — if the sidecar
 * fails to start we still want the dev to be able to poke around the UI.
 */

import { useEffect, useState, type FormEvent } from 'react';
import { Button, Input } from './components/ui';
import { useAppState } from './store';
import { api, ApiError, type SidecarDiagnostics } from './lib/api-client';
import { BUILD_ID } from './lib/build-info';
import { SidecarDiagPanel, summariseDiag } from './components/sidecar-diag-panel';
import { useT } from './lib/i18n';

export function LoginView() {
  const t                  = useT();
  const setToken           = useAppState((s) => s.setToken);
  const setStoreDisplayName= useAppState((s) => s.setDisplayName);
  const setActiveUserId    = useAppState((s) => s.setActiveUserId);
  const setIdentities      = useAppState((s) => s.setIdentities);
  const resetForIdentitySwitch = useAppState((s) => s.resetForIdentitySwitch);
  const activeUserId       = useAppState((s) => s.activeUserId);
  const storedName         = useAppState((s) => s.displayName);
  const showToast          = useAppState((s) => s.showToast);

  // Pre-fill from store so returning users see their name when they
  // re-launch (the cached user_id still works behind the scenes).
  const [displayName, setDisplayName] = useState(storedName ?? '');
  const [busy, setBusy]               = useState(false);
  const [error, setError]             = useState<string | null>(null);
  const [allowMock, setAllowMock]     = useState(false);
  // Separate busy flag for the passkey flow — the name-only Sign in
  // button doesn't share lockout (medic could legitimately abandon
  // a stalled passkey ceremony + try the name flow).
  const [passkeyBusy, setPasskeyBusy] = useState<'login' | 'signup' | null>(null);

  // Sidecar diagnostics polling. Cheap — the IPC reads from an in-memory
  // ring buffer + serialises ~60 small strings. Auto-expands the first
  // time signin errors out.
  const [diag, setDiag]         = useState<SidecarDiagnostics | null>(null);
  const [showDiag, setShowDiag] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const d = await api.getSidecarDiagnostics();
        if (!cancelled) setDiag(d);
      } catch {
        // tauriInvoke returned null / not in Tauri — silently no-op.
      }
    };
    tick();
    const id = setInterval(tick, 2000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    if (error) setShowDiag(true);
  }, [error]);

  // Auto-expand the diag panel the moment the sidecar dies, even before
  // the user tries to sign in. Without this, a sidecar that crashes
  // at startup is invisible until the first click — which is precisely
  // the moment we want the user to already see the error so they don't
  // waste a "what's wrong?" round-trip with us.
  useEffect(() => {
    if (diag && diag.pid != null && !diag.alive) setShowDiag(true);
  }, [diag?.alive, diag?.pid]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const name = displayName.trim();
    if (!name) {
      setError('Please enter your name.');
      return;
    }
    setBusy(true);
    try {
      // F-multiuser-isolation — display_name is the login key now.
      // Backend matches name (trimmed + casefolded) against users:
      //   * hit  → activate that identity (returns its existing data)
      //   * miss → create new identity (empty workspace)
      // If the resolved user_id is DIFFERENT from whatever was active,
      // we wipe the in-memory workspace so the new identity doesn't
      // briefly see the prior identity's patients / chat state.
      const r = await api.login(name, '');
      if (activeUserId && activeUserId !== r.user_id) {
        // Identity changed — clear all per-user zustand slices BEFORE
        // setting the new token so downstream selectors don't fire
        // against stale state.
        resetForIdentitySwitch();
      }
      setToken(r.access_token);
      setActiveUserId(r.user_id);
      setIdentities(r.identities);
      setStoreDisplayName(name);  // persist for avatar pill + pre-fill on re-launch
      if (r.isNewAccount) {
        showToast(`已为「${name}」新建独立账号`, 'success');
      } else {
        showToast(`已切换到「${name}」`, 'success');
      }
    } catch (err) {
      if (err instanceof ApiError) {
        setError(
          err.status === 400
            ? 'Registration failed. Please try a different name.'
            : `Server error (${err.status}). Is the backend running?`,
        );
        setAllowMock(true);
      } else if (err instanceof TypeError) {
        // Network / fetch failure — typical when backend isn't running
        setError('Cannot reach server. Is the backend running on port 8001?');
        setAllowMock(true);
      } else {
        setError(String(err));
        setAllowMock(true);
      }
    } finally {
      setBusy(false);
    }
  }

  async function tryRestartSidecar() {
    try {
      setError(null);
      const ok = await api.restartSidecar();
      showToast(
        ok ? 'Sidecar restart issued' : 'Restart not available in this build',
        ok ? 'success' : 'info',
      );
    } catch (e) {
      showToast(`Restart failed: ${String(e)}`, 'error');
    }
  }

  function continueWithoutServer() {
    setToken('dev-mock-token');
    showToast('Continuing in offline / mock mode', 'info');
  }

  /** Passkey login or signup. Opens a Tauri WebviewWindow at the
   *  sidecar's /auth/passkey-page, runs the WebAuthn ceremony there,
   *  and uses the bounce+poll bridge to deliver the JWT back to React.
   *  See ``api.passkeyAuth`` docstring for the full architecture. */
  async function onPasskey(mode: 'login' | 'signup') {
    setError(null);
    setPasskeyBusy(mode);
    try {
      const r = await api.passkeyAuth(mode, displayName);
      setToken(r.token);
      // Persist the typed name for next launch's prefill if signup
      // produced it; on login the name from this form may not match
      // what the server has for the registered passkey, so we only
      // overwrite if the medic typed one.
      if (displayName.trim()) {
        setStoreDisplayName(displayName.trim());
      }
      showToast(t('login.signIn'), 'success');
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // 5-min timeout phrasing is friendlier than the raw error.
      if (msg.includes('timed out')) {
        setError(t('login.passkey.cancelled'));
      } else {
        setError(t('login.passkey.error', { error: msg }));
      }
      // Surface diagnostics in case sidecar / page is broken.
      setAllowMock(true);
    } finally {
      setPasskeyBusy(null);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg">
      <div className="w-full max-w-md px-6 py-12">
        <div className="mb-10 text-center">
          <h1 className="font-display text-display text-text-primary">Nexus</h1>
          <p className="mt-2 text-body text-text-secondary">
            {t('login.title')}
          </p>
        </div>

        <form onSubmit={onSubmit} className="space-y-4 selectable">
          <div>
            <label
              htmlFor="displayName"
              className="mb-1.5 block text-caption font-medium text-text-secondary"
            >
              {t('newPatient.initials')}
            </label>
            <Input
              id="displayName"
              type="text"
              autoComplete="name"
              required
              autoFocus
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder={t('login.namePlaceholder')}
              disabled={busy}
            />
            <p className="mt-1.5 text-caption text-text-tertiary">
              {t('login.help')}
            </p>
            {/* F-multiuser-isolation — make the new contract obvious:
                same name = re-enter your account; different name =
                brand-new independent space. Without this hint medics
                expected "input any name = same data" (the old broken
                behaviour). */}
            <p className="mt-1 text-[11px] text-text-tertiary leading-relaxed">
              💡 输入用过的名字 = 进入原来的工作空间;输入新名字 = 自动建立独立的新账号。
            </p>
          </div>

          {error && (
            <div className="rounded-sm border border-retract/40 bg-retract/10 px-3 py-2 text-caption text-retract">
              {error}
            </div>
          )}

          <Button
            type="submit"
            variant="primary"
            disabled={busy || passkeyBusy !== null}
            className="w-full"
          >
            {busy ? t('login.signingIn') : t('login.signIn')}
          </Button>

          {/* Passkey buttons — split into "sign in with existing" and
              "sign up new" because WebAuthn's register and
              authenticate ceremonies are distinct (one needs a name;
              the other matches against existing credentials). */}
          <div className="flex items-center gap-2 pt-2 text-caption text-text-tertiary">
            <div className="h-px flex-1 bg-border" />
            <span>{t('login.passkey.divider')}</span>
            <div className="h-px flex-1 bg-border" />
          </div>
          <Button
            type="button"
            variant="subtle"
            disabled={busy || passkeyBusy !== null}
            className="w-full"
            onClick={() => onPasskey('login')}
          >
            🔑 {passkeyBusy === 'login'
              ? t('login.passkey.signingIn')
              : t('login.passkey.signIn')}
          </Button>
          <p className="-mt-1 text-[11px] text-text-tertiary">
            {t('login.passkey.signinHint')}
          </p>
          <Button
            type="button"
            variant="ghost"
            disabled={busy || passkeyBusy !== null || !displayName.trim()}
            className="w-full"
            onClick={() => onPasskey('signup')}
          >
            {passkeyBusy === 'signup'
              ? t('login.passkey.signingIn')
              : t('login.passkey.signUp')}
          </Button>
          <p className="-mt-1 text-[11px] text-text-tertiary">
            {t('login.passkey.signupHint')}
          </p>

          {allowMock && (
            <button
              type="button"
              onClick={continueWithoutServer}
              className="w-full pt-2 text-caption text-text-tertiary underline-offset-2 hover:text-text-secondary hover:underline"
            >
              {t('login.devMock')}
            </button>
          )}
        </form>

        {/* Sidecar diagnostics — present even when login is fine, so a
            user can click in and watch the backend boot if curious.
            Auto-expands on any signin failure. */}
        {diag && (
          <div className="mt-6 rounded-sm border border-border bg-surface-1">
            <button
              type="button"
              onClick={() => setShowDiag((s) => !s)}
              className="flex w-full items-center justify-between px-3 py-2 text-caption text-text-secondary hover:text-text-primary"
              aria-expanded={showDiag}
            >
              <span>
                <span className="font-medium">{t('login.diag.title')}</span>
                <span className="ml-2 text-text-tertiary">— {summariseDiag(diag)}</span>
              </span>
              <span className="font-mono">{showDiag ? '▴' : '▾'}</span>
            </button>
            {showDiag && (
              <div className="border-t border-border px-3 pb-3 pt-1">
                <SidecarDiagPanel diag={diag} />
                <div className="mt-3 flex items-center gap-3 text-[10px] text-text-tertiary">
                  <button
                    type="button"
                    onClick={tryRestartSidecar}
                    className="rounded-sm border border-border px-2 py-1 hover:bg-surface-2"
                    title={t('settings.llm.restartHint')}
                  >
                    {t('settings.llm.restart')}
                  </button>
                  <span>
                    {diag.alive
                      ? 'Sidecar is up. If sign-in still fails, the server may be mid-boot — wait a few seconds and try again.'
                      : 'Sidecar is not running. Check the log above for the failure reason, then click Restart sidecar.'}
                  </span>
                </div>
              </div>
            )}
          </div>
        )}

        <p className="mt-10 text-center text-caption text-text-tertiary">
          By signing in you agree to use Nexus as decision-support only,
          not as a substitute for clinical judgement.
        </p>

        <p
          className="mt-4 text-center font-mono text-[10px] text-text-tertiary/60 selectable"
          title="build identifier — please include this when reporting issues"
        >
          v{BUILD_ID}
        </p>
      </div>
    </div>
  );
}
