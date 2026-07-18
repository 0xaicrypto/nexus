import { HttpTransport } from '../core/http-client.js'
import type { MemoryProjection, MemoryFinding, MemoryTimelineEvent } from '../types.js'

export class MemoryModule {
  constructor(private http: HttpTransport) {}

  getProjection(hash: string) {
    return this.http.get<MemoryProjection>(`/api/v1/memory/patient/${hash}/projection`)
  }
  getFindings(hash: string) {
    return this.http.get<{ findings: MemoryFinding[] }>(`/api/v1/memory/patient/${hash}/findings`)
  }
  getTimeline(hash: string) {
    return this.http.get<{ entries: MemoryTimelineEvent[] }>(`/api/v1/memory/patient/${hash}/timeline`)
  }
  getMedications(hash: string) {
    return this.http.get<{ medications: MemoryFinding[] }>(`/api/v1/memory/patient/${hash}/medications`)
  }
}
