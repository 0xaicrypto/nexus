import { HttpTransport } from '../core/http-client.js'
import { parseSSEStream } from '../core/stream-parser.js'
import type { ChatStreamChunk } from '../types.js'
import type { Doc, SnapshotEntry, PhiFinding } from '../types.js'

export class DocumentsModule {
  constructor(private http: HttpTransport) {}

  list() { return this.http.get<{ docs: Doc[] }>('/api/v1/docs') }
  create(title: string) { return this.http.post<Doc>('/api/v1/docs', { title }) }
  get(id: string) { return this.http.get<Doc>(`/api/v1/docs/${id}`) }
  update(id: string, data: { title?: string; body?: string }) { return this.http.put<Doc>(`/api/v1/docs/${id}`, data) }

  async *polish(docId: string, selection?: string, instruction?: string): AsyncGenerator<ChatStreamChunk> {
    const res = await this.http.stream(`/api/v1/docs/${docId}/polish`, { selection, instruction })
    yield* parseSSEStream(res)
  }

  async *docChat(docId: string, message: string): AsyncGenerator<ChatStreamChunk> {
    const res = await this.http.stream(`/api/v1/docs/${docId}/chat`, { message })
    yield* parseSSEStream(res)
  }

  getSnapshots(docId: string) {
    return this.http.get<{ snapshots: SnapshotEntry[] }>(`/api/v1/docs/${docId}/snapshots`)
  }
  restoreSnapshot(docId: string, snapshotId: number) {
    return this.http.post(`/api/v1/docs/${docId}/snapshots/${snapshotId}/restore`)
  }

  phiScan(docId: string) { return this.http.post<{ findings: PhiFinding[] }>(`/api/v1/docs/${docId}/phi-scan`) }
  exportDocx(docId: string) { return this.http.post(`/api/v1/docs/${docId}/export`) }
}
