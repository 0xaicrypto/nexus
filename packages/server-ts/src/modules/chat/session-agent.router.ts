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
      anchored_count: 0, pending_anchor_count: 0, failed_anchor_count: 0, total_anchor_count: 0,
      server_time: new Date().toISOString(),
    }
  })

  app.get('/api/v1/agent/timeline', async (request) => {
    const ctx = getUserContext(request.user!.userId)
    const limit = parseInt((request.query as any).limit || '20')
    return {
      items: ctx.eventLog.query({ limit }).map(e => ({
        kind: e.eventType,
        timestamp: new Date(e.timestamp * 1000).toISOString(),
        summary: e.content.slice(0, 100),
        sync_id: String(e.idx), metadata: e.metadata,
      })),
    }
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
