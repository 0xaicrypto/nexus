import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, Link } from 'react-router-dom';
import { Plus, FileText } from 'lucide-react';
import { AppShell } from '@/components/layout/AppShell';
import { Alert, Button, Input, Card, Skeleton } from '@/components/ui';
import { api, ApiError } from '@/lib/api-client';

interface Doc {
  id: string;
  title: string;
  updated_at: string;
  ref_count: number;
}

export function WritingPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [docs, setDocs] = useState<Doc[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [creating, setCreating] = useState(false);

  const loadDocs = () => {
    setLoading(true);
    setError(null);
    api.listDocs()
      .then((r) => setDocs(r.docs))
      .catch((err) => setError(err instanceof ApiError ? err.messageText : String(err)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadDocs();
  }, []);

  const handleCreate = async () => {
    if (!newTitle.trim()) return;
    setCreating(true);
    try {
      const doc = await api.createDoc(newTitle.trim());
      setNewTitle('');
      setShowForm(false);
      navigate(`/app/writing/${doc.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setCreating(false);
    }
  };

  return (
    <AppShell>
      <div className="flex h-full flex-col">
        <header className="flex h-14 items-center justify-between border-b border-border bg-surface px-6">
          <h1 className="font-semibold text-text-primary">{t('writing.title', 'Writing Studio')}</h1>
          <Button size="sm" onClick={() => setShowForm((v) => !v)}>
            <Plus size={16} className="mr-1" /> {t('writing.newDoc', 'New Document')}
          </Button>
        </header>

        {showForm && (
          <div className="border-b border-border bg-surface-elevated px-6 py-4">
            <div className="flex items-end gap-3">
              <div className="flex-1">
                <Input
                  value={newTitle}
                  onChange={(e) => setNewTitle(e.target.value)}
                  placeholder={t('writing.docTitle', 'Document title')}
                  onKeyDown={(e) => { if (e.key === 'Enter') handleCreate(); }}
                />
              </div>
              <Button onClick={handleCreate} disabled={!newTitle.trim() || creating} isLoading={creating}>
                {t('common.create', 'Create')}
              </Button>
            </div>
          </div>
        )}

        {error && (
          <div className="px-6 pt-4">
            <Alert variant="error">{error}</Alert>
          </div>
        )}

        <main className="flex-1 overflow-y-auto p-6">
          {loading ? (
            <div className="space-y-3">
              <Skeleton className="h-14 w-full rounded-xl" />
              <Skeleton className="h-14 w-full rounded-xl" />
              <Skeleton className="h-14 w-full rounded-xl" />
            </div>
          ) : docs.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-20 text-center">
              <FileText size={40} className="mb-3 text-text-tertiary" />
              <p className="text-lg text-text-tertiary">{t('writing.noDocs', 'No documents yet')}</p>
              <p className="text-sm text-text-tertiary">{t('writing.createFirst', 'Create your first document')}</p>
            </div>
          ) : (
            <div className="space-y-2">
              {docs.map((d) => (
                <Link
                  key={d.id}
                  to={`/app/writing/${d.id}`}
                  className="block rounded-xl transition-colors hover:bg-surface"
                >
                  <Card className="p-4">
                    <div className="flex items-center justify-between">
                      <div>
                        <h3 className="font-medium text-text-primary">{d.title || t('writing.untitled', 'Untitled')}</h3>
                        <p className="text-xs text-text-tertiary">
                          {new Date(d.updated_at).toLocaleDateString()}
                          {d.ref_count > 0 ? ` · ${d.ref_count} ${t('writing.refs', 'references')}` : ''}
                        </p>
                      </div>
                      <FileText size={16} className="text-text-tertiary" />
                    </div>
                  </Card>
                </Link>
              ))}
            </div>
          )}
        </main>
      </div>
    </AppShell>
  );
}
