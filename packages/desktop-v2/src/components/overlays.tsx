/**
 * Three Radix-backed overlays that live above the main canvas:
 *   • CommandPalette — ⌘K fuzzy search over patients / actions
 *   • NewPatientDialog — header "+ New patient" target
 *   • AccountMenu — popover from the avatar button (theme, logout)
 *
 * Plus the Toast strip rendered at the bottom-right of the window.
 */

import { useState, useEffect, useMemo, useRef, type ReactNode } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import * as Popover from '@radix-ui/react-popover';
import {
  Search, X, Plus, LogOut, Sun, Moon, User, Settings as SettingsIcon,
  Send, Mail, AlertTriangle, CheckCircle,
} from 'lucide-react';
import { Button, Input, Chip } from './ui';
import { useAppState } from '../store';
import { cn, MODE_LABELS, patientDisplayLabel, type PatientCard, type ModeKind } from '../lib/util';
import { api, ApiError, type EmailTransportStatus } from '../lib/api-client';

/* ───────────── CommandPalette (⌘K) ───────────── */

interface PaletteAction {
  kind: 'patient' | 'mode' | 'action';
  label: string;
  hint?: string;
  patient?: PatientCard;
  mode?: ModeKind;
  onRun: () => void;
}

export function CommandPalette() {
  const open       = useAppState((s) => s.commandPaletteOpen);
  const close      = useAppState((s) => s.closeCommandPalette);
  const patients   = useAppState((s) => s.patients);
  const setPatient = useAppState((s) => s.setActivePatient);
  const setMode    = useAppState((s) => s.setActiveMode);
  const openNew    = useAppState((s) => s.openNewPatientDialog);
  const openCompose = useAppState((s) => s.openEmailComposer);
  const activePatient = useAppState((s) => s.activePatient);

  const [q, setQ]           = useState('');
  const [cursor, setCursor] = useState(0);
  const inputRef            = useRef<HTMLInputElement>(null);

  // Reset query when opening
  useEffect(() => {
    if (open) {
      setQ('');
      setCursor(0);
      // Radix focuses the dialog; we want focus on the input
      setTimeout(() => inputRef.current?.focus(), 10);
    }
  }, [open]);

  const actions: PaletteAction[] = useMemo(() => {
    const list: PaletteAction[] = [];

    // Patient picker
    for (const p of patients) {
      list.push({
        kind: 'patient',
        label: patientDisplayLabel(p),
        hint: `${p.sex} · ${p.ageGroup} · ${p.latestModality}`,
        patient: p,
        onRun: () => {
          setPatient(p);
          close();
        },
      });
    }

    // Mode jumps (only if a patient is active)
    if (activePatient) {
      (['patient', 'encounter', 'imaging', 'labs', 'memory', 'report'] as ModeKind[]).forEach((m) => {
        list.push({
          kind: 'mode',
          label: `Open ${MODE_LABELS[m]}`,
          hint: `for ${patientDisplayLabel(activePatient)}`,
          mode: m,
          onRun: () => {
            setMode(m);
            close();
          },
        });
      });
    }

    // Global actions
    list.push({
      kind: 'action',
      label: 'New patient',
      hint: '⌘N',
      onRun: () => {
        close();
        openNew();
      },
    });
    list.push({
      kind: 'action',
      label: 'Compose email',
      hint: 'send via relay / SMTP',
      onRun: () => {
        close();
        openCompose();
      },
    });
    list.push({
      kind: 'action',
      label: 'Back to Today',
      onRun: () => {
        setPatient(null);
        close();
      },
    });

    return list;
  }, [patients, activePatient, setPatient, setMode, close, openNew, openCompose]);

  const filtered = useMemo(() => {
    const term = q.trim().toLowerCase();
    if (!term) return actions.slice(0, 12);
    return actions
      .filter((a) =>
        (a.label + ' ' + (a.hint ?? '')).toLowerCase().includes(term),
      )
      .slice(0, 24);
  }, [actions, q]);

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setCursor((c) => Math.min(c + 1, filtered.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setCursor((c) => Math.max(c - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      filtered[cursor]?.onRun();
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && close()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40 backdrop-blur-[2px]" />
        <Dialog.Content
          className={cn(
            'fixed left-1/2 top-[20%] z-50 w-full max-w-xl -translate-x-1/2',
            'rounded-lg border border-border-strong bg-surface shadow-2xl',
            'focus:outline-none',
          )}
        >
          <Dialog.Title className="sr-only">Command palette</Dialog.Title>

          <div className="flex items-center gap-2 border-b border-border px-4 py-3">
            <Search size={16} className="text-text-tertiary" />
            <input
              ref={inputRef}
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setCursor(0);
              }}
              onKeyDown={onKeyDown}
              placeholder="Search patients, jump to mode, run action…"
              className="flex-1 bg-transparent text-body text-text-primary placeholder:text-text-tertiary focus:outline-none"
            />
            <kbd className="rounded-sm border border-border px-1.5 py-0.5 font-mono text-[10px] text-text-tertiary">
              esc
            </kbd>
          </div>

          <ul className="max-h-[50vh] overflow-y-auto py-2">
            {filtered.length === 0 && (
              <li className="px-4 py-6 text-center text-caption text-text-tertiary">
                No matches
              </li>
            )}
            {filtered.map((a, i) => (
              <li
                key={`${a.kind}:${a.label}:${i}`}
                onMouseEnter={() => setCursor(i)}
                onClick={() => a.onRun()}
                className={cn(
                  'flex cursor-pointer items-center justify-between px-4 py-2',
                  i === cursor && 'bg-accent-subtle',
                )}
              >
                <div className="flex items-center gap-3">
                  <Chip variant={a.kind === 'patient' ? 'tinted' : 'neutral'} mono={a.kind === 'patient'}>
                    {a.kind}
                  </Chip>
                  <span className="text-body text-text-primary">{a.label}</span>
                </div>
                {a.hint && (
                  <span className="text-caption text-text-tertiary">{a.hint}</span>
                )}
              </li>
            ))}
          </ul>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/* ───────────── NewPatientDialog ───────────── */

export function NewPatientDialog() {
  const open  = useAppState((s) => s.newPatientDialogOpen);
  const close = useAppState((s) => s.closeNewPatientDialog);
  const showToast       = useAppState((s) => s.showToast);
  const refreshPatients = useAppState((s) => s.refreshPatients);
  const setActivePatient= useAppState((s) => s.setActivePatient);

  // The backend hashes either MRN, or (initials | age | sex). At least
  // one of (initials, mrn) is required so the hash isn't empty. We let
  // the medic enter EITHER initials OR mrn — whichever they prefer.
  const [initials, setInitials] = useState('');
  const [mrn, setMrn]   = useState('');
  const [sex, setSex]   = useState<'M' | 'F' | ''>('');
  const [age, setAge]   = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setInitials(''); setMrn('');
      setSex(''); setAge(''); setNote('');
      setBusy(false); setError(null);
    }
  }, [open]);

  // Parse age input — accepts "65" (int) or "60-69" (range, takes the
  // lower bound). Backend wants an int 0-130; it'll bucket to age_group.
  function parseAge(s: string): number {
    const m = s.match(/^\s*(\d+)/);
    if (!m) return 0;
    const n = parseInt(m[1], 10);
    if (isNaN(n) || n < 0 || n > 130) return 0;
    return n;
  }

  async function onCreate() {
    setError(null);

    const inits = initials.trim();
    const m     = mrn.trim();
    if (!inits && !m) {
      setError('Enter initials or MRN — one is required so we can mint a PHI-safe hash.');
      return;
    }

    setBusy(true);
    try {
      const result = await api.createManualPatient({
        initials:       inits,
        mrn:            m,
        age:            parseAge(age),
        sex:            sex || 'O',
        chiefComplaint: note.trim(),
      });
      // Refresh the patient rail so the new patient shows up immediately,
      // then look up the canonical PatientCard from the fresh list and
      // navigate to it. (We can't just construct a card here — backend
      // owns derived fields like study_count / latest_modality.)
      await refreshPatients();
      const fresh = useAppState.getState().patients
        .find((p) => p.patientHash === result.patientHash);
      if (fresh) setActivePatient(fresh);
      close();
      showToast(
        `Patient registered: ${fresh ? patientDisplayLabel(fresh) : (inits || m || 'unnamed')}`,
        'success',
      );
    } catch (e) {
      if (e instanceof ApiError) {
        setError(`Server rejected request (${e.status}): ${e.message}`);
      } else if (e instanceof TypeError) {
        setError('Cannot reach server. Is the backend running?');
      } else {
        setError(String(e));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && close()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <Dialog.Content
          className={cn(
            'fixed left-1/2 top-1/2 z-50 w-full max-w-md -translate-x-1/2 -translate-y-1/2',
            'rounded-lg border border-border-strong bg-surface p-6 shadow-2xl',
            'focus:outline-none',
          )}
        >
          <div className="mb-4 flex items-center justify-between">
            <Dialog.Title asChild>
              <h2 className="font-display text-section text-text-primary">
                New patient
              </h2>
            </Dialog.Title>
            <Dialog.Close
              aria-label="Close"
              className="rounded-sm p-1 text-text-tertiary hover:bg-accent-subtle hover:text-text-primary"
            >
              <X size={16} />
            </Dialog.Close>
          </div>

          <Dialog.Description className="mb-5 text-caption text-text-secondary">
            A PHI-safe hash is minted on the server. Enter either initials
            (e.g. <span className="font-mono">J.D.</span>) <strong>or</strong> an MRN —
            one of the two is required so the server has something to hash.
          </Dialog.Description>

          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="mb-1.5 block text-caption font-medium text-text-secondary">
                  Initials
                </label>
                <Input
                  value={initials}
                  onChange={(e) => setInitials(e.target.value)}
                  placeholder="J.D."
                  disabled={busy}
                />
              </div>
              <div>
                <label className="mb-1.5 block text-caption font-medium text-text-secondary">
                  MRN <span className="text-text-tertiary">(or initials)</span>
                </label>
                <Input
                  value={mrn}
                  onChange={(e) => setMrn(e.target.value)}
                  placeholder="MRN-12345"
                  disabled={busy}
                />
              </div>
            </div>

            <div>
              <label className="mb-1.5 block text-caption font-medium text-text-secondary">
                Sex
              </label>
              <div className="flex gap-2">
                {(['F', 'M'] as const).map((s) => (
                  <button
                    key={s}
                    onClick={() => setSex(s)}
                    className={cn(
                      'flex-1 rounded-sm border px-3 py-2 text-body',
                      sex === s
                        ? 'border-accent bg-accent-subtle text-accent'
                        : 'border-border text-text-secondary hover:border-border-strong',
                    )}
                  >
                    {s === 'F' ? 'Female' : 'Male'}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <label className="mb-1.5 block text-caption font-medium text-text-secondary">
                Age <span className="text-text-tertiary">(years — server buckets to age group)</span>
              </label>
              <Input
                value={age}
                onChange={(e) => setAge(e.target.value)}
                placeholder="65"
                inputMode="numeric"
                disabled={busy}
              />
            </div>

            <div>
              <label className="mb-1.5 block text-caption font-medium text-text-secondary">
                Reason for visit <span className="text-text-tertiary">(optional)</span>
              </label>
              <Input
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="Chest pain, follow-up CT, …"
                disabled={busy}
              />
            </div>

            {error && (
              <div className="rounded-sm border border-retract/40 bg-retract/10 px-3 py-2 text-caption text-retract">
                {error}
              </div>
            )}
          </div>

          <div className="mt-6 flex justify-end gap-2">
            <Button variant="subtle" onClick={close} disabled={busy}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={onCreate}
              disabled={busy || (!initials.trim() && !mrn.trim())}
            >
              {busy ? 'Creating…' : 'Create patient'}
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/* ───────────── AccountMenu (avatar popover) ───────────── */

export function AccountMenu({ trigger }: { trigger: ReactNode }) {
  const theme       = useAppState((s) => s.theme);
  const toggleTheme = useAppState((s) => s.toggleTheme);
  const logout      = useAppState((s) => s.logout);
  const openPractitioner = useAppState((s) => s.openPractitionerOverlay);
  const openSettings = useAppState((s) => s.openSettingsOverlay);
  const openCompose  = useAppState((s) => s.openEmailComposer);
  const displayName = useAppState((s) => s.displayName);

  return (
    <Popover.Root>
      <Popover.Trigger asChild>{trigger}</Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="end"
          sideOffset={8}
          className={cn(
            'z-40 w-56 rounded-md border border-border-strong bg-surface p-1.5',
            'shadow-xl focus:outline-none',
          )}
        >
          <MenuRow
            icon={<User size={14} />}
            label={displayName ?? 'Signed in'}
            hint="signed in"
          />
          <MenuDivider />
          <MenuRow
            icon={<SettingsIcon size={14} />}
            label="Settings · Data"
            onClick={openSettings}
          />
          <MenuRow
            icon={<Mail size={14} />}
            label="Compose email…"
            onClick={() => openCompose()}
          />
          <MenuRow
            icon={<User size={14} />}
            label="Nexus has learned"
            onClick={openPractitioner}
          />
          <MenuRow
            icon={theme === 'dark' ? <Sun size={14} /> : <Moon size={14} />}
            label={theme === 'dark' ? 'Light mode' : 'Dark mode'}
            onClick={toggleTheme}
          />
          <MenuDivider />
          <MenuRow
            icon={<LogOut size={14} />}
            label="Sign out"
            onClick={logout}
            destructive
          />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function MenuRow({
  icon, label, hint, onClick, destructive,
}: {
  icon: ReactNode;
  label: string;
  hint?: string;
  onClick?: () => void;
  destructive?: boolean;
}) {
  const interactive = !!onClick;
  return (
    <button
      onClick={onClick}
      disabled={!interactive}
      className={cn(
        'flex w-full items-center justify-between rounded-sm px-2.5 py-1.5 text-left text-body',
        interactive && 'hover:bg-accent-subtle',
        destructive && 'text-retract',
        !destructive && 'text-text-primary',
        !interactive && 'cursor-default text-text-secondary',
      )}
    >
      <span className="flex items-center gap-2">
        {icon}
        {label}
      </span>
      {hint && <span className="text-caption text-text-tertiary">{hint}</span>}
    </button>
  );
}

function MenuDivider() {
  return <div className="my-1 h-px bg-border" />;
}

/* ───────────── Toast strip ───────────── */

export function ToastStrip() {
  const toast = useAppState((s) => s.toast);
  const dismiss = useAppState((s) => s.dismissToast);

  if (!toast) return null;

  const variants = {
    info:    'border-border-strong text-text-primary',
    success: 'border-confirmed/60 text-confirmed',
    error:   'border-retract/60 text-retract',
  };

  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-40">
      <button
        onClick={dismiss}
        className={cn(
          'pointer-events-auto flex items-center gap-3 rounded-md border bg-surface px-4 py-2.5',
          'shadow-lg transition-opacity duration-150',
          variants[toast.kind],
        )}
      >
        <Plus
          size={14}
          className={cn(
            'shrink-0',
            toast.kind === 'error' && 'rotate-45',
          )}
        />
        <span className="text-body">{toast.text}</span>
      </button>
    </div>
  );
}

/* ───────────── EmailComposerDialog ─────────────
 *
 * Modal compose surface for outbound email. Opens when:
 *   - CommandPalette → "Compose email"
 *   - AccountMenu    → "Compose email…"
 *   - PatientMode    → "Email findings" button (pre-fills body with
 *                      the active patient's findings)
 *
 * Transport is probed via GET /api/v1/email/transport every time
 * the dialog opens — operator may have dropped fresh creds into
 * $RUNE_HOME/.env since the last sidecar boot. The Send button stays
 * disabled while ``configured === false`` so the medic doesn't type
 * a draft they can never send.
 *
 * Send dispatches POST /api/v1/email/send. The server returns
 * ``{ok, transport, message}``; ``message`` surfaces verbatim in the
 * status strip whether it succeeded or failed, so the relay's
 * "rate limit hit · 3 sends remaining tomorrow" type messages reach
 * the medic without UI rewriting.
 */
export function EmailComposerDialog() {
  const open    = useAppState((s) => s.emailComposerOpen);
  const close   = useAppState((s) => s.closeEmailComposer);
  const prefill = useAppState((s) => s.emailComposerPrefill);
  const toast   = useAppState((s) => s.showToast);

  const [to, setTo]           = useState('');
  const [cc, setCc]           = useState('');
  const [subject, setSubject] = useState('');
  const [body, setBody]       = useState('');
  const [sending, setSending] = useState(false);
  // Inline status — preferred over a toast for failures since the
  // medic is still looking at the form when the relay rejects.
  const [status, setStatus] = useState<
    { kind: 'idle' | 'ok' | 'error'; text: string }
  >({ kind: 'idle', text: '' });
  const [transport, setTransport] = useState<EmailTransportStatus | null>(null);
  const [probing, setProbing] = useState(false);

  // Reset / seed the form whenever the dialog opens. Also kick off
  // the transport probe so the Send button enables itself the moment
  // we've confirmed the server can actually deliver.
  useEffect(() => {
    if (!open) return;
    setTo(prefill?.to ?? '');
    setCc('');
    setSubject(prefill?.subject ?? '');
    setBody(prefill?.body ?? '');
    setStatus({ kind: 'idle', text: '' });
    setSending(false);

    setProbing(true);
    api.getEmailTransport().then(
      (s) => { setTransport(s); setProbing(false); },
      (e) => {
        setProbing(false);
        setStatus({
          kind: 'error',
          text: `Could not probe email transport: ${
            e instanceof Error ? e.message : String(e)
          }`,
        });
      },
    );
  }, [open, prefill]);

  const parseAddrs = (raw: string): string[] =>
    raw.split(',').map((s) => s.trim()).filter(Boolean);

  // Local validation — same rules the backend will apply, but cheaper
  // to surface here so the Send button reflects validity in real time
  // (rather than only after the POST returns 422).
  const toList = parseAddrs(to);
  const ccList = parseAddrs(cc);
  const looksOk = (a: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(a);
  const allAddrs = [...toList, ...ccList];
  const badAddrs = allAddrs.filter((a) => !looksOk(a));

  const canSend =
    !sending
    && !probing
    && (transport?.configured ?? false)
    && toList.length > 0
    && subject.trim().length > 0
    && body.trim().length > 0
    && badAddrs.length === 0;

  const handleSend = async () => {
    if (!canSend) return;
    setSending(true);
    setStatus({ kind: 'idle', text: '' });
    try {
      const r = await api.sendEmail({
        to: toList, cc: ccList, subject, body,
      });
      if (r.ok) {
        setStatus({ kind: 'ok', text: r.message });
        toast(`Email sent · ${r.sentTo.join(', ') || 'recipients confirmed'}`, 'success');
        // Auto-close after a beat so the medic sees the green strip
        // briefly. 1.2s matches Memory tab's confirm pattern.
        setTimeout(() => { if (!sending) close(); }, 1200);
      } else {
        // ok=false comes back on send-level failures (relay rejected,
        // SMTP auth bad, recipient blocked). Show inline; don't toast.
        setStatus({ kind: 'error', text: r.message });
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // 503 from POST = no transport configured — phrase clearly.
      if (msg.includes('503')) {
        setStatus({
          kind: 'error',
          text: (
            'No email transport is configured on this server. '
            + 'Set NEXUS_RELAY_URL + NEXUS_RELAY_API_KEY (recommended) '
            + 'or NEXUS_SMTP_HOST + NEXUS_SMTP_USER + NEXUS_SMTP_PASSWORD '
            + 'in $RUNE_HOME/.env, then retry.'
          ),
        });
      } else {
        setStatus({ kind: 'error', text: `Send failed: ${msg}` });
      }
    } finally {
      setSending(false);
    }
  };

  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && !sending && close()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/50" />
        <Dialog.Content
          className={cn(
            'fixed inset-x-0 top-0 z-50 mx-auto my-8 max-w-2xl',
            'rounded-lg border border-border-strong bg-surface p-6 shadow-2xl',
            'max-h-[90vh] overflow-y-auto focus:outline-none',
          )}
        >
          <div className="mb-4 flex items-start justify-between">
            <div>
              <Dialog.Title asChild>
                <h2 className="font-display text-section flex items-center gap-2">
                  <Mail size={18} /> Compose email
                </h2>
              </Dialog.Title>
              <Dialog.Description className="mt-1 text-caption text-text-secondary">
                Sends through your configured transport — relay first if
                set, otherwise direct SMTP.
              </Dialog.Description>
            </div>
            <Dialog.Close
              className="rounded-sm p-1 text-text-tertiary hover:bg-accent-subtle"
              disabled={sending}
            >
              <X size={16} />
            </Dialog.Close>
          </div>

          {/* Transport banner — only when there's something to say. */}
          {probing && (
            <div className="mb-4 rounded-sm border border-border bg-bg px-3 py-2 text-caption text-text-secondary">
              Checking transport configuration…
            </div>
          )}
          {!probing && transport && !transport.configured && (
            <div className="mb-4 flex items-start gap-2 rounded-sm border border-caution/40 bg-caution/10 px-3 py-2 text-caption text-caution">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" />
              <div>
                No email transport is configured. Configure
                {' '}<code className="font-mono">NEXUS_RELAY_URL</code>{' '}
                +
                {' '}<code className="font-mono">NEXUS_RELAY_API_KEY</code>{' '}
                (recommended) or
                {' '}<code className="font-mono">NEXUS_SMTP_*</code>{' '}
                in
                {' '}<code className="font-mono">$RUNE_HOME/.env</code>,
                then reopen.
              </div>
            </div>
          )}
          {!probing && transport?.configured && (
            <div className="mb-4 rounded-sm border border-border bg-bg px-3 py-2 text-caption text-text-secondary">
              Sending via{' '}
              <strong className="text-text-primary">
                {transport.relayConfigured
                  ? `relay (${transport.relayUrlHost || '?'})`
                  : 'direct SMTP'}
              </strong>
              {transport.defaultFrom && (
                <>
                  {' · from '}
                  <span className="font-mono">{transport.defaultFrom}</span>
                </>
              )}
              {transport.allowedRecipients.length > 0 && (
                <div className="mt-1">
                  Allow-list active · {transport.allowedRecipients.length} recipient(s)
                </div>
              )}
            </div>
          )}

          <div className="space-y-4">
            <div>
              <label className="mb-1.5 block text-caption font-medium text-text-secondary">
                To <span className="text-text-tertiary">(comma-separated)</span>
              </label>
              <Input
                value={to}
                onChange={(e) => setTo(e.target.value)}
                placeholder="colleague@hospital.org"
                disabled={sending}
              />
            </div>

            <div>
              <label className="mb-1.5 block text-caption font-medium text-text-secondary">
                Cc <span className="text-text-tertiary">(optional)</span>
              </label>
              <Input
                value={cc}
                onChange={(e) => setCc(e.target.value)}
                placeholder=""
                disabled={sending}
              />
            </div>

            <div>
              <label className="mb-1.5 block text-caption font-medium text-text-secondary">
                Subject
              </label>
              <Input
                value={subject}
                onChange={(e) => setSubject(e.target.value)}
                placeholder="CT findings · Mr Patel"
                disabled={sending}
              />
            </div>

            <div>
              <label className="mb-1.5 block text-caption font-medium text-text-secondary">
                Body
              </label>
              <textarea
                value={body}
                onChange={(e) => setBody(e.target.value)}
                placeholder="…"
                disabled={sending}
                rows={10}
                className={cn(
                  'w-full resize-y rounded-sm border border-border bg-bg px-3 py-2',
                  'font-mono text-body text-text-primary placeholder:text-text-tertiary',
                  'focus:border-accent focus:outline-none',
                  'disabled:cursor-not-allowed disabled:opacity-60',
                )}
              />
            </div>

            {badAddrs.length > 0 && (
              <div className="rounded-sm border border-caution/40 bg-caution/10 px-3 py-2 text-caption text-caution">
                Not a valid email address: {badAddrs.join(', ')}
              </div>
            )}

            {status.kind === 'error' && (
              <div className="flex items-start gap-2 rounded-sm border border-retract/40 bg-retract/10 px-3 py-2 text-caption text-retract">
                <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                <div className="whitespace-pre-wrap">{status.text}</div>
              </div>
            )}
            {status.kind === 'ok' && (
              <div className="flex items-start gap-2 rounded-sm border border-confirmed/40 bg-confirmed/10 px-3 py-2 text-caption text-confirmed">
                <CheckCircle size={14} className="mt-0.5 shrink-0" />
                <div>{status.text}</div>
              </div>
            )}
          </div>

          <div className="mt-6 flex items-center justify-between">
            <div className="text-caption text-text-tertiary">
              {toList.length === 0
                ? 'Enter at least one recipient'
                : `${toList.length} recipient${toList.length === 1 ? '' : 's'}${
                    ccList.length ? ` + ${ccList.length} cc` : ''
                  }`}
            </div>
            <div className="flex gap-2">
              <Button variant="subtle" onClick={close} disabled={sending}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleSend}
                disabled={!canSend}
              >
                <Send size={14} /> {sending ? 'Sending…' : 'Send'}
              </Button>
            </div>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
