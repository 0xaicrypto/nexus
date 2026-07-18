import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard.js'
import prisma from '../../common/prisma.js'
import { deepseekStream, getApiKey } from '../../common/llm.js'
import crypto from 'crypto'

function uid() { return crypto.randomBytes(8).toString('hex') }

export async function documentsRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  // ── Docs CRUD ──
  app.get('/api/v1/docs', async (request) => {
    const docs = await (prisma as any).doc.findMany({
      where: { userId: request.user!.userId }, orderBy: { updatedAt: 'desc' },
    })
    return { docs: docs.map((d: any) => ({
      id: d.id, title: d.title, body: d.body,
      updated_at: d.updatedAt, created_at: d.createdAt, ref_count: 0,
    }))}
  })

  app.post('/api/v1/docs', async (request) => {
    const { title } = request.body as any
    const id = `doc_${uid()}`
    const now = new Date().toISOString()
    await (prisma as any).doc.create({ data: { id, userId: request.user!.userId, title: title || 'Untitled', body: '', createdAt: now, updatedAt: now } })
    return { id, title: title || 'Untitled', body: '', created_at: now, updated_at: now }
  })

  app.get('/api/v1/docs/:docId', async (request, reply) => {
    const doc = await (prisma as any).doc.findFirst({ where: { id: (request.params as any).docId, userId: request.user!.userId } })
    if (!doc) return reply.status(404).send({ error: 'Not found' })
    return { id: doc.id, title: doc.title, body: doc.body, created_at: doc.createdAt, updated_at: doc.updatedAt }
  })

  app.put('/api/v1/docs/:docId', async (request, reply) => {
    const { docId } = request.params as any
    const { title, body } = request.body as any
    const data: any = { updatedAt: new Date().toISOString() }
    if (title !== undefined) data.title = title
    if (body !== undefined) data.body = body
    try {
      await (prisma as any).doc.update({ where: { id: docId }, data })
    } catch {
      return reply.status(404).send({ error: 'Document not found' })
    }
    const doc = await (prisma as any).doc.findFirst({ where: { id: docId } })
    return { id: doc!.id, title: doc!.title, body: doc!.body, created_at: doc!.createdAt, updated_at: doc!.updatedAt }
  })

  // ── Snapshots ──
  app.get('/api/v1/docs/:docId/snapshots', async (request) => {
    const snaps = await (prisma as any).docSnapshot.findMany({
      where: { docId: (request.params as any).docId }, orderBy: { id: 'desc' },
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

  // ── PHI Scan ──
  app.post('/api/v1/docs/:docId/phi-scan', async (request) => {
    const doc = await (prisma as any).doc.findFirst({ where: { id: (request.params as any).docId, userId: request.user!.userId } })
    if (!doc) return { findings: [] }
    const findings: Array<{ kind: string; text: string; start: number; end: number }> = []
    for (const { regex, kind } of [
      { regex: /\b\d{3}-\d{2}-\d{4}\b/g, kind: 'SSN' },
      { regex: /\b[A-Z][a-z]+ [A-Z][a-z]+\b/g, kind: 'Name' },
    ]) {
      let match
      while ((match = regex.exec(doc.body)) !== null) {
        findings.push({ kind, text: match[0], start: match.index, end: match.index + match[0].length })
      }
    }
    return { findings }
  })

  // #3: AI Polish SSE — uses DeepSeek
  app.post('/api/v1/docs/:docId/polish', async (request, reply) => {
    const { selection, instruction } = request.body as any
    const apiKey = getApiKey()
    const prompt = `Polish the following clinical text${instruction ? ` with instruction: "${instruction}"` : ''}. Keep the meaning but improve clarity and professionalism:\n\n${selection || ''}`
    reply.raw.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' })
    const send = (d: any) => reply.raw.write(`data: ${JSON.stringify(d)}\n\n`)
    try {
      for await (const chunk of deepseekStream([{ role: 'user', content: prompt }], apiKey)) {
        send({ text: chunk })
      }
      send({ done: true })
    } catch (err: any) {
      send({ type: 'error', message: err.message })
    } finally {
      reply.raw.end()
    }
  })

  // #3: Doc Chat SSE — uses DeepSeek, same pattern as main chat
  app.post('/api/v1/docs/:docId/chat', async (request, reply) => {
    const { docId } = request.params as any
    const { message } = request.body as any
    const doc = await (prisma as any).doc.findFirst({ where: { id: docId, userId: request.user!.userId } })
    const apiKey = getApiKey()
    reply.raw.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' })
    const send = (d: any) => reply.raw.write(`data: ${JSON.stringify(d)}\n\n`)
    try {
      send({ type: 'turn_started' })
      const systemPrompt = doc
        ? `You are helping edit a clinical document titled "${doc.title}". Current content:\n\n${doc.body}\n\nAnswer questions about this document and suggest edits.`
        : 'You are a clinical document assistant.'
      const messages = [
        { role: 'system' as const, content: systemPrompt },
        { role: 'user' as const, content: message || 'Help me with this document.' },
      ]
      for await (const chunk of deepseekStream(messages, apiKey)) {
        send({ type: 'reply_chunk', text: chunk })
      }
      send({ type: 'done' })
    } catch (err: any) {
      send({ type: 'error', message: err.message })
    } finally {
      reply.raw.end()
    }
  })

  app.post('/api/v1/docs/:docId/export', async (_req, reply) => {
    return reply.status(501).send({ error: 'DOCX export requires Python worker' })
  })
}
