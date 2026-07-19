import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, Download, Eye, FilePlus, FileText, History, MessageSquare, Paperclip, RotateCcw, ShieldAlert, Sparkles, X } from 'lucide-react';
import { AppShell } from '@/components/layout/AppShell';
import { SkillsBar } from '@/components/SkillsBar';
import { MarkdownRenderer } from '@/components/MarkdownRenderer';
import { Alert, Button, Card, Skeleton, Textarea } from '@/components/ui';
import { api, ApiError } from '@/lib/api-client';
import { cn } from '@/lib/utils';

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

interface PhiFinding {
  start: number;
  end: number;
  text: string;
  suggestion: string;
}

interface ChatMessage {
  role: 'user' | 'assistant';
  text: string;
  _done?: boolean;
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

  const [showReferences, setShowReferences] = useState(false);
  const [references, setReferences] = useState<Array<{reference_id: string; kind: string; label: string; content: string; source_patient_hash: string; created_at: string}>>([]);
  const [referencesLoading, setReferencesLoading] = useState(false);

  const [phiScanning, setPhiScanning] = useState(false);
  const [phiFindings, setPhiFindings] = useState<PhiFinding[] | null>(null);
  const [showPhiDialog, setShowPhiDialog] = useState(false);

  const [exporting, setExporting] = useState(false);
  const [exportResult, setExportResult] = useState<{ docx_path: string; size_bytes: number } | null>(null);

  const [polishOpen, setPolishOpen] = useState(false);
  const [polishInstruction, setPolishInstruction] = useState('');
  const [polishStream, setPolishStream] = useState('');
  const [polishLoading, setPolishLoading] = useState(false);

  const [chatOpen, setChatOpen] = useState(false);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [chatInput, setChatInput] = useState('');
  const [chatLoading, setChatLoading] = useState(false);
  const [activeSkills, setActiveSkills] = useState<string[]>([]);
  const [chatUploadingFile, setChatUploadingFile] = useState(false);
  const [chatAttachedFiles, setChatAttachedFiles] = useState<Array<{name: string; fileId: string}>>([]);

  const [refDialogOpen, setRefDialogOpen] = useState(false);
  const [refForm, setRefForm] = useState({ kind: 'guideline', content: '', label: '', source_patient_hash: '' });
  const [refSubmitting, setRefSubmitting] = useState(false);

  const [preview, setPreview] = useState(false);

  const polishRef = useRef<HTMLDivElement>(null);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const chatFileRef = useRef<HTMLInputElement>(null);

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

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [chatMessages]);

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (polishRef.current && !polishRef.current.contains(e.target as Node)) {
        setPolishOpen(false);
      }
    }
    if (polishOpen) document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [polishOpen]);

  const loadSnapshots = useCallback(() => {
    if (!docId) return;
    setSnapshotsLoading(true);
    api.getDocSnapshots(docId)
      .then((r) => setSnapshots(r.snapshots))
      .catch(() => {})
      .finally(() => setSnapshotsLoading(false));
  }, [docId]);

  const loadReferences = useCallback(() => {
    if (!docId) return;
    setReferencesLoading(true);
    api.getDocReferences(docId)
      .then((r) => setReferences(r.references))
      .catch(() => {})
      .finally(() => setReferencesLoading(false));
  }, [docId]);

  const handleToggleHistory = () => {
    const next = !showHistory;
    setShowHistory(next);
    setShowReferences(false);
    if (next) loadSnapshots();
  };

  const handleToggleReferences = () => {
    const next = !showReferences;
    setShowReferences(next);
    setShowHistory(false);
    if (next) loadReferences();
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

  const handlePhiScan = async () => {
    if (!docId) return;
    setPhiScanning(true);
    setError(null);
    try {
      const result = await api.runPhiScan(docId);
      setPhiFindings(result.findings);
      setShowPhiDialog(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setPhiScanning(false);
    }
  };

  const handleExportDocx = async () => {
    if (!docId) return;
    setExporting(true);
    setError(null);
    try {
      const result = await api.exportDocx(docId, doc?.title);
      setExportResult(result);
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setExporting(false);
    }
  };

  const handlePolish = async () => {
    if (!docId || !bodyRef.current) return;
    const ta = bodyRef.current;
    const selection = body.substring(ta.selectionStart, ta.selectionEnd);
    if (!selection) {
      setError('Select text in the editor first, then click Polish.');
      return;
    }
    setPolishOpen(true);
    setPolishStream('');
    setPolishInstruction('');
  };

  const handlePolishSubmit = async () => {
    if (!docId || !bodyRef.current) return;
    const ta = bodyRef.current;
    const selection = body.substring(ta.selectionStart, ta.selectionEnd);
    if (!selection) return;
    setPolishLoading(true);
    setPolishStream('');
    try {
      let result = '';
      for await (const chunk of api.polishDoc(docId, selection, polishInstruction || undefined)) {
        result += chunk.text;
        setPolishStream(result);
        if (chunk.done) break;
      }
      const start = ta.selectionStart;
      const end = ta.selectionEnd;
      const newBody = body.substring(0, start) + result + body.substring(end);
      setBody(newBody);
      setPolishOpen(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setPolishLoading(false);
    }
  };

  const handleSendChat = async () => {
    if (!docId || !chatInput.trim()) return;
    const text = chatInput.trim();
    const userMsg: ChatMessage = { role: 'user', text };
    setChatMessages((prev) => [...prev, userMsg]);
    setChatInput('');
    setChatLoading(true);
    try {
      for await (const chunk of api.sendDocChat(docId, text, activeSkills)) {
        if (chunk.type === 'reply_chunk' && chunk.text) {
          setChatMessages((prev) => {
            const msgs = [...prev];
            const last = msgs[msgs.length - 1];
            if (!last || last.role !== 'assistant' || last._done) {
              msgs.push({ role: 'assistant', text: chunk.text || '' });
            } else {
              msgs[msgs.length - 1] = { ...last, text: last.text + (chunk.text || '') };
            }
            return msgs;
          });
        } else if (chunk.type === 'doc_chunk' && chunk.text) {
          setChatMessages((prev) => {
            const msgs = [...prev];
            const last = msgs[msgs.length - 1];
            if (last && last.role === 'assistant') {
              msgs[msgs.length - 1] = { ...last, text: last.text + '\n📄 ' + chunk.text };
            }
            return msgs;
          });
        } else if (chunk.type === 'done') {
          setChatMessages((prev) => {
            const msgs = [...prev];
            const last = msgs[msgs.length - 1];
            if (last && last.role === 'assistant') {
              msgs[msgs.length - 1] = { ...last, _done: true };
            }
            return msgs;
          });
          if (chunk.doc_body) {
            setDoc((prev) => prev ? { ...prev, body: chunk.doc_body as string } : prev);
            setBody(chunk.doc_body as string);
          }
        } else if (chunk.type === 'error') {
          setChatMessages((prev) => [...prev, { role: 'assistant', text: 'Error: ' + (chunk.message || 'Unknown') }]);
        }
      }
    } catch (err) {
      setChatMessages((prev) => [...prev, { role: 'assistant', text: 'Chat failed: ' + (err instanceof ApiError ? err.messageText : String(err)) }]);
    } finally {
      setChatLoading(false);
    }
  };

  const handleChatPaste = async (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of Array.from(items)) {
      if (item.kind === 'file') {
        e.preventDefault();
        const file = item.getAsFile();
        if (!file) continue;
        setChatUploadingFile(true);
        try { const result = await api.uploadFile(file); setChatAttachedFiles((prev) => [...prev, { name: result.name, fileId: result.file_id }]); } catch { /* ignore */ }
        finally { setChatUploadingFile(false); }
      }
    }
  };

  const handleChatFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    setChatUploadingFile(true);
    try { const result = await api.uploadFile(f); setChatAttachedFiles((prev) => [...prev, { name: result.name, fileId: result.file_id }]); } catch { /* ignore */ }
    finally { setChatUploadingFile(false); }
  };

  const handleAddReference = async () => {
    if (!docId || !refForm.content.trim() || !refForm.kind.trim()) return;
    setRefSubmitting(true);
    try {
      await api.addDocReference(docId, {
        kind: refForm.kind,
        content: refForm.content,
        label: refForm.label || undefined,
        source_patient_hash: refForm.source_patient_hash || undefined,
      });
      setRefDialogOpen(false);
      setRefForm({ kind: 'guideline', content: '', label: '', source_patient_hash: '' });
      if (showReferences) loadReferences();
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setRefSubmitting(false);
    }
  };

  const highlightedBody = () => {
    if (!phiFindings || phiFindings.length === 0) return null;
    const sorted = [...phiFindings].sort((a, b) => a.start - b.start);
    const parts: JSX.Element[] = [];
    let cursor = 0;
    sorted.forEach((f, i) => {
      if (f.start > cursor) {
        parts.push(<span key={`txt-${i}`}>{body.slice(cursor, f.start)}</span>);
      }
      parts.push(
        <mark key={`phi-${i}`} className="bg-error/20 text-error rounded-sm px-0.5" title={f.suggestion}>
          {body.slice(f.start, f.end)}
        </mark>,
      );
      cursor = f.end;
    });
    if (cursor < body.length) {
      parts.push(<span key="txt-end">{body.slice(cursor)}</span>);
    }
    return parts;
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
      <div className="flex h-full flex-col overflow-hidden">
        <header className="flex h-14 items-center gap-3 border-b border-border bg-surface px-6 shrink-0">
          <Button variant="ghost" size="sm" onClick={() => navigate('/app/writing')}>
            <ArrowLeft size={16} />
          </Button>
          <FileText size={18} className="text-text-tertiary" />
          <h1 className="font-semibold text-text-primary">{doc.title || 'Untitled'}</h1>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setPreview((v) => !v)}
            className="ml-3"
          >
            <Eye size={14} className="mr-1" /> {preview ? 'Edit' : 'Preview'}
          </Button>
          <div className="ml-auto flex items-center gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={handleToggleHistory}
            >
              <History size={14} className="mr-1" /> History
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={handleToggleReferences}
            >
              <FilePlus size={14} className="mr-1" /> References
            </Button>
            <Button size="sm" onClick={handleSave} isLoading={saving} disabled={saving}>
              Save
            </Button>
            <Button size="sm" variant="secondary" onClick={handleExportDocx}>
              <Download size={14} className="mr-1" /> DOCX
            </Button>
          </div>
        </header>

        <div className="flex items-center gap-1 border-b border-border bg-surface px-6 py-1.5 shrink-0">
          <Button
            variant="ghost"
            size="sm"
            onClick={handlePhiScan}
            disabled={phiScanning}
            isLoading={phiScanning}
          >
            <ShieldAlert size={14} className="mr-1" /> Scan PHI
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={handleExportDocx}
            disabled={exporting}
            isLoading={exporting}
          >
            <Download size={14} className="mr-1" /> Export DOCX
          </Button>
          <div className="relative">
            <Button
              variant="ghost"
              size="sm"
              onClick={handlePolish}
            >
              <Sparkles size={14} className="mr-1" /> AI Polish
            </Button>
            {polishOpen && (
              <div
                ref={polishRef}
                className="absolute top-full left-0 mt-1 z-30 w-80 rounded-xl border border-border bg-surface-elevated p-4 shadow-lg"
              >
                <textarea
                  value={polishInstruction}
                  onChange={(e) => setPolishInstruction(e.target.value)}
                  placeholder="Optional instruction (e.g. make it more concise)"
                  className="w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring resize-none h-16"
                />
                {polishStream && (
                  <div className="mt-2 max-h-40 overflow-y-auto rounded-lg border border-border bg-surface p-2 text-sm text-text-secondary whitespace-pre-wrap">
                    {polishStream}
                  </div>
                )}
                <div className="mt-2 flex justify-end gap-2">
                  <Button variant="ghost" size="sm" onClick={() => setPolishOpen(false)}>Cancel</Button>
                  <Button size="sm" onClick={handlePolishSubmit} isLoading={polishLoading} disabled={polishLoading}>
                    Polish
                  </Button>
                </div>
              </div>
            )}
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setRefDialogOpen(true)}
          >
            <FilePlus size={14} className="mr-1" /> Reference
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setChatOpen((v) => !v)}
          >
            <MessageSquare size={14} className="mr-1" /> Chat
          </Button>

          {exportResult && (
            <div className="ml-3 flex items-center gap-2 text-xs text-success">
              <span>Exported: {exportResult.docx_path} ({(exportResult.size_bytes / 1024).toFixed(1)} KB)</span>
              <button onClick={() => setExportResult(null)} className="text-text-tertiary hover:text-text-primary"><X size={12} /></button>
            </div>
          )}
        </div>

        <div className="flex flex-1 overflow-hidden">
          <main className={cn('flex-1 overflow-y-auto p-6', chatOpen ? 'border-r border-border' : '')}>
            {error && (
              <div className="mb-4 max-w-3xl mx-auto">
                <Alert variant="error">{error}</Alert>
              </div>
            )}

            {phiFindings && phiFindings.length > 0 && (
              <div className="mb-4 max-w-3xl mx-auto">
                <Alert variant="warning">
                  Found {phiFindings.length} potential PHI instance{phiFindings.length !== 1 ? 's' : ''}.{' '}
                  <button className="underline font-medium" onClick={() => setShowPhiDialog(true)}>View details</button>
                </Alert>
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
                {preview ? (
                  <div className="min-h-[300px] rounded-lg border border-border bg-surface-elevated p-4">
                    <MarkdownRenderer content={body} />
                  </div>
                ) : (
                  <Textarea
                    ref={bodyRef}
                    value={body}
                    onChange={(e) => setBody(e.target.value)}
                    placeholder="Start writing..."
                    className="min-h-[300px] resize-none overflow-hidden text-sm"
                  />
                )}
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

              {showReferences && (
                <Card className="p-4">
                  <h3 className="mb-3 text-sm font-semibold text-text-secondary">References</h3>
                  {referencesLoading ? (
                    <div className="space-y-2">
                      <Skeleton className="h-12 w-full rounded-lg" />
                      <Skeleton className="h-12 w-full rounded-lg" />
                    </div>
                  ) : references.length === 0 ? (
                    <p className="text-sm text-text-tertiary">No references yet</p>
                  ) : (
                    <div className="space-y-2">
                      {references.map((ref) => (
                        <div
                          key={ref.reference_id}
                          className="rounded-lg border border-border p-3"
                        >
                          <div className="flex items-center justify-between">
                            <span className="text-xs font-medium text-text-secondary uppercase">{ref.kind}</span>
                            <span className="text-xs text-text-tertiary">{new Date(ref.created_at).toLocaleString()}</span>
                          </div>
                          {ref.label && <p className="mt-1 text-sm font-medium text-text-primary">{ref.label}</p>}
                          <p className="mt-1 text-sm text-text-secondary whitespace-pre-wrap">{ref.content}</p>
                          {ref.source_patient_hash && (
                            <p className="mt-1 text-xs text-text-tertiary">Patient: {ref.source_patient_hash}</p>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </Card>
              )}
            </div>
          </main>

          {chatOpen && (
            <aside className="w-80 flex shrink-0 flex-col bg-surface">
              <div className="flex h-10 items-center justify-between border-b border-border px-3">
                <span className="text-sm font-medium text-text-secondary">Doc Chat</span>
                <button onClick={() => setChatOpen(false)} className="text-text-tertiary hover:text-text-primary">
                  <X size={14} />
                </button>
              </div>
              <SkillsBar active={activeSkills} onToggle={(name) => setActiveSkills((prev) => prev.includes(name) ? prev.filter((s) => s !== name) : [...prev, name])} />
              <div className="flex-1 overflow-y-auto p-3 space-y-3">
                {chatMessages.length === 0 && (
                  <p className="text-sm text-text-tertiary text-center mt-4 leading-relaxed">
                    Ask the AI to write or research content.<br />
                    It will update this document automatically.<br />
                    <span className="text-xs">e.g. "Write a clinical review on..."</span>
                  </p>
                )}
                {chatMessages.map((m, i) => (
                  <div
                    key={i}
                    className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}
                  >
                    <div
                      className={`max-w-[90%] rounded-2xl px-3 py-2 text-sm leading-relaxed ${
                        m.role === 'user'
                          ? 'bg-accent text-white'
                          : 'border border-border bg-surface-elevated text-text-primary shadow-sm'
                      }`}
                    >
                      <MarkdownRenderer content={m.text || ''} />
                      {m._done === false && !m.text && (
                        <span className="animate-pulse">●</span>
                      )}
                    </div>
                  </div>
                ))}
                {chatLoading && (
                  <div className="flex justify-start">
                    <div className="rounded-2xl border border-border bg-surface-elevated px-3 py-2 text-sm shadow-sm">
                      <span className="animate-pulse">●</span>
                    </div>
                  </div>
                )}
                <div ref={chatEndRef} />
              </div>
              <div className="border-t border-border p-3">
                {chatAttachedFiles.length > 0 && (
                  <div className="mb-2 flex gap-1 flex-wrap">
                    {chatAttachedFiles.map((f) => (
                      <span key={f.fileId} className="inline-flex items-center rounded-full bg-surface-elevated border border-border px-2 py-0.5 text-xs text-text-secondary">{f.name}</span>
                    ))}
                  </div>
                )}
                <div className="flex gap-2">
                  <input ref={chatFileRef} type="file" onChange={handleChatFile} className="hidden" disabled={chatUploadingFile} />
                  <Button variant="ghost" size="sm" onClick={() => chatFileRef.current?.click()} disabled={chatLoading || chatUploadingFile} isLoading={chatUploadingFile} className="shrink-0">
                    <Paperclip size={16} />
                  </Button>
                  <Textarea
                    value={chatInput}
                    onChange={(e) => setChatInput(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSendChat(); } }}
                    onPaste={handleChatPaste}
                    placeholder="Ask a question..."
                    rows={1}
                    className="min-h-0 flex-1 resize-none py-1.5"
                    style={{ maxHeight: '120px' }}
                  />
                  <Button size="sm" onClick={handleSendChat} disabled={chatLoading || !chatInput.trim()} className="shrink-0">
                    Send
                  </Button>
                </div>
              </div>
            </aside>
          )}
        </div>

        {/* PHI Findings Dialog */}
        {showPhiDialog && phiFindings && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={() => setShowPhiDialog(false)}>
            <div className="w-full max-w-2xl max-h-[80vh] overflow-y-auto rounded-xl border border-border bg-surface-elevated shadow-xl p-6 m-4" onClick={(e) => e.stopPropagation()}>
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-semibold text-text-primary">PHI Findings</h2>
                <button onClick={() => setShowPhiDialog(false)} className="text-text-tertiary hover:text-text-primary">
                  <X size={18} />
                </button>
              </div>
              <p className="text-sm text-text-secondary mb-4">
                Found {phiFindings.length} potential PHI instance{phiFindings.length !== 1 ? 's' : ''} in the document. Review and manually redact as needed.
              </p>
              <div className="rounded-lg border border-border bg-surface p-4 mb-4 max-h-60 overflow-y-auto text-sm text-text-primary whitespace-pre-wrap">
                {highlightedBody()}
              </div>
              <div className="space-y-3">
                {phiFindings.map((f, i) => (
                  <div key={i} className="rounded-lg border border-border p-3">
                    <div className="flex items-start justify-between gap-2">
                      <div>
                        <p className="text-sm font-medium text-text-primary">&ldquo;{f.text}&rdquo;</p>
                        <p className="text-xs text-text-tertiary mt-0.5">
                          Position: {f.start}–{f.end}
                        </p>
                      </div>
                      <span className="text-xs text-warning bg-warning/10 rounded-full px-2 py-0.5 shrink-0">PHI</span>
                    </div>
                    <p className="mt-2 text-sm text-text-secondary">
                      <span className="font-medium">Suggestion:</span> {f.suggestion}
                    </p>
                  </div>
                ))}
              </div>
              <div className="mt-4 flex justify-end">
                <Button variant="ghost" size="sm" onClick={() => setShowPhiDialog(false)}>Close</Button>
              </div>
            </div>
          </div>
        )}

        {/* Add Reference Dialog */}
        {refDialogOpen && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={() => setRefDialogOpen(false)}>
            <div className="w-full max-w-md rounded-xl border border-border bg-surface-elevated shadow-xl p-6 m-4" onClick={(e) => e.stopPropagation()}>
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-semibold text-text-primary">Add Reference</h2>
                <button onClick={() => setRefDialogOpen(false)} className="text-text-tertiary hover:text-text-primary">
                  <X size={18} />
                </button>
              </div>
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-text-secondary mb-1">Kind</label>
                  <select
                    value={refForm.kind}
                    onChange={(e) => setRefForm((p) => ({ ...p, kind: e.target.value }))}
                    className="w-full rounded-lg border border-border bg-surface-elevated px-3 py-2 text-sm text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <option value="guideline">Guideline</option>
                    <option value="research">Research</option>
                    <option value="protocol">Protocol</option>
                    <option value="note">Note</option>
                    <option value="other">Other</option>
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-text-secondary mb-1">Label</label>
                  <input
                    type="text"
                    value={refForm.label}
                    onChange={(e) => setRefForm((p) => ({ ...p, label: e.target.value }))}
                    placeholder="e.g. WHO Guideline v3"
                    className="w-full rounded-lg border border-border bg-surface-elevated px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-text-secondary mb-1">Content</label>
                  <textarea
                    value={refForm.content}
                    onChange={(e) => setRefForm((p) => ({ ...p, content: e.target.value }))}
                    placeholder="Paste or type reference content..."
                    rows={4}
                    className="w-full rounded-lg border border-border bg-surface-elevated px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring resize-none"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-text-secondary mb-1">Source Patient Hash (optional)</label>
                  <input
                    type="text"
                    value={refForm.source_patient_hash}
                    onChange={(e) => setRefForm((p) => ({ ...p, source_patient_hash: e.target.value }))}
                    placeholder="Optional patient hash"
                    className="w-full rounded-lg border border-border bg-surface-elevated px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  />
                </div>
              </div>
              <div className="mt-4 flex justify-end gap-2">
                <Button variant="ghost" size="sm" onClick={() => setRefDialogOpen(false)}>Cancel</Button>
                <Button size="sm" onClick={handleAddReference} isLoading={refSubmitting} disabled={refSubmitting}>
                  Add
                </Button>
              </div>
            </div>
          </div>
        )}
      </div>
    </AppShell>
  );
}
