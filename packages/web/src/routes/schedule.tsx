import { useCallback, useEffect, useState } from 'react';
import { Calendar, Clock, Trash2 } from 'lucide-react';
import { AppShell } from '@/components/layout/AppShell';
import { api, ApiError } from '@/lib/api-client';
import { Alert, Button, Card, Skeleton, Badge } from '@/components/ui';

interface Task {
  task_id: string;
  kind: string;
  fire_at: string;
  payload: Record<string, unknown>;
  patient_hash?: string;
  session_id?: string;
}

export function SchedulePage() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [cancellingIds, setCancellingIds] = useState<Set<string>>(new Set());

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    api.listSchedule()
      .then((r) => setTasks(r.tasks))
      .catch((err) => setError(err instanceof ApiError ? err.messageText : String(err)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleCancel = async (taskId: string) => {
    setCancellingIds((prev) => new Set(prev).add(taskId));
    try {
      await api.cancelTask(taskId);
      setTasks((prev) => prev.filter((t) => t.task_id !== taskId));
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setCancellingIds((prev) => { const s = new Set(prev); s.delete(taskId); return s; });
    }
  };

  const formatDate = (iso: string) => new Date(iso).toLocaleString();

  const payloadPreview = (p: Record<string, unknown>) => {
    const str = JSON.stringify(p);
    return str.length > 80 ? str.slice(0, 80) + '…' : str;
  };

  return (
    <AppShell>
      <div className="flex h-full flex-col overflow-y-auto">
        <div className="flex items-center gap-3 border-b border-border bg-surface px-6 py-3">
          <Calendar size={20} className="text-text-secondary" />
          <h1 className="text-lg font-semibold text-text-primary">Schedule</h1>
          <Badge variant="default">{tasks.length}</Badge>
        </div>

        {loading ? (
          <div className="space-y-4 p-6">
            <Skeleton className="h-20 w-full rounded-xl" />
            <Skeleton className="h-20 w-full rounded-xl" />
            <Skeleton className="h-20 w-full rounded-xl" />
          </div>
        ) : error ? (
          <div className="p-6">
            <Alert variant="error">{error}</Alert>
          </div>
        ) : tasks.length === 0 ? (
          <div className="flex flex-1 items-center justify-center p-6">
            <div className="text-center">
              <Calendar size={48} className="mx-auto mb-3 text-text-tertiary" />
              <p className="text-sm text-text-secondary">No scheduled tasks</p>
            </div>
          </div>
        ) : (
          <div className="space-y-3 p-6">
            {tasks.map((t) => (
              <Card key={t.task_id} className="p-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0 flex-1 space-y-1">
                    <div className="flex items-center gap-2">
                      <Badge variant="default">{t.kind}</Badge>
                      {t.patient_hash && (
                        <span className="text-xs text-text-tertiary">
                          Patient: {t.patient_hash.slice(0, 8)}…
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-1 text-xs text-text-tertiary">
                      <Clock size={12} />
                      <span>{formatDate(t.fire_at)}</span>
                    </div>
                    <p className="truncate text-xs text-text-tertiary">{payloadPreview(t.payload)}</p>
                  </div>
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => handleCancel(t.task_id)}
                    disabled={cancellingIds.has(t.task_id)}
                  >
                    <Trash2 size={14} className="mr-1" />
                    Cancel
                  </Button>
                </div>
              </Card>
            ))}
          </div>
        )}
      </div>
    </AppShell>
  );
}
