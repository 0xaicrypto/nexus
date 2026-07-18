import { HttpTransport } from '../core/http-client.js'
import type { LlmStatus } from '../types.js'

export class SettingsModule {
  constructor(private http: HttpTransport) {}

  getLlmStatus() { return this.http.get<LlmStatus>('/api/v1/settings/llm') }
  testLlm() { return this.http.post<{ ok: boolean; provider: string; model: string; latencyMs?: number }>('/api/v1/settings/llm/test') }
  updateLlm(input: Record<string, string>) { return this.http.put('/api/v1/settings/llm', input) }
}
