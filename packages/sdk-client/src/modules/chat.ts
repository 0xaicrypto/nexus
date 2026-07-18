import { HttpTransport } from '../core/http-client.js'
import { parseSSEStream } from '../core/stream-parser.js'
import type { ChatSession, ChatStreamChunk, SendChatOptions } from '../types.js'

export class ChatModule {
  constructor(private http: HttpTransport) {}

  async *sendMessage(opts: SendChatOptions): AsyncGenerator<ChatStreamChunk> {
    const body: Record<string, unknown> = {
      text: opts.text,
      session_id: opts.sessionId || '',
      patient_hash: opts.patientHash ?? null,
    }
    if (opts.skills) body.skills = opts.skills
    if (opts.scope) body.scope = opts.scope

    const res = await this.http.stream('/api/v1/agent/chat', body)
    yield* parseSSEStream(res)
  }

  listSessions(includeArchived = false) {
    return this.http.get<{ sessions: ChatSession[] }>('/api/v1/sessions', { include_archived: includeArchived ? '1' : '0' })
  }

  createSession(title: string) {
    return this.http.post<ChatSession>('/api/v1/sessions', { title })
  }

  deleteSession(id: string) {
    return this.http.del(`/api/v1/sessions/${id}`)
  }
}
