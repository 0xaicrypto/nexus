/**
 * IdentityPicker — F26.2 (USER_MANAGEMENT.md §6.2)
 *
 * Avatar dropdown for switching between local identities on this Mac.
 * Renders the current identity's emoji + display_name as a clickable
 * pill in the global header. Click → dropdown listing all identities
 * with relative-time "last active" + an "add new identity" footer.
 *
 * Switching flow:
 *   1. POST /auth/identities/{user_id}/activate → new JWT
 *   2. setToken(new_jwt)                         ← in-memory rotation
 *   3. resetForIdentitySwitch()                  ← drop old user's
 *                                                  patients/sessions/
 *                                                  cached UI state
 *   4. refreshPatients() / refreshStudies()      ← repopulate from
 *                                                  the new user
 *   5. Close the dropdown
 *
 * Identity creation flow ("+ 添加新身份"):
 *   - Opens a tiny inline form asking for display_name (required)
 *   - No password, no email — just a name. Backend creates row +
 *     immediately makes it active. Same switch flow as above.
 *
 * No "settings / manage" entry here — that lives in
 * full-screen-overlays.tsx (F26.3). This picker is the FAST switch.
 */
import { useEffect, useRef, useState } from 'react';
import { useAppState } from '../store';
import { api, type Identity } from '../lib/api-client';
import { cn } from '../lib/util';

function formatRelative(iso: string | null): string {
  if (!iso) return '从未';
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
  const identities       = useAppState((s) => s.identities);
  const activeUserId     = useAppState((s) => s.activeUserId);
  const setToken         = useAppState((s) => s.setToken);
  const setActiveUserId  = useAppState((s) => s.setActiveUserId);
  const setIdentities    = useAppState((s) => s.setIdentities);
  const resetForSwitch   = useAppState((s) => s.resetForIdentitySwitch);
  const refreshPatients  = useAppState((s) => s.refreshPatients);
  const refreshStudies   = useAppState((s) => s.refreshStudies);
  const showToast        = useAppState((s) => s.showToast);

  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [addingNew, setAddingNew] = useState(false);
  const [newName, setNewName] = useState('');
  const dropdownRef = useRef<HTMLDivElement | null>(null);

  // Close on outside click — standard dropdown pattern.
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (!dropdownRef.current) return;
      if (!dropdownRef.current.contains(e.target as Node)) {
        setOpen(false);
        setAddingNew(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const active: Identity | null =
    identities.find((i) => i.userId === activeUserId) ?? null;

  async function handleSwitch(target: Identity) {
    if (target.userId === activeUserId) {
      setOpen(false);
      return;
    }
    if (busy) return;
    setBusy(true);
    try {
      const r = await api.activateIdentity(target.userId);
      // Order matters: reset state BEFORE setting token so any
      // in-flight requests using the old token don't pollute the new
      // user's projection caches.
      resetForSwitch();
      setToken(r.access_token);
      setActiveUserId(r.user_id);
      // Refresh the identity list so last_active_at flips to "刚刚".
      try {
        const list = await api.listIdentities();
        setIdentities(list.identities);
      } catch { /* non-fatal */ }
      // Repopulate the new user's data.
      await Promise.all([refreshPatients(), refreshStudies()]);
      showToast(`已切换到 ${target.avatarEmoji} ${target.displayName}`, 'success');
      setOpen(false);
    } catch (e) {
      showToast(
        `切换失败：${e instanceof Error ? e.message : String(e)}`,
        'error',
      );
    } finally {
      setBusy(false);
    }
  }

  async function handleAddNew() {
    const name = newName.trim();
    if (!name) {
      showToast('请输入名字', 'error');
      return;
    }
    if (busy) return;
    setBusy(true);
    try {
      const r = await api.createIdentity({ displayName: name });
      resetForSwitch();
      setToken(r.access_token);
      setActiveUserId(r.user_id);
      // Re-fetch so the new identity is in the list with proper
      // last_active_at.
      try {
        const list = await api.listIdentities();
        setIdentities(list.identities);
      } catch { /* non-fatal */ }
      await Promise.all([refreshPatients(), refreshStudies()]);
      showToast(`已创建身份 ${r.identity.displayName}`, 'success');
      setNewName('');
      setAddingNew(false);
      setOpen(false);
    } catch (e) {
      showToast(
        `创建失败：${e instanceof Error ? e.message : String(e)}`,
        'error',
      );
    } finally {
      setBusy(false);
    }
  }

  // Render the trigger pill: emoji + name. Click → toggle dropdown.
  // We render even with no identities loaded (renders "—") to avoid
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
        title="切换身份 / 添加新身份"
      >
        <span>{triggerLabel}</span>
        <span className="text-text-tertiary">▾</span>
      </button>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-1 w-72
                        rounded-md border border-border bg-surface-1 shadow-lg">
          <div className="px-3 py-2 text-[10px] uppercase tracking-wider
                          text-text-tertiary border-b border-border">
            本机身份
          </div>
          <div className="max-h-72 overflow-y-auto py-1">
            {identities.length === 0 && (
              <div className="px-3 py-2 text-caption text-text-tertiary">
                暂无身份（启动出错？）
              </div>
            )}
            {identities.map((i) => (
              <button
                key={i.userId}
                type="button"
                onClick={() => handleSwitch(i)}
                disabled={busy}
                className={cn(
                  'flex w-full items-center gap-2 px-3 py-2 text-left',
                  'text-caption hover:bg-surface-2 disabled:opacity-50',
                  i.userId === activeUserId && 'bg-surface-2',
                )}
              >
                <span className="text-base">{i.avatarEmoji}</span>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-text-primary">
                    {i.displayName}
                  </div>
                  <div className="truncate text-[10px] text-text-tertiary">
                    {i.userId === activeUserId
                      ? '当前活跃'
                      : `上次活跃 ${formatRelative(i.lastActiveAt)}`}
                  </div>
                </div>
                {i.userId === activeUserId && (
                  <span className="text-confirmed">✓</span>
                )}
              </button>
            ))}
          </div>

          <div className="border-t border-border">
            {!addingNew ? (
              <button
                type="button"
                onClick={() => setAddingNew(true)}
                disabled={busy}
                className="flex w-full items-center gap-2 px-3 py-2 text-left
                           text-caption text-accent hover:bg-surface-2
                           disabled:opacity-50"
              >
                <span>+</span>
                <span>添加新身份</span>
              </button>
            ) : (
              <div className="space-y-2 px-3 py-2">
                <input
                  type="text"
                  autoFocus
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') handleAddNew();
                    if (e.key === 'Escape') {
                      setAddingNew(false);
                      setNewName('');
                    }
                  }}
                  placeholder="新身份的名字"
                  className="w-full rounded-sm border border-border bg-bg
                             px-2 py-1 text-caption text-text-primary"
                  disabled={busy}
                />
                <div className="flex gap-1.5">
                  <button
                    type="button"
                    onClick={handleAddNew}
                    disabled={busy || !newName.trim()}
                    className="flex-1 rounded-sm bg-accent px-2 py-1
                               text-caption text-white hover:bg-accent-hover
                               disabled:opacity-50"
                  >
                    {busy ? '创建中…' : '创建'}
                  </button>
                  <button
                    type="button"
                    onClick={() => { setAddingNew(false); setNewName(''); }}
                    disabled={busy}
                    className="rounded-sm border border-border px-2 py-1
                               text-caption text-text-secondary
                               hover:bg-surface-2 disabled:opacity-50"
                  >
                    取消
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
