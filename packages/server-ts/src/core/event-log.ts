import fs from 'fs'
import path from 'path'

export interface Event {
  idx: number
  timestamp: number
  eventType: string
  content: string
  metadata: Record<string, unknown>
  agentId: string
  sessionId: string
}

/**
 * Append-only event log backed by JSONL file.
 * Each line is a JSON object — same format as the Python WriteAheadLog.
 * For production use, the Python SDK's SQLite EventLog is the source of truth;
 * this is a lightweight TS-native equivalent for dev/test/standalone mode.
 */
export class EventLog {
  private filePath: string
  private agentId: string
  private cache: Event[] = []
  private nextIdx: number = 1

  constructor(baseDir: string, agentId: string) {
    fs.mkdirSync(baseDir, { recursive: true })
    this.filePath = path.join(baseDir, 'event_log.jsonl')
    this.agentId = agentId
    this.load()
  }

  private load() {
    if (!fs.existsSync(this.filePath)) return
    const lines = fs.readFileSync(this.filePath, 'utf-8').split('\n').filter(Boolean)
    this.cache = lines.map(line => JSON.parse(line))
    this.nextIdx = this.cache.length > 0
      ? Math.max(...this.cache.map(e => e.idx)) + 1
      : 1
  }

  append(event: Omit<Event, 'idx'>): Event {
    const full: Event = { ...event, idx: this.nextIdx++, agentId: event.agentId || this.agentId }
    this.cache.push(full)
    fs.appendFileSync(this.filePath, JSON.stringify(full) + '\n')
    return full
  }

  query(opts: {
    sessionId?: string
    eventType?: string
    limit?: number
    afterIdx?: number
  }): Event[] {
    let results = [...this.cache]
    if (opts.sessionId) results = results.filter(e => e.sessionId === opts.sessionId)
    if (opts.eventType) results = results.filter(e => e.eventType === opts.eventType)
    if (opts.afterIdx !== undefined && opts.afterIdx !== null) results = results.filter(e => e.idx > opts.afterIdx!)
    results.sort((a, b) => b.idx - a.idx)
    if (opts.limit) results = results.slice(0, opts.limit)
    return results
  }

  count(): number {
    return this.cache.length
  }

  deleteSession(sessionId: string): number {
    const before = this.cache.length
    this.cache = this.cache.filter(e => e.sessionId !== sessionId)
    const removed = before - this.cache.length
    if (removed > 0) {
      fs.writeFileSync(this.filePath, this.cache.map(e => JSON.stringify(e)).join('\n') + '\n')
    }
    return removed
  }

  close() {}
}
