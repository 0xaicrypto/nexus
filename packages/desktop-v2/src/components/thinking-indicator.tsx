/**
 * ThinkingIndicator — shared "AI is working" UI for every chat surface.
 *
 * Before this component existed, the four chat surfaces (CrossPatient
 * widget / PatientMode encounter / Research per-study ChatTab /
 * CrossResearch) each rendered a different tiny placeholder:
 *
 *   - "…" (one char, easy to miss)
 *   - "思考中…" (small grey text, no animation)
 *   - "正在跨患者检索 + 思考…" (long, no animation)
 *
 * The medic couldn't reliably tell whether the AI was working, stuck,
 * or already done. This component is the canonical answer:
 *
 *   - Three pulsing dots (CSS keyframes, not GPU-heavy)
 *   - Optional label that names the current activity
 *   - Tone variant so it sits well inside both rw-* and base palette
 *     bubbles
 *
 * Reuse from any chat that needs to say "wait, I'm working" — the
 * thinking transcript ("Searching tavily…", "Searching the patient
 * record…") still belongs in ReasoningPane; this is just the
 * "something is happening" affordance.
 */

interface ThinkingIndicatorProps {
  /** Free-form label shown next to the dots. Defaults to "正在思考". */
  label?: string;
  /** ``rw`` matches Research Workspace palette (cyan accent);
   *  ``base`` uses the desktop's default surface tokens. */
  tone?: 'rw' | 'base';
}

export function ThinkingIndicator({
  label = '正在思考',
  tone = 'rw',
}: ThinkingIndicatorProps) {
  const dotColor = tone === 'rw'
    ? 'bg-rw-accent'
    : 'bg-accent';
  const textColor = tone === 'rw'
    ? 'text-rw-t2'
    : 'text-text-secondary';
  return (
    <span
      role="status"
      aria-live="polite"
      aria-label={`${label}…`}
      className={`inline-flex items-center gap-2 text-[12px] ${textColor}`}
    >
      <span className="inline-flex items-end gap-[3px] h-3">
        <span
          className={`block w-1.5 h-1.5 rounded-full ${dotColor} animate-thinking-bounce`}
          style={{ animationDelay: '0ms' }}
        />
        <span
          className={`block w-1.5 h-1.5 rounded-full ${dotColor} animate-thinking-bounce`}
          style={{ animationDelay: '150ms' }}
        />
        <span
          className={`block w-1.5 h-1.5 rounded-full ${dotColor} animate-thinking-bounce`}
          style={{ animationDelay: '300ms' }}
        />
      </span>
      <span>{label}…</span>
    </span>
  );
}


/**
 * StreamingFooter — unified "AI is still writing" affordance for
 * every chat surface.
 *
 * Before this existed, every bubble showed ThinkingIndicator ONLY
 * when ``m.text`` was empty. The moment the first chunk arrived,
 * the indicator vanished — but the LLM kept streaming for another
 * 5-15 seconds (reasoning steps, citations, additional paragraphs).
 * The medic was left wondering "is it done?".
 *
 * This footer renders BELOW the message body for the ENTIRE duration
 * of streaming, so there's always one visible signal. Label changes
 * meaningfully:
 *   - text empty                → "正在思考"   (initial reasoning)
 *   - text present + streaming  → "继续生成"   (mid-write)
 *   - streaming==false          → nothing      (clean DONE state)
 */
interface StreamingFooterProps {
  streaming?: boolean;
  hasText?: boolean;
  tone?: 'rw' | 'base';
  /** Override the auto-label (e.g. "正在跨患者检索 + 思考"). */
  label?: string;
}

export function StreamingFooter({
  streaming, hasText, tone = 'base', label,
}: StreamingFooterProps) {
  if (!streaming) return null;
  const autoLabel = hasText ? '继续生成' : '正在思考';
  return (
    <div className="mt-1.5">
      <ThinkingIndicator tone={tone} label={label ?? autoLabel} />
    </div>
  );
}


/** Inline blinking cursor at end of streaming text. Tone matches
 *  the surface (rw cyan / base accent). Use right after ChatMarkdown
 *  while streaming so the medic sees characters actively arriving. */
export function StreamingCursor({ tone = 'base' }: { tone?: 'rw' | 'base' }) {
  const color = tone === 'rw' ? 'bg-rw-accent' : 'bg-accent';
  return (
    <span
      className={`ml-0.5 inline-block w-[2px] h-[1.1em] align-text-bottom ${color} animate-pulse`}
      aria-hidden
    />
  );
}
