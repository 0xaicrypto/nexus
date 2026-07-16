import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, FileText, History, RotateCcw } from 'lucide-react';
import { AppShell } from '@/components/layout/AppShell';
import { Alert, Button, Card, Skeleton, Textarea } from '@/components/ui';
import { api, ApiError } from '@/lib/api-client';

interface DocDetail {
  id: string;
  title: string;
  body: string;
  created_at: string;
  updated_at: string;
}

interface SnapshotEntry {
  snapshot_id: string;
  created_at: string;
  body_preview: string;
}

export function WritingEditorPage() {
  const { docId } = useParams<{ docId: string }>();
  const navigate = useNavigate();
  const [doc, setDoc] = useState<DocDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [saving, setSaving] = useState(false);
  const bodyRef = useRef<HTMLTextAreaElement>(null);

  const [showHistory, setShowHistory] = useState(false);
  const [snapshots, setSnapshots] = useState<SnapshotEntry[]>([]);
  const [snapshotsLoading, setSnapshotsLoading] = useState(false);
  const [restoring, setRestoring] = useState<string | null>(null);

  useEffect(() => {
    if (!docId) return;
    setLoading(true);
    setError(null);
    api.getDoc(docId)
      .then((d) => {
        setDoc(d);
        setTitle(d.title);
        setBody(d.body);
      })
      .catch((err) => setError(err instanceof ApiError ? err.messageText : String(err)))
      .finally(() => setLoading(false));
  }, [docId]);

  useEffect(() => {
    if (bodyRef.current) {
      bodyRef.current.style.height = 'auto';
      bodyRef.current.style.height = `${bodyRef.current.scrollHeight}px`;
    }
  }, [body]);

  const loadSnapshots = useCallback(() => {
    if (!docId) return;
    setSnapshotsLoading(true);
    api.getDocSnapshots(docId)
      .then((r) => setSnapshots(r.snapshots))
      .catch(() => {})
      .finally(() => setSnapshotsLoading(false));
  }, [docId]);

  const handleToggleHistory = () => {
    const next = !showHistory;
    setShowHistory(next);
    if (next) loadSnapshots();
  };

  const handleSave = async () => {
    if (!docId) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await api.updateDoc(docId, { title, body });
      setDoc((prev) => prev ? { ...prev, title: updated.title, body: updated.body, updated_at: updated.updated_at } : prev);
      setTitle(updated.title);
      setBody(updated.body);
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setSaving(false);
    }
  };

  const handleRestore = async (snapshotId: string) => {
    if (!docId) return;
    setRestoring(snapshotId);
    try {
      const restored = await api.restoreSnapshot(docId, snapshotId);
      setBody(restored.body);
      setDoc((prev) => prev ? { ...prev, body: restored.body, updated_at: new Date().toISOString() } : prev);
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setRestoring(null);
    }
  };

  if (loading) {
    return (
      <AppShell>
        <div className="flex h-full flex-col">
          <div className="flex h-14 items-center border-b border-border bg-surface px-6 gap-3">
            <Skeleton className="h-5 w-5" />
            <Skeleton className="h-5 w-48" />
          </div>
          <div className="p-6 space-y-4">
            <Skeleton className="h-8 w-64" />
            <Skeleton className="h-64 w-full rounded-xl" />
          </div>
        </div>
      </AppShell>
    );
  }

  if (error && !doc) {
    return (
      <AppShell>
        <div className="flex h-full flex-col">
          <div className="flex h-14 items-center border-b border-border bg-surface px-6">
            <Button variant="ghost" size="sm" onClick={() => navigate('/app/writing')}>
              <ArrowLeft size={16} className="mr-1" /> Back
            </Button>
          </div>
          <div className="p-6">
            <Alert variant="error">{error}</Alert>
          </div>
        </div>
      </AppShell>
    );
  }

  if (!doc) {
    return (
      <AppShell>
        <div className="flex h-full flex-col">
          <div className="flex h-14 items-center border-b border-border bg-surface px-6">
            <Button variant="ghost" size="sm" onClick={() => navigate('/app/writing')}>
              <ArrowLeft size={16} className="mr-1" /> Back
            </Button>
          </div>
          <div className="flex flex-1 items-center justify-center">
            <p className="text-text-tertiary">Document not found</p>
          </div>
        </div>
      </AppShell>
    );
  }

  return (
    <AppShell>
      <div className="flex h-full flex-col overflow-y-auto">
        <header className="flex h-14 items-center gap-3 border-b border-border bg-surface px-6">
          <Button variant="ghost" size="sm" onClick={() => navigate('/app/writing')}>
            <ArrowLeft size={16} />
          </Button>
          <FileText size={18} className="text-text-tertiary" />
          <h1 className="font-semibold text-text-primary">{doc.title || 'Untitled'}</h1>
          <div className="ml-auto flex items-center gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={handleToggleHistory}
            >
              <History size={14} className="mr-1" /> History
            </Button>
            <Button size="sm" onClick={handleSave} isLoading={saving} disabled={saving}>
              Save
            </Button>
          </div>
        </header>

        <main className="flex-1 overflow-y-auto p-6">
          {error && (
            <div className="mb-4 max-w-3xl mx-auto">
              <Alert variant="error">{error}</Alert>
            </div>
          )}

          <div className="mx-auto max-w-3xl space-y-4">
            <div>
              <input
                type="text"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="Document title"
                className="w-full rounded-lg border border-border bg-surface-elevated px-4 py-2 text-lg font-semibold text-text-primary placeholder:text-text-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>

            <div>
              <Textarea
                ref={bodyRef}
                value={body}
                onChange={(e) => setBody(e.target.value)}
                placeholder="Start writing..."
                className="min-h-[300px] resize-none overflow-hidden text-sm"
              />
            </div>

            {doc.updated_at && (
              <p className="text-xs text-text-tertiary">
                Last updated: {new Date(doc.updated_at).toLocaleString()}
              </p>
            )}

            {showHistory && (
              <Card className="p-4">
                <h3 className="mb-3 text-sm font-semibold text-text-secondary">Snapshots</h3>
                {snapshotsLoading ? (
                  <div className="space-y-2">
                    <Skeleton className="h-12 w-full rounded-lg" />
                    <Skeleton className="h-12 w-full rounded-lg" />
                  </div>
                ) : snapshots.length === 0 ? (
                  <p className="text-sm text-text-tertiary">No snapshots available</p>
                ) : (
                  <div className="space-y-2">
                    {snapshots.map((s) => (
                      <div
                        key={s.snapshot_id}
                        className="flex items-start justify-between rounded-lg border border-border p-3"
                      >
                        <div className="flex-1 min-w-0">
                          <p className="text-xs text-text-tertiary">
                            {new Date(s.created_at).toLocaleString()}
                          </p>
                          <p className="mt-1 text-sm text-text-secondary truncate">
                            {s.body_preview || '(empty)'}
                          </p>
                        </div>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => handleRestore(s.snapshot_id)}
                          disabled={restoring === s.snapshot_id}
                          isLoading={restoring === s.snapshot_id}
                        >
                          <RotateCcw size={14} className="mr-1" /> Restore
                        </Button>
                      </div>
                    ))}
                  </div>
                )}
              </Card>
            )}
          </div>
        </main>
      </div>
    </AppShell>
  );
}
