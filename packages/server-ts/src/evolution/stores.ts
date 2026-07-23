import { VersionedStore } from '../core/versioned-store'
import path from 'path'
import fs from 'fs'

export interface Fact {
  id: string
  category: 'preference' | 'fact' | 'constraint' | 'goal' | 'context'
  importance: number  // 1-5
  content: string
  count: number        // how many times this fact has been observed
  ttl?: number         // optional expiry
  createdAt: number
  updatedAt: number
  lastSeenAt: number
}

export interface Episode {
  sessionId: string
  summary: string
  turnCount: number
  createdAt: number
}

export interface LearnedSkill {
  name: string
  taskKind: string
  bestStrategy: string
  taskCount: number
  successCount: number
  failureCount: number
  createdAt: number
}

export interface KnowledgeArticle {
  id: string
  title: string
  content: string
  sources: string[]
  version: number
  status: 'current' | 'stale'
  staleBecause?: string[]  // which source IDs changed
  createdAt: number
  updatedAt: number
}

// ── Facts Store ──────────────────────────────────────────────

export class FactsStore {
  private store: VersionedStore
  private working: Fact[] = []

  constructor(baseDir: string) {
    const dir = path.join(baseDir, 'facts')
    fs.mkdirSync(dir, { recursive: true })
    this.store = new VersionedStore(dir)
    const current = this.store.current()
    if (current && Array.isArray(current)) {
      this.working = current.map((f: any) => ({
        ...f,
        count: f.count || 1,
        updatedAt: f.updatedAt || f.createdAt || 0,
        lastSeenAt: f.lastSeenAt || f.createdAt || 0,
      }))
    }
  }

  all(): Fact[] { return [...this.working] }
  currentVersion() { return this.store.currentVersion() }

  add(fact: Omit<Fact, 'id' | 'createdAt' | 'updatedAt' | 'lastSeenAt' | 'count'>): Fact {
    const now = Date.now()
    // Dedup: merge with existing fact of same content + category
    const existing = this.working.find(f => f.content === fact.content && f.category === fact.category)
    if (existing) {
      existing.count = (existing.count || 1) + 1
      existing.importance = Math.max(existing.importance, fact.importance || 1)
      existing.updatedAt = now
      existing.lastSeenAt = now
      return existing
    }
    const f: Fact = {
      id: `${now}-${Math.random().toString(36).slice(2, 8)}`,
      ...fact,
      count: 1,
      createdAt: now,
      updatedAt: now,
      lastSeenAt: now,
    }
    this.working.push(f)
    return f
  }

  remove(id: string): boolean {
    const before = this.working.length
    this.working = this.working.filter(f => f.id !== id)
    return this.working.length < before
  }

  commit(): string {
    return this.store.propose(this.working)
  }

  rollback(version: string): string {
    const prev = this.store.rollback(version)
    const current = this.store.current()
    if (current && Array.isArray(current)) this.working = current
    return prev
  }
}

// ── Episodes Store ───────────────────────────────────────────

export class EpisodesStore {
  private store: VersionedStore
  private working: Episode[] = []

  constructor(baseDir: string) {
    const dir = path.join(baseDir, 'episodes')
    fs.mkdirSync(dir, { recursive: true })
    this.store = new VersionedStore(dir)
    const current = this.store.current()
    if (current && Array.isArray(current)) this.working = current
  }

  upsert(sessionId: string, summary: string, turnCount: number) {
    const existing = this.working.findIndex(e => e.sessionId === sessionId)
    const ep: Episode = { sessionId, summary, turnCount, createdAt: Date.now() }
    if (existing >= 0) this.working[existing] = ep
    else this.working.push(ep)
  }

  all(): Episode[] { return [...this.working] }
  commit(): string { return this.store.propose(this.working) }
}

// ── Skills Store ─────────────────────────────────────────────

export class SkillsStore {
  private store: VersionedStore
  private working: LearnedSkill[] = []

  constructor(baseDir: string) {
    const dir = path.join(baseDir, 'skills')
    fs.mkdirSync(dir, { recursive: true })
    this.store = new VersionedStore(dir)
    const current = this.store.current()
    if (current && Array.isArray(current)) this.working = current
  }

  recordTask(name: string, taskKind: string, success: boolean, strategy: string) {
    let skill = this.working.find(s => s.name === name)
    if (!skill) {
      skill = { name, taskKind, bestStrategy: strategy, taskCount: 0, successCount: 0, failureCount: 0, createdAt: Date.now() }
      this.working.push(skill)
    }
    skill.taskCount++
    if (success) skill.successCount++
    else skill.failureCount++
    skill.bestStrategy = strategy
  }

  all(): LearnedSkill[] { return [...this.working] }
  commit(): string { return this.store.propose(this.working) }
}

// ── Knowledge Store ──────────────────────────────────────────

export class KnowledgeStore {
  private store: VersionedStore
  private working: KnowledgeArticle[] = []

  constructor(baseDir: string) {
    const dir = path.join(baseDir, 'knowledge')
    fs.mkdirSync(dir, { recursive: true })
    this.store = new VersionedStore(dir)
    const current = this.store.current()
    if (current && Array.isArray(current)) {
      this.working = current.map((a: any) => ({
        ...a,
        version: a.version || 1,
        status: a.status || 'current',
        updatedAt: a.updatedAt || a.createdAt || 0,
      }))
    }
  }

  add(article: Omit<KnowledgeArticle, 'id' | 'createdAt' | 'updatedAt' | 'version' | 'status'>): KnowledgeArticle {
    const now = Date.now()
    const a: KnowledgeArticle = {
      id: `${now}-${Math.random().toString(36).slice(2, 8)}`,
      ...article,
      version: 1,
      status: 'current',
      createdAt: now,
      updatedAt: now,
    }
    this.working.unshift(a)
    return a
  }

  update(id: string, patch: Partial<Pick<KnowledgeArticle, 'title' | 'content' | 'sources'>>): KnowledgeArticle | null {
    const idx = this.working.findIndex(a => a.id === id)
    if (idx === -1) return null
    const now = Date.now()
    this.working[idx] = {
      ...this.working[idx],
      ...patch,
      version: this.working[idx].version + 1,
      status: 'current',
      staleBecause: undefined,
      updatedAt: now,
    }
    return this.working[idx]
  }

  markStale(id: string, changedSources: string[]): boolean {
    const article = this.working.find(a => a.id === id)
    if (!article) return false
    article.status = 'stale'
    article.staleBecause = changedSources
    article.updatedAt = Date.now()
    return true
  }

  markFresh(id: string): boolean {
    const article = this.working.find(a => a.id === id)
    if (!article) return false
    article.status = 'current'
    article.staleBecause = undefined
    article.updatedAt = Date.now()
    return true
  }

  isStale(id: string): boolean {
    return this.working.find(a => a.id === id)?.status === 'stale'
  }

  getStale(): KnowledgeArticle[] {
    return this.working.filter(a => a.status === 'stale')
  }

  compile(article: Omit<KnowledgeArticle, 'id' | 'createdAt' | 'updatedAt' | 'version' | 'status'>): KnowledgeArticle {
    return this.add(article)
  }

  all(): KnowledgeArticle[] { return [...this.working] }
  commit(): string { return this.store.propose(this.working) }
}
