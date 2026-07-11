/**
 * AdminUsersView — 用户管理 (admin-only user management panel).
 *
 * Opens from AccountMenu → 用户管理 (the entry is rendered only when
 * ``store.role === 'admin'``; the server additionally enforces
 * role=admin on every endpoint and answers 403 admin_required).
 *
 * Server surface:
 *   GET  /api/v1/admin/users
 *   POST /api/v1/admin/users/{id}/disable   (400 cannot_disable_self)
 *   POST /api/v1/admin/users/{id}/enable
 *   POST /api/v1/admin/users/{id}/reset-password {new_password}
 *
 * UI: full-width dialog (same Radix pattern as the other full-screen
 * overlays) with a table of users — username, role, created, last
 * login, status badges (active/disabled · has_password)
 * — and per-row actions:
 *   - Disable (confirm dialog; hidden on the signed-in admin's row)
 *   - Enable
 *   - Reset password (small form modal with generate-random + copy)
 */

import { useCallback, useEffect, useState, type ReactNode } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import {
  X, RefreshCw, Ban, CheckCircle2, KeyRound, Copy, Dices,
} from 'lucide-react';
import { Button, Chip, Input } from './ui';
import { api, ApiError, type AdminUser } from '../lib/api-client';
import { useAppState } from '../store';
import { cn } from '../lib/util';
import { useT } from '../lib/i18n';

/* ───────────── helpers ───────────── */

/** Render a server timestamp (ISO string or epoch seconds/ms). */
function fmtWhen(v: string | number | null, never: string): string {
  if (v === null || v === undefined || v === '') return never;
  let d: Date;
  if (typeof v === 'number') {
    d = new Date(v < 1e12 ? v * 1000 : v);   // epoch s vs ms
  } else {
    d = new Date(v);
  }
  if (Number.isNaN(d.getTime())) return String(v);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 16-char random password: unambiguous alphanumerics + symbols,
 *  crypto-grade randomness. Always satisfies the ≥8-char rule and is
 *  never a "common password". */
function generatePassword(): string {
  const alphabet =
    'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789!@#$%^&*';
  const buf = new Uint32Array(16);
  crypto.getRandomValues(buf);
  let out = '';
  for (const n of buf) out += alphabet[n % alphabet.length];
  return out;
}

/** Map admin-endpoint failures onto friendly localized strings. */
function describeAdminError(
  err: unknown,
  t: (k: any, v?: Record<string, string | number>) => string,
): string {
  if (err instanceof ApiError) {
    switch (err.code) {
      case 'cannot_disable_self': return t('admin.err.cannotDisableSelf');
      case 'admin_required':      return t('admin.err.adminRequired');
      case 'rate_limited':        return t('auth.err.rateLimited');
    }
    return t('admin.err.generic', { error: err.serverMessage || String(err.status) });
  }
  if (err instanceof TypeError) return t('auth.err.network');
  return t('admin.err.generic', { error: String(err) });
}

/* ───────────── inner modal shell (confirm / reset) ─────────────
 * Rendered INSIDE the Radix Dialog.Content, so a plain fixed overlay
 * with a higher z-index is enough — no nested portals needed. */

function InnerModal({
  onClose, children,
}: {
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="w-full max-w-sm rounded-lg border border-border-strong bg-surface p-5 shadow-2xl">
        {children}
      </div>
    </div>
  );
}

/* ───────────── main view ───────────── */

export function AdminUsersView() {
  const t            = useT();
  const open         = useAppState((s) => s.adminUsersOverlayOpen);
  const close        = useAppState((s) => s.closeAdminUsersOverlay);
  const role         = useAppState((s) => s.role);
  const activeUserId = useAppState((s) => s.activeUserId);
  const showToast    = useAppState((s) => s.showToast);

  const [users, setUsers]         = useState<AdminUser[]>([]);
  const [loading, setLoading]     = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Row-level busy flag so double-clicks don't double-fire.
  const [busyUserId, setBusyUserId] = useState<string | null>(null);

  // Sub-dialogs.
  const [confirmDisable, setConfirmDisable] = useState<AdminUser | null>(null);
  const [resetTarget, setResetTarget]       = useState<AdminUser | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const list = await api.adminListUsers();
      setUsers(list);
    } catch (e) {
      setLoadError(describeAdminError(e, t));
    } finally {
      setLoading(false);
    }
    // ``t`` is stable enough for our purposes (changes only on locale
    // switch, which re-renders anyway).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (open) void refresh();
  }, [open, refresh]);

  async function doDisable(u: AdminUser) {
    setBusyUserId(u.userId);
    try {
      await api.adminDisableUser(u.userId);
      showToast(t('admin.disabled.toast', { username: u.username }), 'success');
      setConfirmDisable(null);
      await refresh();
    } catch (e) {
      showToast(describeAdminError(e, t), 'error');
      setConfirmDisable(null);
    } finally {
      setBusyUserId(null);
    }
  }

  async function doEnable(u: AdminUser) {
    setBusyUserId(u.userId);
    try {
      await api.adminEnableUser(u.userId);
      showToast(t('admin.enabled.toast', { username: u.username }), 'success');
      await refresh();
    } catch (e) {
      showToast(describeAdminError(e, t), 'error');
    } finally {
      setBusyUserId(null);
    }
  }

  // Server enforces this too (403 admin_required) — the client gate
  // just avoids rendering a panel that can only fail.
  if (role !== 'admin') return null;

  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && close()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/50" />
        <Dialog.Content
          className={cn(
            'fixed inset-x-0 top-0 z-50 mx-auto my-8 max-w-4xl',
            'rounded-lg border border-border-strong bg-surface shadow-2xl',
            'max-h-[85vh] overflow-y-auto focus:outline-none',
          )}
        >
          <div className="flex items-center justify-between border-b border-border px-6 py-4">
            <Dialog.Title asChild>
              <h1 className="font-display text-section">
                {t('admin.title')}
                <span className="ml-2 text-caption text-text-tertiary">
                  {users.length > 0 ? users.length : ''}
                </span>
              </h1>
            </Dialog.Title>
            <div className="flex items-center gap-2">
              <Button
                variant="ghost"
                className="!px-2 !py-1 !text-[13px]"
                onClick={() => void refresh()}
                disabled={loading}
              >
                <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
                {t('admin.refresh')}
              </Button>
              <Dialog.Close className="rounded-sm p-1 text-text-tertiary hover:bg-accent-subtle">
                <X size={16} />
              </Dialog.Close>
            </div>
          </div>

          <p className="px-6 py-3 text-caption text-text-secondary">
            {t('admin.subtitle')}
          </p>

          {loading && users.length === 0 && (
            <p className="px-6 py-6 text-caption text-text-tertiary">
              {t('admin.loading')}
            </p>
          )}

          {loadError && (
            <div className="mx-6 mb-4 rounded-sm border border-retract/40 bg-retract/10 px-3 py-2 text-caption text-retract">
              {t('admin.loadFailed', { error: loadError })}
            </div>
          )}

          {!loading && !loadError && users.length === 0 && (
            <p className="px-6 py-10 text-center text-caption text-text-tertiary">
              {t('admin.empty')}
            </p>
          )}

          {users.length > 0 && (
            <div className="px-6 pb-6">
              <table className="w-full border-collapse text-caption">
                <thead>
                  <tr className="border-b border-border text-left text-[10px] uppercase tracking-wider text-text-tertiary">
                    <th className="py-2 pr-3 font-medium">{t('admin.col.username')}</th>
                    <th className="py-2 pr-3 font-medium">{t('admin.col.role')}</th>
                    <th className="py-2 pr-3 font-medium">{t('admin.col.created')}</th>
                    <th className="py-2 pr-3 font-medium">{t('admin.col.lastLogin')}</th>
                    <th className="py-2 pr-3 font-medium">{t('admin.col.status')}</th>
                    <th className="py-2 text-right font-medium">{t('admin.col.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((u) => {
                    const isSelf   = u.userId === activeUserId;
                    const disabled = u.disabledAt !== null;
                    const rowBusy  = busyUserId === u.userId;
                    return (
                      <tr
                        key={u.userId}
                        className={cn(
                          'border-b border-border/60',
                          disabled && 'opacity-60',
                        )}
                      >
                        <td className="py-2.5 pr-3">
                          <span className="font-medium text-text-primary">
                            {u.username}
                          </span>
                          {isSelf && (
                            <span className="ml-1.5 text-[10px] text-text-tertiary">
                              ({t('admin.you')})
                            </span>
                          )}
                        </td>
                        <td className="py-2.5 pr-3">
                          <Chip variant={u.role === 'admin' ? 'tinted' : 'neutral'}>
                            {u.role === 'admin'
                              ? t('admin.role.admin')
                              : t('admin.role.user')}
                          </Chip>
                        </td>
                        <td className="py-2.5 pr-3 font-mono text-[11px] text-text-secondary">
                          {fmtWhen(u.createdAt, t('admin.never'))}
                        </td>
                        <td className="py-2.5 pr-3 font-mono text-[11px] text-text-secondary">
                          {fmtWhen(u.lastLoginAt, t('admin.never'))}
                        </td>
                        <td className="py-2.5 pr-3">
                          <div className="flex flex-wrap gap-1">
                            {disabled ? (
                              <Chip variant="retract">{t('admin.status.disabled')}</Chip>
                            ) : (
                              <Chip variant="confirmed">{t('admin.status.active')}</Chip>
                            )}
                            {u.hasPassword ? (
                              <Chip>{t('admin.status.password')}</Chip>
                            ) : (
                              <Chip variant="caution">{t('admin.status.noPassword')}</Chip>
                            )}
                          </div>
                        </td>
                        <td className="py-2.5 text-right">
                          <div className="flex justify-end gap-1.5">
                            <button
                              type="button"
                              onClick={() => setResetTarget(u)}
                              disabled={rowBusy}
                              className="inline-flex items-center gap-1 rounded-sm border border-border px-2 py-1 text-[11px] text-text-secondary hover:bg-accent-subtle hover:text-text-primary disabled:opacity-50"
                            >
                              <KeyRound size={11} />
                              {t('admin.action.resetPassword')}
                            </button>
                            {disabled ? (
                              <button
                                type="button"
                                onClick={() => void doEnable(u)}
                                disabled={rowBusy}
                                className="inline-flex items-center gap-1 rounded-sm border border-confirmed/40 px-2 py-1 text-[11px] text-confirmed hover:bg-confirmed/10 disabled:opacity-50"
                              >
                                <CheckCircle2 size={11} />
                                {t('admin.action.enable')}
                              </button>
                            ) : !isSelf ? (
                              /* Disable is hidden on the signed-in
                                 admin's own row — the server would
                                 reject it (400 cannot_disable_self)
                                 anyway. */
                              <button
                                type="button"
                                onClick={() => setConfirmDisable(u)}
                                disabled={rowBusy}
                                className="inline-flex items-center gap-1 rounded-sm border border-retract/40 px-2 py-1 text-[11px] text-retract hover:bg-retract/10 disabled:opacity-50"
                              >
                                <Ban size={11} />
                                {t('admin.action.disable')}
                              </button>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* ── confirm-disable dialog ── */}
          {confirmDisable && (
            <InnerModal onClose={() => setConfirmDisable(null)}>
              <h2 className="font-display text-section text-text-primary">
                {t('admin.confirmDisable.title')}
              </h2>
              <p className="mt-2 text-body text-text-secondary">
                {t('admin.confirmDisable.body', { username: confirmDisable.username })}
              </p>
              <div className="mt-5 flex justify-end gap-2">
                <Button
                  variant="subtle"
                  onClick={() => setConfirmDisable(null)}
                  disabled={busyUserId !== null}
                >
                  {t('admin.confirm.cancel')}
                </Button>
                <Button
                  variant="primary"
                  className="!bg-retract hover:!bg-retract/90"
                  onClick={() => void doDisable(confirmDisable)}
                  disabled={busyUserId !== null}
                >
                  {t('admin.confirm.disable')}
                </Button>
              </div>
            </InnerModal>
          )}

          {/* ── reset-password dialog ── */}
          {resetTarget && (
            <ResetPasswordModal
              user={resetTarget}
              onClose={() => setResetTarget(null)}
              onDone={() => {
                setResetTarget(null);
                void refresh();
              }}
            />
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/* ───────────── reset-password sub-modal ───────────── */

function ResetPasswordModal({
  user, onClose, onDone,
}: {
  user: AdminUser;
  onClose: () => void;
  onDone: () => void;
}) {
  const t         = useT();
  const showToast = useAppState((s) => s.showToast);
  const [pw, setPw]         = useState('');
  const [busy, setBusy]     = useState(false);
  const [error, setError]   = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  function generate() {
    setPw(generatePassword());
    setCopied(false);
    setError(null);
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(pw);
      setCopied(true);
      showToast(t('admin.reset.copied'), 'success');
    } catch {
      // Clipboard API unavailable (non-secure context) — select-and-
      // copy manually; nothing else to do.
      setCopied(false);
    }
  }

  async function submit() {
    setError(null);
    if (pw.length < 8) {
      setError(t('auth.err.passwordTooShort'));
      return;
    }
    setBusy(true);
    try {
      await api.adminResetPassword(user.userId, pw);
      showToast(t('admin.reset.done', { username: user.username }), 'success');
      onDone();
    } catch (e) {
      setError(describeAdminError(e, t));
    } finally {
      setBusy(false);
    }
  }

  return (
    <InnerModal onClose={() => { if (!busy) onClose(); }}>
      <h2 className="font-display text-section text-text-primary">
        {t('admin.reset.title', { username: user.username })}
      </h2>
      <p className="mt-2 text-caption text-text-secondary">
        {t('admin.reset.explain')}
      </p>

      <label
        htmlFor="admin-reset-pw"
        className="mt-4 mb-1.5 block text-caption font-medium text-text-secondary"
      >
        {t('admin.reset.newPassword')}
      </label>
      <div className="flex gap-1.5">
        <Input
          id="admin-reset-pw"
          type="text"
          autoFocus
          value={pw}
          onChange={(e) => { setPw(e.target.value); setCopied(false); }}
          onKeyDown={(e) => { if (e.key === 'Enter') void submit(); }}
          placeholder={t('auth.passwordPlaceholder')}
          disabled={busy}
          className="font-mono"
        />
        <button
          type="button"
          onClick={generate}
          disabled={busy}
          title={t('admin.reset.generate')}
          className="inline-flex shrink-0 items-center gap-1 rounded-sm border border-border px-2.5 text-caption text-text-secondary hover:bg-accent-subtle hover:text-text-primary disabled:opacity-50"
        >
          <Dices size={13} />
          {t('admin.reset.generate')}
        </button>
        <button
          type="button"
          onClick={() => void copy()}
          disabled={busy || !pw}
          title={t('admin.reset.copy')}
          className={cn(
            'inline-flex shrink-0 items-center gap-1 rounded-sm border px-2.5 text-caption disabled:opacity-50',
            copied
              ? 'border-confirmed/40 text-confirmed'
              : 'border-border text-text-secondary hover:bg-accent-subtle hover:text-text-primary',
          )}
        >
          <Copy size={13} />
          {t('admin.reset.copy')}
        </button>
      </div>

      {error && (
        <div className="mt-3 rounded-sm border border-retract/40 bg-retract/10 px-3 py-2 text-caption text-retract">
          {error}
        </div>
      )}

      <div className="mt-5 flex justify-end gap-2">
        <Button variant="subtle" onClick={onClose} disabled={busy}>
          {t('admin.confirm.cancel')}
        </Button>
        <Button
          variant="primary"
          onClick={() => void submit()}
          disabled={busy || pw.length < 8}
        >
          {busy ? t('admin.reset.submitting') : t('admin.reset.submit')}
        </Button>
      </div>
    </InnerModal>
  );
}
