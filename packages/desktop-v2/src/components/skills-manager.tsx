/**
 * SkillsManagerModal — the 技能与插件 management surface (F-skills).
 *
 * Two tabs:
 *   已安装 — every installed skill with an enable/disable switch,
 *            source badge, and confirm-to-uninstall flow.
 *   发现   — search across the official registry + GitHub (source
 *            filter chips), with per-row install buttons.
 *
 * The installed list is the zustand ``skills`` cache (refreshed on
 * open + after every mutation) so the composer's "/" menu stays in
 * sync without its own fetch.
 *
 * Entry points: the "/" menu's 管理技能与插件… row and the
 * AccountMenu's 技能与插件 item (all users — not admin-gated).
 */
import { useEffect, useRef, useState } from 'react';
import { Search, Zap } from 'lucide-react';
import { Modal } from './modal';
import { Chip } from './ui';
import { useAppState } from '../store';
import { useT } from '../lib/i18n';
import { cn } from '../lib/util';
import { api, ApiError, type SkillSearchResult } from '../lib/api-client';

type Tab = 'installed' | 'discover';
type SourceFilter = 'official' | 'github' | null;

export function SkillsManagerModal() {
  const t = useT();
  const open  = useAppState((s) => s.skillsManagerOpen);
  const close = useAppState((s) => s.closeSkillsManager);
  const [tab, setTab] = useState<Tab>('installed');

  // Reset to the installed tab on every open.
  useEffect(() => {
    if (open) setTab('installed');
  }, [open]);

  const tabBtn = (key: Tab, label: string) => (
    <button
      type="button"
      onClick={() => setTab(key)}
      className={cn(
        'rounded-md px-3 py-1 text-sm transition-colors',
        tab === key
          ? 'bg-accent-subtle font-medium text-accent'
          : 'text-text-secondary hover:text-text-primary',
      )}
    >
      {label}
    </button>
  );

  return (
    <Modal
      open={open}
      onClose={close}
      title={t('skills.manager.title')}
      tone="base"
      width={560}
      headerExtra={
        <div className="flex items-center gap-1">
          {tabBtn('installed', t('skills.tab.installed'))}
          {tabBtn('discover', t('skills.tab.discover'))}
        </div>
      }
    >
      {tab === 'installed' ? <InstalledTab /> : <DiscoverTab />}
    </Modal>
  );
}

/* ───────────── 已安装 ───────────── */

function InstalledTab() {
  const t = useT();
  const skills        = useAppState((s) => s.skills);
  const skillsLoaded  = useAppState((s) => s.skillsLoaded);
  const refreshSkills = useAppState((s) => s.refreshSkills);
  const showToast     = useAppState((s) => s.showToast);
  // Name of the skill whose uninstall is awaiting confirmation.
  const [confirming, setConfirming] = useState<string | null>(null);
  // Names with an in-flight toggle/uninstall request.
  const [busy, setBusy] = useState<Set<string>>(new Set());

  useEffect(() => { void refreshSkills(); }, [refreshSkills]);

  const setBusyFor = (name: string, on: boolean) =>
    setBusy((prev) => {
      const next = new Set(prev);
      if (on) next.add(name); else next.delete(name);
      return next;
    });

  async function onToggle(name: string, enabled: boolean) {
    setBusyFor(name, true);
    try {
      await api.toggleSkill(name, enabled);
      await refreshSkills();
    } catch (e) {
      showToast(
        t('skills.installed.toggleFailed', {
          error: e instanceof Error ? e.message : String(e),
        }),
        'error',
      );
    } finally {
      setBusyFor(name, false);
    }
  }

  async function onUninstall(name: string) {
    setBusyFor(name, true);
    try {
      await api.uninstallSkill(name);
      setConfirming(null);
      await refreshSkills();
    } catch (e) {
      showToast(
        t('skills.installed.uninstallFailed', {
          error: e instanceof Error ? e.message : String(e),
        }),
        'error',
      );
    } finally {
      setBusyFor(name, false);
    }
  }

  if (!skillsLoaded) {
    return (
      <div className="py-10 text-center text-caption text-text-tertiary">
        {t('skills.installed.loading')}
      </div>
    );
  }
  if (skills.length === 0) {
    return (
      <div className="flex flex-col items-center gap-2 py-10 text-center">
        <Zap size={20} className="text-text-tertiary" />
        <div className="text-body text-text-secondary">
          {t('skills.installed.empty')}
        </div>
      </div>
    );
  }

  return (
    <ul className="divide-y divide-border">
      {skills.map((s) => {
        const rowBusy = busy.has(s.name);
        return (
          <li key={s.name} className="flex items-start gap-3 py-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="text-body font-medium text-text-primary">
                  {s.name}
                </span>
                <Chip variant={s.source === 'official' ? 'tinted' : 'neutral'}>
                  {s.source === 'official'
                    ? t('skills.source.official')
                    : s.source === 'github'
                    ? t('skills.source.github')
                    : s.source}
                </Chip>
              </div>
              <div className="mt-0.5 truncate text-caption text-text-secondary">
                {s.description}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-2 pt-0.5">
              <SkillSwitch
                enabled={s.enabled}
                disabled={rowBusy}
                labelOn={t('skills.installed.enabled')}
                labelOff={t('skills.installed.disabled')}
                onToggle={(next) => onToggle(s.name, next)}
              />
              {confirming === s.name ? (
                <span className="flex items-center gap-1.5 text-caption">
                  <span className="text-retract">
                    {t('skills.installed.confirmUninstall')}
                  </span>
                  <button
                    type="button"
                    disabled={rowBusy}
                    onClick={() => onUninstall(s.name)}
                    className="rounded-sm border border-retract/40 px-1.5 py-0.5 text-retract hover:bg-retract/10 disabled:opacity-50"
                  >
                    {t('skills.installed.confirmYes')}
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirming(null)}
                    className="rounded-sm border border-border px-1.5 py-0.5 text-text-secondary hover:text-text-primary"
                  >
                    {t('skills.installed.confirmNo')}
                  </button>
                </span>
              ) : (
                <button
                  type="button"
                  disabled={rowBusy}
                  onClick={() => setConfirming(s.name)}
                  className="rounded-sm px-1.5 py-0.5 text-caption text-text-tertiary hover:text-retract disabled:opacity-50"
                >
                  {t('skills.installed.uninstall')}
                </button>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/** Small hand-rolled switch — Radix has no Switch in this project's
 *  dependency set and we can't add packages, so a 32×18 pill button
 *  with a sliding dot stands in. */
function SkillSwitch({
  enabled, disabled, labelOn, labelOff, onToggle,
}: {
  enabled: boolean;
  disabled?: boolean;
  labelOn: string;
  labelOff: string;
  onToggle: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      title={enabled ? labelOn : labelOff}
      disabled={disabled}
      onClick={() => onToggle(!enabled)}
      className={cn(
        'relative h-[18px] w-8 rounded-full transition-colors disabled:opacity-50',
        enabled ? 'bg-accent' : 'bg-border-strong',
      )}
    >
      <span
        className={cn(
          'absolute top-[2px] h-[14px] w-[14px] rounded-full bg-white transition-all',
          enabled ? 'left-[16px]' : 'left-[2px]',
        )}
      />
    </button>
  );
}

/* ───────────── 发现 ───────────── */

function DiscoverTab() {
  const t = useT();
  const refreshSkills = useAppState((s) => s.refreshSkills);
  const showToast = useAppState((s) => s.showToast);
  const [q, setQ]           = useState('');
  const [source, setSource] = useState<SourceFilter>(null);
  const [results, setResults]     = useState<SkillSearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [searched, setSearched]   = useState(false);
  const [error, setError]         = useState<string | null>(null);
  const [installing, setInstalling] = useState<Set<string>>(new Set());
  // Identifiers installed during THIS dialog session — overlays the
  // server-reported ``installed`` flag without a re-search.
  const [justInstalled, setJustInstalled] = useState<Set<string>>(new Set());
  // Monotonic search sequence — a stale response must never clobber
  // the results of a newer query.
  const seqRef = useRef(0);

  // Debounced search on query / source change.
  useEffect(() => {
    const term = q.trim();
    if (!term) {
      setResults([]);
      setSearched(false);
      setSearching(false);
      setError(null);
      return;
    }
    const seq = ++seqRef.current;
    setSearching(true);
    setError(null);
    const timer = setTimeout(async () => {
      try {
        const r = await api.searchSkills(term, source ?? undefined);
        if (seqRef.current !== seq) return;
        setResults(r);
        setSearched(true);
      } catch (e) {
        if (seqRef.current !== seq) return;
        setResults([]);
        setSearched(true);
        if (e instanceof ApiError && e.code === 'search_unavailable') {
          setError(t('skills.discover.unavailable'));
        } else {
          setError(t('skills.discover.error', {
            error: e instanceof Error ? e.message : String(e),
          }));
        }
      } finally {
        if (seqRef.current === seq) setSearching(false);
      }
    }, 350);
    return () => clearTimeout(timer);
    // ``t`` is stable per locale; intentionally not a dep to avoid
    // re-searching on locale switch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, source]);

  async function onInstall(r: SkillSearchResult) {
    setInstalling((prev) => new Set(prev).add(r.identifier));
    setError(null);
    try {
      const res = await api.installSkill(r.identifier);
      setJustInstalled((prev) => new Set(prev).add(r.identifier));
      // Repo-root "skill pack" installs bring in several skills at
      // once — surface how many actually landed.
      if ((res.count ?? 1) > 1) {
        showToast(
          t('skills.discover.installedPack', { count: String(res.count) }),
          'success',
        );
      }
      await refreshSkills();
    } catch (e) {
      if (e instanceof ApiError && e.code === 'already_installed') {
        // Someone (another window?) beat us to it — treat as success.
        setJustInstalled((prev) => new Set(prev).add(r.identifier));
        await refreshSkills();
      } else if (e instanceof ApiError && e.code === 'install_network') {
        // GitHub unreachable (GFW / offline) — actionable hint: point
        // the user at the NEXUS_GITHUB_MIRROR .env setting.
        setError(t('skills.discover.installNetwork'));
      } else {
        setError(t('skills.discover.installFailed', {
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    } finally {
      setInstalling((prev) => {
        const next = new Set(prev);
        next.delete(r.identifier);
        return next;
      });
    }
  }

  const sourceChip = (key: 'official' | 'github', label: string) => (
    <button
      type="button"
      onClick={() => setSource((cur) => (cur === key ? null : key))}
      className={cn(
        'rounded-full border px-2.5 py-0.5 text-caption transition-colors',
        source === key
          ? 'border-accent bg-accent-subtle text-accent'
          : 'border-border text-text-secondary hover:border-border-strong',
      )}
    >
      {label}
    </button>
  );

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2 rounded-sm border border-border bg-bg px-3 py-2 focus-within:border-accent">
        <Search size={14} className="shrink-0 text-text-tertiary" />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t('skills.discover.searchPlaceholder')}
          className="flex-1 bg-transparent text-body text-text-primary placeholder:text-text-tertiary focus:outline-none"
        />
      </div>
      <div className="flex items-center gap-1.5">
        {sourceChip('official', t('skills.source.official'))}
        {sourceChip('github', t('skills.source.github'))}
        {/* Subtle offline-catalog badge — the current results came
            from the server's built-in snapshot because GitHub was
            unreachable (cached: true rows). */}
        {!searching && results.some((r) => r.cached) && (
          <span
            title={t('skills.discover.installNetwork')}
            className="ml-auto rounded-full border border-border px-2 py-0.5 text-caption text-text-tertiary"
          >
            {t('skills.discover.offlineCatalog')}
          </span>
        )}
      </div>

      {error && (
        <div className="rounded-sm border border-retract/40 bg-retract/10 px-3 py-2 text-caption text-retract">
          {error}
        </div>
      )}

      {searching && (
        <div className="py-6 text-center text-caption text-text-tertiary">
          {t('skills.discover.searching')}
        </div>
      )}
      {!searching && !searched && !error && (
        <div className="py-6 text-center text-caption text-text-tertiary">
          {t('skills.discover.hint')}
        </div>
      )}
      {!searching && searched && !error && results.length === 0 && (
        <div className="py-6 text-center text-caption text-text-tertiary">
          {t('skills.discover.empty')}
        </div>
      )}

      {!searching && results.length > 0 && (
        <ul className="divide-y divide-border">
          {results.map((r) => {
            const installed  = r.installed || justInstalled.has(r.identifier);
            const rowBusy    = installing.has(r.identifier);
            return (
              <li key={r.identifier} className="flex items-start gap-3 py-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-body font-medium text-text-primary">
                      {r.name}
                    </span>
                    <Chip variant={r.source === 'official' ? 'tinted' : 'neutral'}>
                      {r.source === 'official'
                        ? t('skills.source.official')
                        : r.source === 'github'
                        ? t('skills.source.github')
                        : r.source}
                    </Chip>
                  </div>
                  <div className="mt-0.5 truncate text-caption text-text-secondary">
                    {r.description}
                  </div>
                </div>
                {installed ? (
                  <span className="shrink-0 pt-1 text-caption text-confirmed">
                    {t('skills.discover.installed')}
                  </span>
                ) : (
                  <button
                    type="button"
                    disabled={rowBusy}
                    onClick={() => onInstall(r)}
                    className="shrink-0 rounded-sm bg-accent px-2.5 py-1 text-caption font-medium text-white hover:bg-accent-hover disabled:opacity-60"
                  >
                    {rowBusy
                      ? t('skills.discover.installing')
                      : t('skills.discover.install')}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
