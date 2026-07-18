import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard'
import prisma from '../../common/prisma'
import { getUserContext } from './user-context.js'

export async function chatRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  app.post('/api/v1/agent/chat', async (request, reply) => {
    const body = request.body as any
    if (!body.text) return reply.status(400).send({ error: 'text required' })

    const userId = request.user!.userId
    const ctx = getUserContext(userId)
    const sid = body.session_id || `session_${Math.random().toString(36).slice(2, 10)}`
    const patientHash = body.patient_hash || null

    reply.raw.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' })
    const send = (d: unknown) => reply.raw.write(`data: ${JSON.stringify(d)}\n\n`)

    try {
      const llmCall = async (system: string, msg: string) => {
        send({ type: 'reasoning_chunk', text: 'Analyzing...' })
        send({ type: 'context_info', text: `Context: ~${Math.ceil(system.length / 4)} tokens`, kind: 'budget' })
        const facts = ctx.facts.all()
        return `[Heurion] I have ${facts.length} accumulated facts and ${ctx.episodes.all().length} session episodes in my memory. How can I help you?`
      }

      send({ type: 'turn_started', event_idx: ctx.eventLog.count() + 1, patient_hash: patientHash })
      const result = await ctx.orchestrator.turn({
        userId, message: body.text, sessionId: sid, patientHash,
        persona: 'You are Heurion, a clinical AI assistant for oncology research. Be concise and evidence-based.',
        llmCall,
      })

      send({ type: 'context_info', text: result.budget.map((b: any) => `${b.layer}: ${b.tokens}t`).join(' | '), kind: 'projection' })
      send({ type: 'final_answer_chunk', text: result.response })

      await prisma.session.upsert({
        where: { id: sid },
        update: { lastMessageAt: new Date().toISOString(), messageCount: { increment: 1 } },
        create: { id: sid, userId, title: body.text.slice(0, 50), createdAt: new Date().toISOString() },
      })

      send({ type: 'turn_complete', assistant_event_idx: ctx.eventLog.count() })
      ctx.orchestrator.postTurn(userId, sid, body.text).catch(() => {})
    } catch (err: any) {
      send({ type: 'error', message: err.message || 'Chat failed' })
    } finally {
      reply.raw.end()
    }
  })

  app.get('/api/v1/chat/projection', async (request) => {
    const ctx = getUserContext(request.user!.userId)
    const result = await ctx.orchestrator['projection'].project({
      userId: request.user!.userId, patientHash: null, sessionId: 'debug',
      persona: 'debug', facts: ctx.facts.all(), episodes: ctx.episodes.all(), skills: ctx.skills.all(),
    })
    return result
  })
}
