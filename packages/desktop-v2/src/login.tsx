/**
 * LoginView — username + password auth (2026-07 server rework).
 *
 * Three screens in one centred card:
 *   - 登录  (login)    — username + password
 *   - 注册  (register) — username + password + confirm + optional
 *                        display name
 *   - claim            — one-time set-password for legacy passwordless
 *                        accounts. Reached automatically when /login
 *                        answers 409 claim_required.
 *
 * Server contract (base http://localhost:8001):
 *   POST /api/v1/auth/register {username, password, display_name?}
 *   POST /api/v1/auth/login    {username, password}
 *   POST /api/v1/auth/claim    {username, password}
 * Errors arrive as {"error":{"code","message"},"status_code"} — we
 * route on ``ApiError.code`` and map to friendly zh/en strings via
 * the ``auth.err.*`` i18n keys.
 *
 * Kept from the previous incarnation:
 *   - Sidecar diagnostics panel (polls get_sidecar_diagnostics every
 *     2 s; auto-expands on error / dead sidecar).
 *   - "Continue without server" offline escape hatch.
 */

import { useEffect, useState, type FormEvent } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import { Button, Input } from './components/ui';
import { useAppState } from './store';
import {
  api, ApiError,
  type AuthSession, type SidecarDiagnostics,
} from './lib/api-client';
import { BUILD_ID } from './lib/build-info';
import { SidecarDiagPanel, summariseDiag } from './components/sidecar-diag-panel';
import { useT } from './lib/i18n';

type AuthMode = 'login' | 'register' | 'claim';

/** Password input with a show/hide toggle. Controlled. */
function PasswordInput({
  id, value, onChange, placeholder, disabled, autoComplete, autoFocus,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  disabled?: boolean;
  autoComplete?: string;
  autoFocus?: boolean;
}) {
  const t = useT();
  const [visible, setVisible] = useState(false);
  return (
    <div className="relative">
      <Input
        id={id}
        type={visible ? 'text' : 'password'}
        autoComplete={autoComplete ?? 'current-password'}
        autoFocus={autoFocus}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        className="pr-10"
      />
      <button
        type="button"
        tabIndex={-1}
        onClick={() => setVisible((v) => !v)}
        className="absolute inset-y-0 right-0 flex items-center px-3
                   text-text-tertiary hover:text-text-primary"
        aria-label={visible ? t('auth.hidePassword') : t('auth.showPassword')}
        title={visible ? t('auth.hidePassword') : t('auth.showPassword')}
      >
        {visible ? <EyeOff size={15} /> : <Eye size={15} />}
      </button>
    </div>
  );
}

export function LoginView() {
  const t                   = useT();
  const setToken            = useAppState((s) => s.setToken);
  const setRole             = useAppState((s) => s.setRole);
  const setStoreDisplayName = useAppState((s) => s.setDisplayName);
  const setActiveUserId     = useAppState((s) => s.setActiveUserId);
  const setIdentities       = useAppState((s) => s.setIdentities);
  const resetForIdentitySwitch = useAppState((s) => s.resetForIdentitySwitch);
  const activeUserId        = useAppState((s) => s.activeUserId);
  const storedName          = useAppState((s) => s.displayName);
  const prefillUsername     = useAppState((s) => s.loginPrefillUsername);
  const setPrefillUsername  = useAppState((s) => s.setLoginPrefillUsername);
  const showToast           = useAppState((s) => s.showToast);

  const [mode, setMode]         = useState<AuthMode>('login');
  // Username prefill priority: account-switcher handoff > remembered
  // display name (best-effort — for most accounts username == the
  // name they typed at registration).
  const [username, setUsername] = useState(prefillUsername ?? storedName ?? '');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm]   = useState('');
  const [displayName, setDisplayName] = useState('');
  const [busy, setBusy]         = useState(false);
  const [error, setError]       = useState<string | null>(null);
  const [allowMock, setAllowMock] = useState(false);

  // One-shot consume of the switcher's username prefill.
  useEffect(() => {
    if (prefillUsername !== null) setPrefillUsername(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sidecar diagnostics polling — cheap (in-memory ring buffer IPC).
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

  // Auto-expand the diag panel the moment the sidecar dies, even
  // before the user tries to sign in.
  useEffect(() => {
    if (diag && diag.pid != null && !diag.alive) setShowDiag(true);
  }, [diag?.alive, diag?.pid]);

  function switchMode(m: AuthMode) {
    setMode(m);
    setError(null);
    setPassword('');
    setConfirm('');
  }

  /** Map an auth failure to a friendly localized message. */
  function describeError(err: unknown): string {
    if (err instanceof ApiError) {
      switch (err.code) {
        case 'username_taken':      return t('auth.err.usernameTaken');
        case 'invalid_credentials': return t('auth.err.invalidCredentials');
        case 'account_disabled':    return t('auth.err.accountDisabled');
        case 'rate_limited':        return t('auth.err.rateLimited');
        case 'user_not_found':      return t('auth.err.userNotFound');
        case 'already_claimed':     return t('auth.err.alreadyClaimed');
        case 'weak_password':
        case 'common_password':     return t('auth.err.weakPassword');
      }
      if (err.status === 422) {
        // Server-side validation — distinguish the "common password"
        // rejection from generic schema errors when possible.
        return /common/i.test(err.serverMessage)
          ? t('auth.err.weakPassword')
          : t('auth.err.validation');
      }
      if (err.status === 429) return t('auth.err.rateLimited');
      return t('auth.err.server', { status: err.status });
    }
    if (err instanceof TypeError) return t('auth.err.network');
    return String(err);
  }

  /** Push a successful register/login/claim session into the store. */
  async function finishSignIn(r: AuthSession) {
    if (activeUserId && activeUserId !== r.userId) {
      // Different account than the one whose data may still be in the
      // store — wipe per-user state BEFORE the new token lands.
      resetForIdentitySwitch();
    }
    setToken(r.token);
    setRole(r.role);
    setActiveUserId(r.userId);
    setStoreDisplayName(r.displayName);
    // Refresh the account-switcher list (best-effort; non-fatal).
    try {
      const list = await api.listIdentities();
      setIdentities(list.identities);
    } catch { /* picker just shows the current account */ }
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);

    const name = username.trim();
    if (!name) { setError(t('auth.err.usernameRequired')); return; }
    if (!password) { setError(t('auth.err.passwordRequired')); return; }

    if (mode === 'register' || mode === 'claim') {
      if (password.length < 8) {
        setError(t('auth.err.passwordTooShort'));
        return;
      }
      if (password !== confirm) {
        setError(t('auth.err.passwordMismatch'));
        return;
      }
    }

    setBusy(true);
    try {
      if (mode === 'login') {
        const r = await api.login(name, password);
        await finishSignIn(r);
        showToast(t('auth.welcomeBack', { name: r.displayName }), 'success');
      } else if (mode === 'register') {
        const r = await api.register({
          username: name,
          password,
          displayName: displayName.trim() || undefined,
        });
        await finishSignIn(r);
        showToast(t('auth.accountCreated', { name: r.displayName }), 'success');
      } else {
        const r = await api.claim(name, password);
        await finishSignIn(r);
        showToast(t('auth.claimed'), 'success');
      }
    } catch (err) {
      // Legacy passwordless account — server refuses /login with 409
      // claim_required until a password is set. Route to the claim
      // screen with the username carried over.
      if (
        mode === 'login' &&
        err instanceof ApiError &&
        err.code === 'claim_required'
      ) {
        switchMode('claim');
        return;
      }
      setError(describeError(err));
      // Only offer the offline escape hatch for infra-level failures —
      // a wrong password shouldn't dangle "continue without server".
      if (err instanceof TypeError ||
          (err instanceof ApiError && err.status >= 500)) {
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

  const anyBusy = busy;

  const tabBtn = (m: 'login' | 'register', label: string) => (
    <button
      type="button"
      onClick={() => switchMode(m)}
      disabled={anyBusy}
      className={
        'flex-1 rounded-sm px-3 py-1.5 text-[14px] font-medium transition-colors ' +
        (mode === m
          ? 'bg-surface text-text-primary shadow-sm'
          : 'text-text-secondary hover:text-text-primary')
      }
      aria-pressed={mode === m}
    >
      {label}
    </button>
  );

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg">
      <div className="w-full max-w-md px-6 py-12">
        <div className="mb-8 text-center">
          <h1 className="font-display text-display text-text-primary">Nexus</h1>
          <p className="mt-2 text-body text-text-secondary">
            {t('login.title')}
          </p>
        </div>

        <div className="rounded-md border border-border bg-surface-1 p-5">
          {mode !== 'claim' ? (
            <>
              {/* 登录 / 注册 tab strip */}
              <div className="mb-5 flex gap-1 rounded-md border border-border bg-bg p-1">
                {tabBtn('login',    t('auth.tab.login'))}
                {tabBtn('register', t('auth.tab.register'))}
              </div>

              <form onSubmit={onSubmit} className="space-y-4 selectable">
                <div>
                  <label
                    htmlFor="auth-username"
                    className="mb-1.5 block text-caption font-medium text-text-secondary"
                  >
                    {t('auth.username')}
                  </label>
                  <Input
                    id="auth-username"
                    type="text"
                    autoComplete="username"
                    required
                    autoFocus
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    placeholder={t('auth.usernamePlaceholder')}
                    disabled={anyBusy}
                  />
                  {mode === 'register' && (
                    <p className="mt-1 text-[11px] text-text-tertiary">
                      {t('auth.usernameHint')}
                    </p>
                  )}
                </div>

                <div>
                  <label
                    htmlFor="auth-password"
                    className="mb-1.5 block text-caption font-medium text-text-secondary"
                  >
                    {t('auth.password')}
                  </label>
                  <PasswordInput
                    id="auth-password"
                    value={password}
                    onChange={setPassword}
                    placeholder={mode === 'register'
                      ? t('auth.passwordPlaceholder') : undefined}
                    autoComplete={mode === 'register'
                      ? 'new-password' : 'current-password'}
                    disabled={anyBusy}
                  />
                  {mode === 'register' && (
                    <p className="mt-1 text-[11px] text-text-tertiary">
                      {t('auth.registerHint')}
                    </p>
                  )}
                </div>

                {mode === 'register' && (
                  <>
                    <div>
                      <label
                        htmlFor="auth-confirm"
                        className="mb-1.5 block text-caption font-medium text-text-secondary"
                      >
                        {t('auth.confirmPassword')}
                      </label>
                      <PasswordInput
                        id="auth-confirm"
                        value={confirm}
                        onChange={setConfirm}
                        autoComplete="new-password"
                        disabled={anyBusy}
                      />
                    </div>
                    <div>
                      <label
                        htmlFor="auth-display-name"
                        className="mb-1.5 block text-caption font-medium text-text-secondary"
                      >
                        {t('auth.displayName')}
                      </label>
                      <Input
                        id="auth-display-name"
                        type="text"
                        autoComplete="name"
                        value={displayName}
                        onChange={(e) => setDisplayName(e.target.value)}
                        placeholder={t('auth.displayNamePlaceholder')}
                        disabled={anyBusy}
                      />
                    </div>
                  </>
                )}

                {error && (
                  <div className="rounded-sm border border-retract/40 bg-retract/10 px-3 py-2 text-caption text-retract">
                    {error}
                  </div>
                )}

                <Button
                  type="submit"
                  variant="primary"
                  disabled={anyBusy}
                  className="w-full"
                >
                  {mode === 'login'
                    ? (busy ? t('auth.signingIn')  : t('auth.signIn'))
                    : (busy ? t('auth.registering') : t('auth.registerCta'))}
                </Button>

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
            </>
          ) : (
            /* ── Claim screen — legacy passwordless account migration ── */
            <form onSubmit={onSubmit} className="space-y-4 selectable">
              <div>
                <h2 className="font-display text-section text-text-primary">
                  {t('auth.claim.title')}
                </h2>
                <p className="mt-2 rounded-sm border border-caution/40 bg-caution/10 px-3 py-2 text-caption text-caution">
                  {t('auth.claim.explain')}
                </p>
              </div>

              <div>
                <label
                  htmlFor="claim-username"
                  className="mb-1.5 block text-caption font-medium text-text-secondary"
                >
                  {t('auth.username')}
                </label>
                <Input
                  id="claim-username"
                  type="text"
                  autoComplete="username"
                  required
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  disabled={anyBusy}
                />
              </div>

              <div>
                <label
                  htmlFor="claim-password"
                  className="mb-1.5 block text-caption font-medium text-text-secondary"
                >
                  {t('auth.password')}
                </label>
                <PasswordInput
                  id="claim-password"
                  value={password}
                  onChange={setPassword}
                  placeholder={t('auth.passwordPlaceholder')}
                  autoComplete="new-password"
                  autoFocus
                  disabled={anyBusy}
                />
              </div>

              <div>
                <label
                  htmlFor="claim-confirm"
                  className="mb-1.5 block text-caption font-medium text-text-secondary"
                >
                  {t('auth.confirmPassword')}
                </label>
                <PasswordInput
                  id="claim-confirm"
                  value={confirm}
                  onChange={setConfirm}
                  autoComplete="new-password"
                  disabled={anyBusy}
                />
              </div>

              {error && (
                <div className="rounded-sm border border-retract/40 bg-retract/10 px-3 py-2 text-caption text-retract">
                  {error}
                </div>
              )}

              <Button
                type="submit"
                variant="primary"
                disabled={anyBusy}
                className="w-full"
              >
                {busy ? t('auth.claim.submitting') : t('auth.claim.submit')}
              </Button>
              <button
                type="button"
                onClick={() => switchMode('login')}
                disabled={anyBusy}
                className="w-full pt-1 text-caption text-text-tertiary underline-offset-2 hover:text-text-secondary hover:underline"
              >
                {t('auth.claim.back')}
              </button>
            </form>
          )}
        </div>

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
