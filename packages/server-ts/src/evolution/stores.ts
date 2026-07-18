import { VersionedStore } from '../core/versioned-store'
import path from 'path'
import fs from 'fs'

export interface Fact {
  id: string
  category: 'preference' | 'fact' | 'constraint' | 'goal' | 'context'
  importance: number  // 1-5
  content: string
  ttl?: number        // optional expiry
  createdAt: number
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
  createdAt: number
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
    if (current && Array.isArray(current)) this.working = current
  }

  all(): Fact[] { return [...this.working] }
  currentVersion() { return this.store.currentVersion() }

  add(fact: Omit<Fact, 'id' | 'createdAt'>): Fact {
    const f: Fact = { id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, ...fact, createdAt: Date.now() }
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
    if (current && Array.isArray(current)) this.working = current
  }

  compile(article: Omit<KnowledgeArticle, 'id' | 'createdAt'>): KnowledgeArticle {
    const a: KnowledgeArticle = { id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, ...article, createdAt: Date.now() }
    this.working.push(a)
    return a
  }

  all(): KnowledgeArticle[] { return [...this.working] }
  commit(): string { return this.store.propose(this.working) }
}
