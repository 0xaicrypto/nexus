import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, ApiError } from '@/lib/api-client';
import type { ChatStreamChunk, LlmStatus } from '@/lib/types';
import { useAuthStore } from '@/stores/auth';

interface Message {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  isStreaming?: boolean;
}

export function ChatPage() {
  const navigate = useNavigate();
  const { isAuthenticated, displayName, clearSession } = useAuthStore();

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [llmStatus, setLlmStatus] = useState<LlmStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!isAuthenticated) {
      navigate('/login', { replace: true });
      return;
    }
    api
      .getLlmStatus()
      .then(setLlmStatus)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) clearSession();
        else setError(err instanceof ApiError ? err.messageText : 'Failed to load LLM status');
      });
  }, [isAuthenticated, navigate, clearSession]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleLogout = () => {
    api.logout();
    clearSession();
    navigate('/login', { replace: true });
  };

  const handleSend = async () => {
    if (!input.trim() || loading) return;
    const userText = input.trim();
    setInput('');
    setError(null);

    const userMessage: Message = { id: crypto.randomUUID(), role: 'user', text: userText };
    const assistantMessage: Message = {
      id: crypto.randomUUID(),
      role: 'assistant',
      text: '',
      isStreaming: true,
    };

    setMessages((prev) => [...prev, userMessage, assistantMessage]);
    setLoading(true);

    abortRef.current = new AbortController();
    try {
      for await (const chunk of api.sendChat(userText, '', abortRef.current.signal)) {
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (!last || last.role !== 'assistant') return prev;
          const next = applyChunk(last, chunk);
          return [...prev.slice(0, -1), next];
        });
      }
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.messageText);
      } else if (err instanceof Error && err.name !== 'AbortError') {
        setError(err.message);
      }
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.role === 'assistant') {
          return [...prev.slice(0, -1), { ...last, isStreaming: false }];
        }
        return prev;
      });
    } finally {
      setLoading(false);
      abortRef.current = null;
    }
  };

  const handleStop = () => {
    abortRef.current?.abort();
  };

  return (
    <div className="flex h-screen flex-col bg-slate-50">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-3">
        <div className="flex items-center gap-3">
          <div className="text-lg font-bold text-slate-900">Nexus</div>
          {llmStatus && (
            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600">
              {llmStatus.provider}/{llmStatus.model}
            </span>
          )}
        </div>
        <div className="flex items-center gap-4">
          <span className="text-sm text-slate-500">{displayName || 'User'}</span>
          <button
            onClick={handleLogout}
            className="rounded-lg px-3 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-100"
          >
            Log out
          </button>
        </div>
      </header>

      <main className="flex-1 overflow-y-auto px-4 py-6">
        <div className="mx-auto max-w-3xl space-y-6">
          {messages.length === 0 && (
            <div className="py-20 text-center text-slate-400">
              <p className="text-lg">Start a conversation with your twin.</p>
              <p className="text-sm">It remembers context across turns.</p>
            </div>
          )}
          {messages.map((m) => (
            <div
              key={m.id}
              className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}
            >
              <div
                className={`max-w-[80%] rounded-2xl px-4 py-3 text-sm leading-relaxed ${
                  m.role === 'user'
                    ? 'bg-nexus-600 text-white'
                    : 'bg-white text-slate-800 shadow-sm'
                }`}
              >
                {m.text || (m.isStreaming ? <span className="animate-pulse">●</span> : null)}
              </div>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
      </main>

      {error && (
        <div className="mx-auto w-full max-w-3xl px-4 pb-2">
          <div className="rounded-lg bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>
        </div>
      )}

      <footer className="border-t border-slate-200 bg-white px-4 py-4">
        <div className="mx-auto flex max-w-3xl gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSend()}
            placeholder="Type a message…"
            disabled={loading}
            className="flex-1 rounded-xl border border-slate-300 px-4 py-3 focus:border-nexus-500 focus:outline-none focus:ring-1 focus:ring-nexus-500 disabled:bg-slate-100"
          />
          {loading ? (
            <button
              onClick={handleStop}
              className="rounded-xl bg-slate-200 px-5 py-3 font-semibold text-slate-700 hover:bg-slate-300"
            >
              Stop
            </button>
          ) : (
            <button
              onClick={handleSend}
              disabled={!input.trim()}
              className="rounded-xl bg-nexus-600 px-5 py-3 font-semibold text-white hover:bg-nexus-700 disabled:opacity-50"
            >
              Send
            </button>
          )}
        </div>
      </footer>
    </div>
  );
}

function applyChunk(message: Message, chunk: ChatStreamChunk): Message {
  switch (chunk.type) {
    case 'final_answer_chunk':
      return { ...message, text: message.text + chunk.text };
    case 'turn_complete':
      return { ...message, isStreaming: false };
    case 'error':
      return { ...message, text: message.text || `Error: ${chunk.message}`, isStreaming: false };
    default:
      return message;
  }
}
