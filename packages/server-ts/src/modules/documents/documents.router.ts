import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard.js'
import prisma from '../../common/prisma.js'
import { deepseekStream, deepseekChat, getApiKey } from '../../common/llm.js'
import crypto from 'crypto'
import { Document, Packer, Paragraph, TextRun } from 'docx'

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
    const existing = await (prisma as any).doc.findFirst({ where: { id: docId, userId: request.user!.userId } })
    if (!existing) return reply.status(404).send({ error: 'Document not found' })

    const now = new Date().toISOString()
    const data: any = { updatedAt: now }
    if (title !== undefined) data.title = title

    // Snapshot before body changes so users can restore previous versions.
    if (body !== undefined && body !== existing.body) {
      await (prisma as any).docSnapshot.create({
        data: {
          docId,
          userId: request.user!.userId,
          body: existing.body,
          label: 'Manual save',
          createdAt: now,
        },
      })
      data.body = body
    }

    await (prisma as any).doc.update({ where: { id: docId }, data })
    const doc = await (prisma as any).doc.findFirst({ where: { id: docId } })
    return { id: doc!.id, title: doc!.title, body: doc!.body, created_at: doc!.createdAt, updated_at: doc!.updatedAt }
  })

  app.delete('/api/v1/docs/:docId', async (request, reply) => {
    const { docId } = request.params as any
    const userId = request.user!.userId
    const existing = await (prisma as any).doc.findFirst({ where: { id: docId, userId } })
    if (!existing) return reply.status(404).send({ error: 'Document not found' })
    await (prisma as any).doc.delete({ where: { id: docId } })
    return { deleted: true }
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
    const suggestions: Record<string, string> = {
      SSN: 'Potential Social Security Number — consider removing or replacing with a surrogate ID.',
      Name: 'Potential patient name — consider using initials or a de-identified label.',
    }
    const findings: Array<{ kind: string; text: string; start: number; end: number; suggestion: string }> = []
    for (const { regex, kind } of [
      { regex: /\b\d{3}-\d{2}-\d{4}\b/g, kind: 'SSN' },
      { regex: /\b[A-Z][a-z]+ [A-Z][a-z]+\b/g, kind: 'Name' },
    ]) {
      let match
      while ((match = regex.exec(doc.body)) !== null) {
        findings.push({ kind, text: match[0], start: match.index, end: match.index + match[0].length, suggestion: suggestions[kind] || 'Review for potential PHI.' })
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

  // #3: Doc Chat SSE — structured output that can edit the document
  app.post('/api/v1/docs/:docId/chat', async (request, reply) => {
    const { docId } = request.params as any
    const { message } = request.body as any
    const userId = request.user!.userId
    const doc = await (prisma as any).doc.findFirst({ where: { id: docId, userId } })
    if (!doc) return reply.status(404).send({ error: 'Document not found' })

    const apiKey = getApiKey()
    reply.raw.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' })
    const send = (d: any) => reply.raw.write(`data: ${JSON.stringify(d)}\n\n`)

    try {
      send({ type: 'turn_started' })

      const structuredPrompt = `You are helping edit a clinical document titled "${doc.title}".

Current document content:
${doc.body}

User request: ${message || 'Help me with this document.'}

Respond using EXACTLY this format:

REPLY:
<your concise, helpful response to the user>

UPDATED_DOCUMENT:
<the complete updated document content>

Instructions:
- If the user wants you to modify the document, write the full new document content after UPDATED_DOCUMENT:.
- If no changes are needed, repeat the current document content exactly after UPDATED_DOCUMENT:.
- Do not wrap the document content in markdown code fences.
- The REPLY section should briefly explain what you changed or answer the user's question.`

      const fullResponse = await deepseekChat([
        { role: 'system' as const, content: 'You are a precise clinical document editor.' },
        { role: 'user' as const, content: structuredPrompt },
      ], apiKey)

      const parsed = parseDocChatResponse(fullResponse, doc.body)

      // Stream reply to client
      for (const chunk of chunkText(parsed.reply, 80)) {
        send({ type: 'reply_chunk', text: chunk })
      }

      let docBody: string | undefined
      if (parsed.updatedBody && parsed.updatedBody !== doc.body) {
        const now = new Date().toISOString()
        // Snapshot before AI edit
        await (prisma as any).docSnapshot.create({
          data: {
            docId,
            userId,
            body: doc.body,
            label: 'Before AI edit',
            createdAt: now,
          },
        })
        // Update document
        await (prisma as any).doc.update({
          where: { id: docId },
          data: { body: parsed.updatedBody, updatedAt: now },
        })
        docBody = parsed.updatedBody
      }

      // Persist chat messages
      const msgNow = new Date().toISOString()
      await (prisma as any).docChatMessage.create({
        data: { id: `dcm_${uid()}`, docId, userId, role: 'user', text: message || '', docApplied: 0, createdAt: msgNow },
      })
      await (prisma as any).docChatMessage.create({
        data: { id: `dcm_${uid()}`, docId, userId, role: 'assistant', text: parsed.reply, docApplied: docBody ? 1 : 0, createdAt: msgNow },
      })

      send({ type: 'done', doc_body: docBody })
    } catch (err: any) {
      send({ type: 'error', message: err.message || 'Chat failed' })
    } finally {
      reply.raw.end()
    }
  })

  app.post('/api/v1/docs/:docId/export', async (request, reply) => {
    const { docId } = request.params as any
    const userId = request.user!.userId
    const doc = await (prisma as any).doc.findFirst({ where: { id: docId, userId } })
    if (!doc) return reply.status(404).send({ error: 'Document not found' })

    const docx = new Document({
      sections: [{
        properties: {},
        children: [
          new Paragraph({
            children: [new TextRun({ text: doc.title || 'Untitled', bold: true, size: 32 })],
          }),
          new Paragraph({ children: [] }),
          ...(doc.body || '').split('\n').map((line: string) =>
            new Paragraph({ children: [new TextRun(line)] })
          ),
        ],
      }],
    })

    const buffer = await Packer.toBuffer(docx)
    return reply
      .header('Content-Type', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
      .header('Content-Disposition', `attachment; filename="${(doc.title || 'document').replace(/[^a-z0-9\u4e00-\u9fa5_-]/gi, '_')}.docx"`)
      .send(buffer)
  })

  // ── References ──
  app.post('/api/v1/docs/:docId/references', async (request, reply) => {
    const { docId } = request.params as any
    const userId = request.user!.userId
    const doc = await (prisma as any).doc.findFirst({ where: { id: docId, userId } })
    if (!doc) return reply.status(404).send({ error: 'Document not found' })

    const { kind, content, label, source_patient_hash } = request.body as any
    const id = `ref_${uid()}`
    const now = new Date().toISOString()
    await (prisma as any).docReference.create({
      data: {
        id,
        docId,
        userId,
        refType: kind || 'note',
        targetId: source_patient_hash || '',
        snapshot: content || '',
        sourceNodes: JSON.stringify({ label: label || '' }),
        granularity: 'doc',
        createdAt: now,
      },
    })
    return { reference_id: id, kind: kind || 'note', content: content || '', label: label || '', source_patient_hash: source_patient_hash || '', created_at: now }
  })

  app.get('/api/v1/docs/:docId/references', async (request, reply) => {
    const { docId } = request.params as any
    const userId = request.user!.userId
    const refs = await (prisma as any).docReference.findMany({
      where: { docId, userId },
      orderBy: { createdAt: 'desc' },
    })
    return {
      references: refs.map((r: any) => {
        let meta: any = {}
        try { meta = JSON.parse(r.sourceNodes || '{}') } catch { /* ignore */ }
        return {
          reference_id: r.id,
          kind: r.refType,
          content: r.snapshot,
          label: meta.label || '',
          source_patient_hash: r.targetId,
          created_at: r.createdAt,
        }
      }),
    }
  })
}

function parseDocChatResponse(response: string, currentBody: string): { reply: string; updatedBody: string } {
  const replyMatch = response.match(/REPLY:\s*([\s\S]*?)(?=UPDATED_DOCUMENT:|$)/)
  const docMatch = response.match(/UPDATED_DOCUMENT:\s*([\s\S]*?)$/)

  const reply = replyMatch ? replyMatch[1].trim() : response.trim()
  const updatedBody = docMatch ? docMatch[1].trim() : currentBody

  return { reply, updatedBody }
}

function chunkText(text: string, size: number): string[] {
  const chunks: string[] = []
  for (let i = 0; i < text.length; i += size) {
    chunks.push(text.slice(i, i + size))
  }
  return chunks
}
