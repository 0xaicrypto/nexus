import path from 'path'
import fs from 'fs'
import { EventLog } from '../../core/event-log'
import { FactsStore, EpisodesStore, SkillsStore, KnowledgeStore } from '../../evolution/stores'
import { ContractEngine } from '../../core/contracts'
import { ChatOrchestrator } from './chat.orchestrator.js'

const TTL_MS = 30 * 60 * 1000 // 30 minutes idle → evict
const GC_INTERVAL_MS = 5 * 60 * 1000

interface UserContext {
  eventLog: EventLog; facts: FactsStore; episodes: EpisodesStore; skills: SkillsStore; knowledge: KnowledgeStore
  orchestrator: ChatOrchestrator
  lastAccess: number
}

const contexts = new Map<string, UserContext>()
let gcTimer: ReturnType<typeof setInterval> | null = null

function ensureGC() {
  if (gcTimer) return
  gcTimer = setInterval(() => {
    const now = Date.now()
    for (const [id, ctx] of contexts) {
      if (now - ctx.lastAccess > TTL_MS) {
        ctx.eventLog.close()
        contexts.delete(id)
      }
    }
  }, GC_INTERVAL_MS).unref()
}

export function getUserContext(userId: string): Omit<UserContext, 'lastAccess'> {
  ensureGC()
  const existing = contexts.get(userId)
  if (existing) { existing.lastAccess = Date.now(); return existing }
  const baseDir = path.join(process.env.TWIN_BASE_DIR || '.nexus/twins', userId)
  fs.mkdirSync(baseDir, { recursive: true })
  const eventLog = new EventLog(baseDir, userId)
  const facts = new FactsStore(baseDir)
  const episodes = new EpisodesStore(baseDir)
  const skills = new SkillsStore(baseDir)
  const knowledge = new KnowledgeStore(baseDir)
  const contracts = new ContractEngine()
  contracts.addRule({
    name: 'max_response_length',
    description: 'Response should not exceed 2000 tokens',
    check: (ctx) => {
      const est = Math.ceil(ctx.length / 4)
      return est > 2000 ? { passed: false, violations: [`Too long (${est} tokens)`], score: 0.5 } : { passed: true, violations: [], score: 1 }
    },
  })
  const ctx = { eventLog, facts, episodes, skills, knowledge, orchestrator: new ChatOrchestrator(eventLog, facts, episodes, skills, contracts), lastAccess: Date.now() }
  contexts.set(userId, ctx)
  return ctx
}
