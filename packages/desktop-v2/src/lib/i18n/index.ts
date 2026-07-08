/**
 * Minimal i18n facade for Nexus desktop-v2.
 *
 * Design points:
 *   * No third-party dependency (react-i18next / react-intl are
 *     overkill for ~200 strings).
 *   * Two dictionaries — en-US is canonical (the source-of-truth
 *     ``Dict`` type), zh-CN must structurally match.
 *   * Active locale held in the Zustand store next to ``theme`` (see
 *     store.ts). Reading via ``useT()`` re-renders the consumer on
 *     locale change just like the theme toggle.
 *   * ``t(key, vars?)`` does a flat lookup + ``{name}`` substitution.
 *     Keys missing in the active locale fall back to the en-US dict
 *     (defence-in-depth — TypeScript should already catch missing
 *     keys at build time, but a future locale added as
 *     ``Partial<Dict>`` would still work).
 *
 * Why not React context: the existing Zustand-based store already
 * holds theme + auth + everything else. Adding one more piece of
 * client state there keeps the access pattern uniform.
 */
import { useAppState } from '../../store';
import { en, type Dict } from './en-US';
import { zh } from './zh-CN';

export type Locale = 'en-US' | 'zh-CN';

export const SUPPORTED_LOCALES: Locale[] = ['zh-CN', 'en-US'];
export const DEFAULT_LOCALE: Locale = 'zh-CN';
export const LOCALE_STORAGE_KEY = 'nexus.locale';

const DICTIONARIES: Record<Locale, Dict> = {
  'en-US': en,
  'zh-CN': zh,
};

/** Pure formatter — pulled out so we can unit-test without React.
 *  Substitutes ``{var}`` in ``template`` from ``vars``. Missing vars
 *  are left as-is (visible ``{var}``) — that's a louder failure than
 *  silently producing an empty string. */
export function formatTemplate(
  template: string,
  vars?: Record<string, string | number>,
): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (m, name) => {
    if (name in vars) {
      const v = vars[name];
      return v === undefined || v === null ? m : String(v);
    }
    return m;
  });
}

/** Resolve a key against the active locale. Falls back to en-US then
 *  to the key itself so the UI never renders an empty bubble. */
export function tFor(
  locale: Locale,
  key: keyof Dict,
  vars?: Record<string, string | number>,
): string {
  const dict = DICTIONARIES[locale] ?? en;
  // typeof dict[key] is string in the canonical case; if a future
  // Partial<Dict> locale omitted a key we fall back to en first.
  const raw = (dict as Record<string, string>)[key as string]
    ?? (en as Record<string, string>)[key as string]
    ?? String(key);
  return formatTemplate(raw, vars);
}

/** React hook — returns a ``t(key, vars?)`` bound to the active locale.
 *  Subscribers re-render on locale change because ``useAppState`` is
 *  reactive. */
export function useT(): (
  key: keyof Dict,
  vars?: Record<string, string | number>,
) => string {
  const locale = useAppState((s) => s.locale);
  return (key, vars) => tFor(locale, key, vars);
}

/** Validate a candidate string is one of the supported locales.
 *  Used when hydrating from localStorage so a corrupted value just
 *  resets to the default rather than crashing the boot path. */
export function isLocale(v: unknown): v is Locale {
  return v === 'en-US' || v === 'zh-CN';
}

/** Read the persisted locale, falling back to the default. */
export function readStoredLocale(): Locale {
  try {
    const raw = localStorage.getItem(LOCALE_STORAGE_KEY);
    if (isLocale(raw)) return raw;
  } catch {
    /* SSR or privacy mode — fall through */
  }
  return DEFAULT_LOCALE;
}

export function writeStoredLocale(loc: Locale): void {
  try {
    localStorage.setItem(LOCALE_STORAGE_KEY, loc);
  } catch {
    /* no-op */
  }
}

/** Map ``ModeKind`` to its dictionary key. Centralised so layout.tsx,
 *  modes.tsx, and the command palette all agree on which string to
 *  show for each mode. Importing this avoids the ``t('mode.' + m as
 *  keyof Dict)`` pattern which TS can't narrow.
 */
import type { ModeKind } from '../util';

export const MODE_LABEL_KEYS: Record<ModeKind, keyof Dict> = {
  today:     'mode.today',
  patient:   'mode.patient',
  encounter: 'mode.encounter',
  imaging:   'mode.imaging',
  labs:      'mode.labs',
  memory:    'mode.memory',
  report:    'mode.report',
  // Research Workspace renders through its own component, not as a
  // per-patient mode tab — reuse the patient label as a safe fallback
  // so existing widgets that index MODE_LABEL_KEYS exhaustively still
  // typecheck.
  research:  'mode.patient',
};

/** Convenience: get the localized label for a single mode without
 *  re-importing both ``useT`` and ``MODE_LABEL_KEYS`` in every
 *  component. */
export function useModeLabel(): (m: ModeKind) => string {
  const t = useT();
  return (m) => t(MODE_LABEL_KEYS[m]);
}
