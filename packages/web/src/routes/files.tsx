import { useCallback, useEffect, useState } from 'react';
import { File, FileText, Image, Trash2 } from 'lucide-react';
import { AppShell } from '@/components/layout/AppShell';
import { api, ApiError } from '@/lib/api-client';
import { Alert, Button, Card, Skeleton, Badge } from '@/components/ui';

interface FileItem {
  file_id: string;
  name: string;
  mime: string;
  size_bytes: number;
  created_at: string;
}

export function FilesPage() {
  const [files, setFiles] = useState<FileItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    api.listFiles()
      .then((r) => setFiles(r.files))
      .catch((err) => setError(err instanceof ApiError ? err.messageText : String(err)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleDelete = async (fileId: string) => {
    setDeletingIds((prev) => new Set(prev).add(fileId));
    try {
      await api.deleteFile(fileId);
      setFiles((prev) => prev.filter((f) => f.file_id !== fileId));
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setDeletingIds((prev) => { const s = new Set(prev); s.delete(fileId); return s; });
    }
  };

  const formatBytes = (b: number) => {
    if (b < 1024) return `${b} B`;
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
    if (b < 1024 * 1024 * 1024) return `${(b / (1024 * 1024)).toFixed(1)} MB`;
    return `${(b / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  };

  const getIcon = (mime: string) => {
    if (mime.startsWith('image/')) return <Image size={18} className="text-text-tertiary" />;
    if (mime.startsWith('text/') || mime.startsWith('application/pdf')) return <FileText size={18} className="text-text-tertiary" />;
    return <File size={18} className="text-text-tertiary" />;
  };

  return (
    <AppShell>
      <div className="flex h-full flex-col overflow-y-auto">
        <div className="flex items-center gap-3 border-b border-border bg-surface px-6 py-3">
          <File size={20} className="text-text-secondary" />
          <h1 className="text-lg font-semibold text-text-primary">Files</h1>
          <Badge variant="default">{files.length}</Badge>
        </div>

        {loading ? (
          <div className="space-y-4 p-6">
            <Skeleton className="h-16 w-full rounded-xl" />
            <Skeleton className="h-16 w-full rounded-xl" />
            <Skeleton className="h-16 w-full rounded-xl" />
          </div>
        ) : error ? (
          <div className="p-6">
            <Alert variant="error">{error}</Alert>
          </div>
        ) : files.length === 0 ? (
          <div className="flex flex-1 items-center justify-center p-6">
            <div className="text-center">
              <File size={48} className="mx-auto mb-3 text-text-tertiary" />
              <p className="text-sm text-text-secondary">No files found</p>
            </div>
          </div>
        ) : (
          <div className="space-y-3 p-6">
            {files.map((f) => (
              <Card key={f.file_id} className="flex items-center justify-between p-4">
                <div className="flex items-center gap-3 min-w-0">
                  {getIcon(f.mime)}
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-text-primary">{f.name}</p>
                    <p className="text-xs text-text-tertiary">
                      {f.mime} · {formatBytes(f.size_bytes)} · {new Date(f.created_at).toLocaleString()}
                    </p>
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="danger"
                  onClick={() => handleDelete(f.file_id)}
                  disabled={deletingIds.has(f.file_id)}
                >
                  <Trash2 size={14} className="mr-1" />
                  Delete
                </Button>
              </Card>
            ))}
          </div>
        )}
      </div>
    </AppShell>
  );
}
