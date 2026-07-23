import { useEffect, useState } from 'react';
import { BookOpen, Edit3, RotateCcw, AlertTriangle } from 'lucide-react';
import { AppShell } from '@/components/layout/AppShell';
import { api } from '@/lib/api-client';
import { Alert, Button, Card, Skeleton, Badge, Textarea, Input } from '@/components/ui';
import { cn } from '@/lib/utils';

interface Article {
  id: string;
  title: string;
  content: string;
  sources: string[];
  version: number;
  status: 'current' | 'stale';
  staleBecause?: string[];
  createdAt: number;
  updatedAt: number;
}

export function KnowledgePage() {
  const [articles, setArticles] = useState<Article[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState('');
  const [editContent, setEditContent] = useState('');

  const loadArticles = async () => {
    setLoading(true);
    try {
      // Knowledge API not yet exposed on backend; use stubbed data for now
      const r = await fetch('/api/v1/docs', { headers: { Authorization: `Bearer ${api.getToken()}` } }).then(r => r.json());
      const docs = r.docs || [];
      setArticles(docs.map((d: any) => ({
        id: d.id,
        title: d.title || 'Untitled',
        content: '',
        sources: [],
        version: 1,
        status: 'current' as const,
        createdAt: new Date(d.created_at).getTime(),
        updatedAt: new Date(d.updated_at).getTime(),
      })));
    } catch {
      setError('Knowledge store not yet activated on this server');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadArticles(); }, []);

  const staleCount = articles.filter(a => a.status === 'stale').length;

  return (
    <AppShell>
      <div className="flex h-full flex-col overflow-y-auto">
        <header className="flex h-14 items-center justify-between border-b border-border bg-surface px-6">
          <div className="flex items-center gap-3">
            <BookOpen size={20} className="text-accent" />
            <h1 className="font-semibold text-text-primary">Knowledge Base</h1>
            {staleCount > 0 && (
              <Badge variant="warning">
                <AlertTriangle size={12} className="mr-1" /> {staleCount} stale
              </Badge>
            )}
          </div>
        </header>

        <main className="p-6">
          {error && <Alert variant="error" className="mb-4">{error}</Alert>}

          {loading ? (
            <div className="space-y-4">
              <Skeleton className="h-24 w-full rounded-xl" />
              <Skeleton className="h-24 w-full rounded-xl" />
            </div>
          ) : articles.length === 0 ? (
            <Card className="p-8 text-center">
              <BookOpen size={32} className="mx-auto mb-3 text-text-tertiary" />
              <p className="text-text-secondary">No knowledge articles yet.</p>
              <p className="mt-1 text-sm text-text-tertiary">Articles are auto-generated when 3+ related facts accumulate.</p>
            </Card>
          ) : (
            <div className="space-y-4">
              {articles.map((a) => (
                <Card key={a.id} className={cn('p-4', a.status === 'stale' && 'border-warning/50')}>
                  <div className="flex items-start justify-between">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <h3 className="font-medium text-text-primary truncate">{a.title}</h3>
                        <Badge variant="default">v{a.version}</Badge>
                        {a.status === 'stale' && (
                          <Badge variant="warning">
                            <AlertTriangle size={10} className="mr-1" /> Stale
                          </Badge>
                        )}
                      </div>
                      <p className="mt-1 text-xs text-text-tertiary">
                        {new Date(a.updatedAt).toLocaleDateString()}
                        {a.sources.length > 0 && ` · ${a.sources.length} sources`}
                      </p>
                      {a.status === 'stale' && a.staleBecause && (
                        <p className="mt-1 text-xs text-warning">
                          Dependent facts updated: {a.staleBecause.join(', ')}
                        </p>
                      )}
                    </div>
                    <div className="ml-3 flex gap-1">
                      {a.status === 'stale' && (
                        <Button size="sm" variant="secondary">
                          <RotateCcw size={14} className="mr-1" /> Regenerate
                        </Button>
                      )}
                      {editingId === a.id ? (
                        <Button size="sm" variant="ghost" onClick={() => setEditingId(null)}>
                          Done
                        </Button>
                      ) : (
                        <Button size="sm" variant="ghost" onClick={() => { setEditingId(a.id); setEditTitle(a.title); setEditContent(a.content); }}>
                          <Edit3 size={14} />
                        </Button>
                      )}
                    </div>
                  </div>
                  {editingId === a.id && (
                    <div className="mt-3 space-y-3">
                      <Input value={editTitle} onChange={e => setEditTitle(e.target.value)} placeholder="Title" />
                      <Textarea value={editContent} onChange={e => setEditContent(e.target.value)} rows={4} />
                      <Button size="sm" onClick={() => setEditingId(null)}>Save</Button>
                    </div>
                  )}
                </Card>
              ))}
            </div>
          )}
        </main>
      </div>
    </AppShell>
  );
}
