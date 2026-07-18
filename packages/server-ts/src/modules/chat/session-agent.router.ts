import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard'
import prisma from '../../common/prisma'
import { getUserContext } from './user-context.js'

export async function sessionRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  app.get('/api/v1/sessions', async (request) => {
    const includeArchived = (request.query as any).include_archived === '1'
    const rows = await prisma.session.findMany({
      where: { userId: request.user!.userId, archived: includeArchived ? undefined : 0 },
      orderBy: { lastMessageAt: 'desc' },
    })
    return {
      sessions: rows.map(s => ({
        id: s.id, title: s.title,
        created_at: s.createdAt, updated_at: s.lastMessageAt,
        archived: s.archived === 1, message_count: s.messageCount,
      })),
    }
  })

  app.post('/api/v1/sessions', async (request) => {
    const { title } = request.body as any
    const id = `session_${Math.random().toString(36).slice(2, 10)}`
    const now = new Date().toISOString()
    await prisma.session.create({ data: { id, userId: request.user!.userId, title: title || 'New Session', createdAt: now } })
    return { id, title: title || 'New Session', created_at: now, message_count: 0, archived: false }
  })

  app.delete('/api/v1/sessions/:sessionId', async (request) => {
    await prisma.session.deleteMany({ where: { id: (request.params as any).sessionId, userId: request.user!.userId } })
    return {}
  })
}

export async function agentRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  app.get('/api/v1/agent/state', async (request) => {
    const ctx = getUserContext(request.user!.userId)
    return {
      user_id: request.user!.userId,
      on_chain: false,
      memory_count: ctx.facts.all().length,
      episode_count: ctx.episodes.all().length,
      skill_count: ctx.skills.all().filter((s: any) => s.successCount > 0).length,
      anchored_count: 0, pending_anchor_count: 0, failed_anchor_count: 0, total_anchor_count: 0,
      server_time: new Date().toISOString(),
    }
  })

  // Timeline — grouped conversation turns + evolution events
  app.get('/api/v1/agent/timeline', async (request) => {
    const ctx = getUserContext(request.user!.userId)
    const limit = parseInt((request.query as any).limit || '20')
    const all = ctx.eventLog.query({ limit: 200 }).reverse()

    const items: Array<{ kind: string; timestamp: string; summary: string; sync_id: string }> = []

    // 1. Group user+assistant into conversation turns
    let currentTurn: { user?: typeof all[0]; assistant?: typeof all[0] } = {}
    for (const evt of all) {
      if (evt.eventType === 'user_message') {
        if (currentTurn.user) { currentTurn = {} }
        currentTurn.user = evt
      } else if (evt.eventType === 'assistant_response' && currentTurn.user) {
        currentTurn.assistant = evt
        // Create a conversation turn entry
        const summary = currentTurn.user.content.slice(0, 80)
        items.push({
          kind: 'conversation',
          timestamp: new Date(currentTurn.assistant.timestamp * 1000).toISOString(),
          summary: summary + (summary.length >= 80 ? '...' : ''),
          sync_id: `turn_${currentTurn.assistant.idx}`,
        })
        currentTurn = {}
      }
    }

    // 2. Add episode summaries (one per session)
    const episodes = ctx.episodes.all().slice(-5)
    for (const ep of episodes) {
      items.push({
        kind: 'session_summary',
        timestamp: new Date(ep.createdAt).toISOString(),
        summary: `📝 ${ep.summary.slice(0, 100)}`,
        sync_id: `ep_${ep.sessionId}`,
      })
    }

    // 3. Evolution events from event log
    const evolutionEvents = all.filter(e => e.eventType === 'evolution')
    for (const evt of evolutionEvents) {
      items.push({
        kind: 'evolution',
        timestamp: new Date(evt.timestamp * 1000).toISOString(),
        summary: evt.content,
        sync_id: `evo_${evt.idx}`,
      })
    }

    // 4. Overall status
    const facts = ctx.facts.all()
    if (facts.length > 0) {
      items.push({
        kind: 'evolution',
        timestamp: new Date().toISOString(),
        summary: `🧠 ${facts.length} facts accumulated across ${ctx.episodes.all().length} sessions`,
        sync_id: 'evolution_status',
      })
    }

    // Sort by time, newest first, keep only most recent
    items.sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime())

    return { items: items.slice(0, limit) }
  })

  app.get('/api/v1/agent/messages', async (request) => {
    const ctx = getUserContext(request.user!.userId)
    const sessionId = (request.query as any).session_id
    const events = ctx.eventLog.query({ sessionId, limit: parseInt((request.query as any).limit || '100') }).reverse()
    return {
      messages: events.map(e => ({
        role: e.eventType === 'user_message' ? 'user' : 'assistant',
        content: e.content,
        timestamp: new Date(e.timestamp * 1000).toISOString(),
        sync_id: String(e.idx), metadata: e.metadata,
      })),
      total: events.length,
    }
  })
}
