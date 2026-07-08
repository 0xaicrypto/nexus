/**
 * ChatMarkdown — shared markdown renderer for every chat surface
 * (Patient encounter / Research Chat / Cross-Research / inline answer
 * widgets).
 *
 * Before this existed, every chat bubble rendered the LLM's reply with
 * ``whitespace-pre-wrap`` and a raw ``{text}`` interpolation — so a
 * response like ``**Plan**\n\n* 化疗`` showed up as literal asterisks
 * instead of bold + bullet. LLMs default to Markdown; the UI was
 * eating it.
 *
 * Why custom rather than naked `<ReactMarkdown>`:
 *   - We Tailwind-style every block element so it inherits the chat
 *     bubble's text colour + spacing rules (no `prose` plugin).
 *   - Links open in the system browser via Tauri shell.open() so the
 *     in-app webview doesn't end up navigating away from Nexus.
 *   - We disable raw HTML by NOT enabling rehype-raw — defence against
 *     a hostile model pasting `<script>` into a turn.
 */
import { open as shellOpen } from '@tauri-apps/plugin-shell';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Fragment, useState, type ReactNode, isValidElement, cloneElement } from 'react';

/** F-unified-chat-files — minimal file-chip metadata used by ChatMarkdown
 *  to inflate `[F1]` tokens that the LLM emits in answers into a
 *  clickable inline chip. The parent chat surface composes this map
 *  from its `useChatFiles` hook and passes it down. */
export interface FileChipRef {
  fileId: string;
  name: string;
  textExtractionStatus: string;
}

interface ChatMarkdownProps {
  text: string;
  /** Tone variant — default is for agent bubbles (dark surface, light
   *  text); ``inverse`` is for user bubbles (accent fill, dark text). */
  tone?: 'agent' | 'inverse';
  /** Map of "F1" / "F2" / ... → file metadata for inline chip
   *  rendering. Pass undefined to disable chip transform (renders the
   *  literal "[F1]" string). */
  fileMap?: Record<string, FileChipRef>;
}

// Match [F1], [F2], etc. Use \b boundaries to avoid eating [F1].5 or
// the like. Capture the index for lookup.
const F_CHIP_RE = /\[F(\d+)\]/g;

/** Walk a ReactNode tree, replacing inline `[Fn]` tokens in text
 *  nodes with chip buttons drawn from the fileMap. Anything that's
 *  not a plain string is returned untouched (recursing into element
 *  children handles `<strong>[F1]</strong>` style cases). */
function inflateChipsInChildren(
  children: ReactNode,
  fileMap: Record<string, FileChipRef> | undefined,
  onChipClick: (token: string, ref: FileChipRef | undefined) => void,
): ReactNode {
  if (!fileMap || Object.keys(fileMap).length === 0) return children;

  const transform = (node: ReactNode, keyPrefix: string): ReactNode => {
    if (typeof node === 'string') {
      // Fast path: no token in this fragment.
      if (!node.includes('[F')) return node;
      const out: ReactNode[] = [];
      let last = 0;
      let m: RegExpExecArray | null;
      F_CHIP_RE.lastIndex = 0;
      while ((m = F_CHIP_RE.exec(node)) !== null) {
        if (m.index > last) out.push(node.slice(last, m.index));
        const token = `F${m[1]}`;
        const ref = fileMap[token];
        out.push(
          <FileChipInline
            key={`${keyPrefix}-${m.index}`}
            token={token}
            ref={ref}
            onClick={() => onChipClick(token, ref)}
          />,
        );
        last = m.index + m[0].length;
      }
      if (last === 0) return node;
      if (last < node.length) out.push(node.slice(last));
      return <Fragment>{out}</Fragment>;
    }
    if (Array.isArray(node)) {
      return node.map((c, i) => (
        <Fragment key={`${keyPrefix}-${i}`}>{transform(c, `${keyPrefix}-${i}`)}</Fragment>
      ));
    }
    if (isValidElement(node)) {
      // Recurse into children of inline elements (strong / em / a / li / etc.)
      const childProps = node.props as { children?: ReactNode };
      if (childProps.children !== undefined) {
        return cloneElement(
          node,
          undefined,
          transform(childProps.children, `${keyPrefix}c`),
        );
      }
    }
    return node;
  };

  return transform(children, 'c');
}

/** Inline pill rendered in place of "[F1]" inside an agent reply.
 *  Status badge mirrors ChatFileLibDrawer / ChipStrip. Click opens
 *  a tiny modal with file metadata + a download link. */
function FileChipInline({
  token, ref, onClick,
}: { token: string; ref: FileChipRef | undefined; onClick: () => void }) {
  if (!ref) {
    // LLM cited a token we don't have a record for — defensive
    // surface so the medic immediately notices, instead of silently
    // showing a fake-looking [F99] in text.
    return (
      <span
        title={`引用了不存在的文件 ${token} — 模型可能产生了幻觉`}
        className="rounded-sm border border-retract/40 bg-retract/10 px-1 text-[11px] text-retract"
      >
        ⚠ {token}
      </span>
    );
  }
  const badge = STATUS_GLYPH[ref.textExtractionStatus];
  return (
    <button
      type="button"
      onClick={onClick}
      title={`${ref.name} — ${ref.textExtractionStatus}`}
      className="inline-flex items-center gap-0.5 align-baseline rounded-sm border border-accent/40 bg-accent-subtle/40 px-1 py-px text-[11px] text-accent hover:bg-accent-subtle/70"
    >
      {token}
      {badge && <span className="opacity-70">{badge}</span>}
    </button>
  );
}

const STATUS_GLYPH: Record<string, string> = {
  pending: '⏳',
  vision_ocr: '🤖',
  encrypted: '🔒',
  unreadable: '⚠',
  // text_layer: '' — happy path stays empty
};

export function ChatMarkdown({ text, tone = 'agent', fileMap }: ChatMarkdownProps) {
  // For an empty / falsy text just render nothing — the parent
  // bubble usually has a "thinking…" placeholder for that case.
  if (!text) return null;

  const linkColor = tone === 'inverse'
    ? 'underline decoration-current/60 hover:decoration-current'
    : 'text-rw-accent underline decoration-rw-accent/40 hover:decoration-rw-accent';

  // F-unified-chat-files — modal state for clicked file chip.
  const [activeChip, setActiveChip] = useState<{
    token: string; ref: FileChipRef | undefined;
  } | null>(null);
  const onChipClick = (token: string, ref: FileChipRef | undefined) => {
    setActiveChip({ token, ref });
  };
  const inflate = (children: ReactNode): ReactNode =>
    inflateChipsInChildren(children, fileMap, onChipClick);

  return (
    <div className="chat-md text-[14px] leading-relaxed">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        // No `rehype-raw` ⇒ raw <script>/<iframe> in the LLM reply is
        // rendered as escaped text rather than executed. This is a
        // security boundary, not just polish.
        components={{
          // Block-level overrides — give each element the chat's
          // line-height + spacing instead of the browser defaults.
          p: ({ children }) => (
            <p className="my-2 first:mt-0 last:mb-0 whitespace-pre-wrap">
              {inflate(children)}
            </p>
          ),
          ul: ({ children }) => (
            <ul className="my-2 ml-5 list-disc space-y-1">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="my-2 ml-5 list-decimal space-y-1">{children}</ol>
          ),
          li: ({ children }) => <li className="pl-1">{inflate(children)}</li>,
          h1: ({ children }) => (
            <h1 className="mt-3 mb-1 text-[16px] font-semibold">{children}</h1>
          ),
          h2: ({ children }) => (
            <h2 className="mt-3 mb-1 text-[15px] font-semibold">{children}</h2>
          ),
          h3: ({ children }) => (
            <h3 className="mt-2 mb-1 text-[14px] font-semibold">{children}</h3>
          ),
          blockquote: ({ children }) => (
            <blockquote className="my-2 border-l-2 border-current/30 pl-3 opacity-80">
              {children}
            </blockquote>
          ),
          // Inline overrides.
          // NOTE: strong/em are overridden again below with chip-
          // inflation; the duplicate at this position would shadow
          // them. We leave only `code` here.
          code: ({ className, children, ...rest }) => {
            const isBlock = /^language-/.test(className || '')
              || String(children).includes('\n');
            if (isBlock) {
              return (
                <pre className="my-2 overflow-x-auto rounded-md
                                bg-black/30 p-2 text-[12px] font-mono">
                  <code className={className} {...rest}>{children}</code>
                </pre>
              );
            }
            return (
              <code className="rounded bg-black/30 px-1 py-0.5 text-[12px] font-mono">
                {children}
              </code>
            );
          },
          a: ({ href, children }) => (
            <a
              href={href}
              onClick={(e) => {
                // Route external URLs through Tauri's shell.open so the
                // system browser handles them — `<a>` clicks inside the
                // WebView would otherwise navigate the whole app to
                // pubmed.ncbi.nlm.nih.gov and lose state.
                if (href && /^https?:/i.test(href)) {
                  e.preventDefault();
                  void shellOpen(href).catch(() => {
                    // Fallback to default behaviour if Tauri shell isn't
                    // available (e.g. running `pnpm dev` in the browser).
                    window.open(href, '_blank', 'noopener,noreferrer');
                  });
                }
              }}
              className={linkColor}
            >
              {children as ReactNode}
            </a>
          ),
          table: ({ children }) => (
            <table className="my-2 border-collapse text-[12px]">{children}</table>
          ),
          th: ({ children }) => (
            <th className="border border-current/30 px-2 py-1 text-left font-semibold">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="border border-current/20 px-2 py-1">{inflate(children)}</td>
          ),
          hr: () => <hr className="my-3 border-current/20"/>,
          strong: ({ children }) => <strong className="font-semibold">{inflate(children)}</strong>,
          em: ({ children }) => <em className="italic">{inflate(children)}</em>,
        }}
      >
        {text}
      </ReactMarkdown>
      {activeChip && (
        <FileChipModal
          chip={activeChip}
          onClose={() => setActiveChip(null)}
        />
      )}
    </div>
  );
}


/** Small modal shown when the medic clicks a `[Fn]` citation chip
 *  inside an agent reply. Goal: confirm the file's identity + give a
 *  one-click "open" path. Renders metadata + extraction status; the
 *  full file viewer integration is a follow-up. */
function FileChipModal({
  chip, onClose,
}: {
  chip: { token: string; ref: FileChipRef | undefined };
  onClose: () => void;
}) {
  const ref = chip.ref;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
    >
      <div
        className="w-[420px] max-w-[90vw] rounded-md bg-surface-1 border border-border p-4 text-sm shadow-xl text-text-primary"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-baseline justify-between mb-2">
          <div>
            <div className="font-mono text-[12px] text-text-tertiary">{chip.token}</div>
            {ref ? (
              <div className="text-base font-semibold mt-0.5 break-words">
                {ref.name}
              </div>
            ) : (
              <div className="text-base font-semibold mt-0.5 text-retract">
                引用了不存在的文件
              </div>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-xl leading-none opacity-60 hover:opacity-100"
          >×</button>
        </div>
        {ref ? (
          <div className="space-y-1 text-[12px] text-text-secondary">
            <div>提取状态: <code className="text-[11px]">{ref.textExtractionStatus}</code></div>
            {ref.textExtractionStatus === 'vision_ocr' && (
              <div className="text-caution">
                ⚠ 内容由 AI 视觉模型识别,医疗决策请核对原文。
              </div>
            )}
            {ref.textExtractionStatus === 'unreadable' && (
              <div className="text-retract">
                ⚠ 此文件未能提取文本。模型不可能"读过"该文件 ——
                若 AI 仍引用本文件,可能在产生幻觉。
              </div>
            )}
            <div className="pt-2 text-[11px] text-text-tertiary">
              file_id: <code>{ref.fileId.slice(0, 16)}…</code>
            </div>
          </div>
        ) : (
          <div className="text-[12px] text-retract leading-relaxed">
            模型在回答里引用了 <code>{chip.token}</code>,
            但当前文件库里没有对应文件。这是一个幻觉信号 ——
            请告诉医生重新核对引用。
          </div>
        )}
      </div>
    </div>
  );
}
