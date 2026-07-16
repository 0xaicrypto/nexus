import { useState } from 'react';
import { Download, FileArchive } from 'lucide-react';
import { AppShell } from '@/components/layout/AppShell';
import { api, ApiError } from '@/lib/api-client';
import { Alert, Button, Card } from '@/components/ui';

interface ExportResult {
  bundle_path: string;
  size_bytes: number;
  created_at: string;
  counts: Record<string, number>;
}

export function ExportPage() {
  const [result, setResult] = useState<ExportResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleExport = async () => {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.exportBundle();
      setResult(r);
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setLoading(false);
    }
  };

  const formatBytes = (b: number) => {
    if (b < 1024) return `${b} B`;
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
    if (b < 1024 * 1024 * 1024) return `${(b / (1024 * 1024)).toFixed(1)} MB`;
    return `${(b / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  };

  return (
    <AppShell>
      <div className="flex h-full flex-col overflow-y-auto">
        <div className="flex items-center gap-3 border-b border-border bg-surface px-6 py-3">
          <Download size={20} className="text-text-secondary" />
          <h1 className="text-lg font-semibold text-text-primary">Export</h1>
        </div>

        <div className="mx-auto max-w-lg space-y-6 p-6">
          <Card className="p-6 text-center">
            <FileArchive size={48} className="mx-auto mb-4 text-text-tertiary" />
            <p className="mb-1 text-sm text-text-primary">
              Export a bundled archive of your Nexus data.
            </p>
            <p className="mb-4 text-xs text-text-tertiary">
              This will create a timestamped bundle containing patients, studies, messages, documents, and more.
            </p>
            <Button onClick={handleExport} disabled={loading} isLoading={loading}>
              <Download size={16} className="mr-2" />
              Export Bundle
            </Button>
          </Card>

          {error && <Alert variant="error">{error}</Alert>}

          {result && (
            <Card className="p-4 space-y-2">
              <h3 className="font-semibold text-text-primary">Export Result</h3>
              <div className="grid grid-cols-2 gap-y-1 text-sm">
                <span className="text-text-tertiary">Path</span>
                <span className="truncate font-mono text-xs text-text-primary">{result.bundle_path}</span>
                <span className="text-text-tertiary">Size</span>
                <span className="text-text-primary">{formatBytes(result.size_bytes)}</span>
                <span className="text-text-tertiary">Created</span>
                <span className="text-text-primary">{new Date(result.created_at).toLocaleString()}</span>
              </div>
              {Object.keys(result.counts).length > 0 && (
                <div>
                  <p className="mb-1 text-xs font-semibold text-text-tertiary">Counts</p>
                  <div className="flex flex-wrap gap-2">
                    {Object.entries(result.counts).map(([k, v]) => (
                      <span key={k} className="rounded-full bg-surface px-2 py-0.5 text-xs text-text-secondary border border-border">
                        {k}: {v}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </Card>
          )}
        </div>
      </div>
    </AppShell>
  );
}
