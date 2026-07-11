/**
 * IdentityPicker — account switcher (2026-07 auth rework).
 *
 * The old server-side "create identity" / "switch identity" endpoints
 * are GONE — accounts are username+password now, so "switching" means
 * signing in as the other account.
 *
 * What this component does today:
 *   - Renders the signed-in account's emoji + display name as a pill
 *     in the global header.
 *   - Dropdown lists the accounts known to this device (from
 *     GET /auth/identities — still available, read-only).
 *   - Clicking another account LOGS OUT and routes to the login
 *     screen with that account's name prefilled in the username
 *     field (best-effort: for most accounts username == the name
 *     they registered with).
 *   - "Sign in with another account…" logs out with an empty prefill
 *     so the medic can log in or register fresh.
 */
import { useEffect, useRef, useState } from 'react';
import { useAppState } from '../store';
import { api, type Identity } from '../lib/api-client';
import { cn } from '../lib/util';
import { useT } from '../lib/i18n';

function formatRelative(iso: string | null, never: string): string {
  if (!iso) return never;
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return '—';
  const min = Math.floor(ms / 60_000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min} 分钟前`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h} 小时前`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d} 天前`;
  const mo = Math.floor(d / 30);
  return `${mo} 个月前`;
}

export function IdentityPicker() {
  const t                = useT();
  const identities       = useAppState((s) => s.identities);
  const activeUserId     = useAppState((s) => s.activeUserId);
  const setIdentities    = useAppState((s) => s.setIdentities);
  const logout           = useAppState((s) => s.logout);
  const setPrefill       = useAppState((s) => s.setLoginPrefillUsername);
  const showToast        = useAppState((s) => s.showToast);

  const [open, setOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement | null>(null);

  // Close on outside click — standard dropdown pattern.
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (!dropdownRef.current) return;
      if (!dropdownRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  // Refresh the read-only identities list whenever the dropdown opens
  // so last_active_at is current. Best-effort; the cached list still
  // renders if the fetch fails.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      try {
        const list = await api.listIdentities();
        if (!cancelled) setIdentities(list.identities);
      } catch { /* keep cached list */ }
    })();
    return () => { cancelled = true; };
  }, [open, setIdentities]);

  const active: Identity | null =
    identities.find((i) => i.userId === activeUserId) ?? null;

  /** Switching accounts = sign out + prefill the login form. There is
   *  no server-side "activate" any more — the other account's
   *  password is required. */
  function handleSwitch(target: Identity) {
    setOpen(false);
    if (target.userId === activeUserId) return;
    setPrefill(target.displayName);
    logout();
    showToast(t('switcher.signedOutToSwitch', { name: target.displayName }), 'info');
  }

  /** "Add" = sign out with a blank login form (register tab is one
   *  click away on the login screen). */
  function handleAdd() {
    setOpen(false);
    setPrefill(null);
    logout();
    showToast(t('switcher.signedOutToAdd'), 'info');
  }

  // Render the trigger pill: emoji + name. Click → toggle dropdown.
  // We render even with no identities loaded (renders "…") to avoid
  // layout shift during boot.
  const triggerLabel =
    active ? `${active.avatarEmoji} ${active.displayName}` : '🩺 …';

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'flex items-center gap-1.5 rounded-md border border-border',
          'px-2.5 py-1 text-caption text-text-secondary',
          'hover:border-border-strong hover:bg-surface-1',
        )}
        title={t('switcher.switch')}
      >
        <span>{triggerLabel}</span>
        <span className="text-text-tertiary">▾</span>
      </button>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-1 w-72
                        rounded-md border border-border bg-surface-1 shadow-lg">
          <div className="px-3 py-2 text-[10px] uppercase tracking-wider
                          text-text-tertiary border-b border-border">
            {t('switcher.title')}
          </div>
          <div className="max-h-72 overflow-y-auto py-1">
            {identities.length === 0 && (
              <div className="px-3 py-2 text-caption text-text-tertiary">
                {t('switcher.empty')}
              </div>
            )}
            {identities.map((i) => (
              <button
                key={i.userId}
                type="button"
                onClick={() => handleSwitch(i)}
                className={cn(
                  'flex w-full items-center gap-2 px-3 py-2 text-left',
                  'text-caption hover:bg-surface-2',
                  i.userId === activeUserId && 'bg-surface-2',
                )}
                title={i.userId === activeUserId
                  ? undefined
                  : t('switcher.switchHint')}
              >
                <span className="text-base">{i.avatarEmoji}</span>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-text-primary">
                    {i.displayName}
                  </div>
                  <div className="truncate text-[10px] text-text-tertiary">
                    {i.userId === activeUserId
                      ? t('switcher.current')
                      : t('switcher.lastActive', {
                          when: formatRelative(i.lastActiveAt, t('switcher.never')),
                        })}
                  </div>
                </div>
                {i.userId === activeUserId && (
                  <span className="text-confirmed">✓</span>
                )}
              </button>
            ))}
          </div>

          <div className="border-t border-border">
            <button
              type="button"
              onClick={handleAdd}
              className="flex w-full items-center gap-2 px-3 py-2 text-left
                         text-caption text-accent hover:bg-surface-2"
            >
              <span>+</span>
              <span>{t('switcher.add')}</span>
            </button>
            <p className="px-3 pb-2 text-[10px] leading-relaxed text-text-tertiary">
              {t('switcher.switchHint')}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
