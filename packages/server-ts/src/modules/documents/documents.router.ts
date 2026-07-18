import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard.js'
import prisma from '../../common/prisma.js'
import crypto from 'crypto'

function uid() { return crypto.randomBytes(8).toString('hex') }

export async function documentsRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  // ── List (matches frontend: /api/v1/docs → { docs: [...] }) ──
  app.get('/api/v1/docs', async (request) => {
    const docs = await (prisma as any).doc.findMany({
      where: { userId: request.user!.userId },
      orderBy: { updatedAt: 'desc' },
    })
    return {
      docs: docs.map((d: any) => ({
        id: d.id,
        title: d.title,
        body: d.body,
        updated_at: d.updatedAt,
        created_at: d.createdAt,
        ref_count: 0,
      })),
    }
  })

  // ── Create ──
  app.post('/api/v1/docs', async (request) => {
    const { title } = request.body as any
    const id = `doc_${uid()}`
    const now = new Date().toISOString()
    await (prisma as any).doc.create({
      data: { id, userId: request.user!.userId, title: title || 'Untitled', body: '', createdAt: now, updatedAt: now },
    })
    return { id, title: title || 'Untitled', body: '', created_at: now, updated_at: now }
  })

  // ── Get ──
  app.get('/api/v1/docs/:docId', async (request, reply) => {
    const { docId } = request.params as any
    const doc = await (prisma as any).doc.findFirst({ where: { id: docId, userId: request.user!.userId } })
    if (!doc) return reply.status(404).send({ error: 'Not found' })
    return { id: doc.id, title: doc.title, body: doc.body, created_at: doc.createdAt, updated_at: doc.updatedAt }
  })

  // ── Update ──
  app.put('/api/v1/docs/:docId', async (request, reply) => {
    const { docId } = request.params as any
    const { title, body } = request.body as any
    const data: any = { updatedAt: new Date().toISOString() }
    if (title !== undefined) data.title = title
    if (body !== undefined) data.body = body
    await (prisma as any).doc.update({ where: { id: docId }, data })
    const doc = await (prisma as any).doc.findFirst({ where: { id: docId, userId: request.user!.userId } })
    return { id: doc!.id, title: doc!.title, body: doc!.body, created_at: doc!.createdAt, updated_at: doc!.updatedAt }
  })

  // ── Snapshots ──
  app.get('/api/v1/docs/:docId/snapshots', async (request) => {
    const { docId } = request.params as any
    const snaps = await (prisma as any).docSnapshot.findMany({
      where: { docId, userId: request.user!.userId }, orderBy: { id: 'desc' },
    })
    return { snapshots: snaps.map((s: any) => ({ id: s.id, body: s.body, label: s.label, created_at: s.createdAt })) }
  })

  app.post('/api/v1/docs/:docId/snapshots/:snapId/restore', async (request, reply) => {
    const { docId, snapId } = request.params as any
    const snap = await (prisma as any).docSnapshot.findFirst({ where: { id: Number(snapId), docId } })
    if (!snap) return reply.status(404).send({ error: 'Not found' })
    await (prisma as any).doc.update({ where: { id: docId }, data: { body: snap.body, updatedAt: new Date().toISOString() } })
    return { restored: true }
  })

  // ── PHI Scan (mock) ──
  app.post('/api/v1/docs/:docId/phi-scan', async (request) => {
    const { docId } = request.params as any
    const doc = await (prisma as any).doc.findFirst({ where: { id: docId, userId: request.user!.userId } })
    if (!doc) return { findings: [] }
    const findings: Array<{ kind: string; text: string; start: number; end: number }> = []
    const patterns = [
      { regex: /\b\d{3}-\d{2}-\d{4}\b/g, kind: 'SSN' },
      { regex: /\b[A-Z][a-z]+ [A-Z][a-z]+\b/g, kind: 'Name' },
    ]
    for (const { regex, kind } of patterns) {
      let match
      while ((match = regex.exec(doc.body)) !== null) {
        findings.push({ kind, text: match[0], start: match.index, end: match.index + match[0].length })
      }
    }
    return { findings }
  })

  // ── AI Polish SSE (mock) ──
  app.post('/api/v1/docs/:docId/polish', async (request, reply) => {
    const { selection, instruction } = request.body as any
    reply.raw.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' })
    const send = (d: any) => reply.raw.write(`data: ${JSON.stringify(d)}\n\n`)
    send({ type: 'polish_chunk', text: `\n\n[Polished: ${instruction || 'improve clarity'}]\n${(selection || '').slice(0, 200)}...` })
    send({ type: 'polish_complete' })
    reply.raw.end()
  })

  // ── Doc Chat SSE (mock) ──
  app.post('/api/v1/docs/:docId/chat', async (request, reply) => {
    const { message } = request.body as any
    reply.raw.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' })
    const send = (d: any) => reply.raw.write(`data: ${JSON.stringify(d)}\n\n`)
    send({ type: 'final_answer_chunk', text: `[DocChat] Re: "${(message || '').slice(0, 100)}" — LLM integration coming.` })
    send({ type: 'turn_complete' })
    reply.raw.end()
  })

  // ── Export DOCX ──
  app.post('/api/v1/docs/:docId/export', async (request, reply) => {
    return { error: 'DOCX export requires Python worker' }
  })
}
