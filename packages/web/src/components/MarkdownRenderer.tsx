import { cn } from '@/lib/utils';

interface Props {
  content: string;
  className?: string;
}

export function MarkdownRenderer({ content, className }: Props) {
  if (!content) return null;

  const parts = content.split(/(```[\s\S]*?```)/g);

  return (
    <div className={cn('prose prose-sm dark:prose-invert max-w-none break-words', className)}>
      {parts.map((part, i) => {
        if (part.startsWith('```') && part.endsWith('```')) {
          const langEnd = part.indexOf('\n');
          const lang = part.slice(3, langEnd > 0 ? langEnd : part.length).trim();
          const code = part.slice(langEnd > 0 ? langEnd + 1 : 3, -3).trim();
          return (
            <pre key={i} className="my-2 overflow-x-auto rounded-lg bg-surface p-3 text-sm">
              {lang && <div className="mb-1 text-xs text-text-tertiary">{lang}</div>}
              <code className="text-text-primary whitespace-pre-wrap font-mono">{code}</code>
            </pre>
          );
        }
        return <div key={i} className="whitespace-pre-wrap" dangerouslySetInnerHTML={{ __html: renderInline(part) }} />;
      })}
    </div>
  );
}

function renderInline(text: string): string {
  let html = text;
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong class="font-bold">$1</strong>');
  html = html.replace(/\*([^*\n]+?)\*/g, '<em>$1</em>');
  html = html.replace(/`([^`]+)`/g, '<code class="rounded bg-surface px-1 py-0.5 text-sm font-mono">$1</code>');
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" class="text-accent underline">$1</a>');
  html = html.replace(/^### (.+)$/gm, '<h4 class="font-semibold text-sm mt-3 mb-1">$1</h4>');
  html = html.replace(/^## (.+)$/gm, '<h3 class="font-semibold mt-3 mb-1">$1</h3>');
  html = html.replace(/^# (.+)$/gm, '<h2 class="font-bold text-lg mt-4 mb-2">$1</h2>');
  html = html.replace(/^- (.+)$/gm, '<li class="ml-4">$1</li>');

  return html;
}
