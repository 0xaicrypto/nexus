import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard'
import prisma from '../../common/prisma'
import { getUserContext } from './user-context.js'
import { deepseekStream, getApiKey } from '../../common/llm.js'
import { analyzeChatForPatient, updatePatientFromFindings } from '../patients/clinical-analysis.js'
import fs from 'fs'
import path from 'path'

// #2: Read uploaded file content for chat context
function readAttachmentContent(userId: string, fileId: string): string {
  const dir = path.join(process.env.TWIN_BASE_DIR || '.nexus/twins', userId, 'uploads')
  const filepath = path.join(dir, fileId)
  if (!fs.existsSync(filepath)) return ''
  const buffer = fs.readFileSync(filepath)
  const text = buffer.toString('utf-8').slice(0, 5000)  // 5KB limit
  const name = (fileId.split('_').slice(1).join('_') || fileId)
  return `\n[ATTACHMENT: ${name}]\n${text}\n[/ATTACHMENT]\n`
}

export async function chatRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  app.post('/api/v1/agent/chat', async (request, reply) => {
    const body = request.body as any
    if (!body.text) return reply.status(400).send({ error: 'text required' })

    const userId = request.user!.userId
    const ctx = getUserContext(userId)
    const sid = body.session_id || `session_${Math.random().toString(36).slice(2, 10)}`
    const patientHash = body.patient_hash || null
    const apiKey = getApiKey()

    reply.raw.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' })
    const send = (d: unknown) => reply.raw.write(`data: ${JSON.stringify(d)}\n\n`)

    try {
      send({ type: 'turn_started', event_idx: ctx.eventLog.count() + 1, patient_hash: patientHash })

      // #2: Read attachment content
      let attachmentText = ''
      const rawAttachments = body.attachments || []
      for (const att of rawAttachments) {
        // Accept both string (file_id) and object ({ file_id, fileId, name })
        const fid = typeof att === 'string' ? att : (att.file_id || att.fileId || '')
        const name = typeof att === 'string' ? fid.split('_').slice(1).join('_') : (att.name || '')
        if (fid) {
          const content = readAttachmentContent(userId, fid)
          if (content) {
            attachmentText += content
            send({ type: 'context_info', text: `Attachment: ${name.slice(0, 30)}`, kind: 'attachment' })
          }
        }
      }

      let fullMessage = attachmentText ? `${attachmentText}\n\nUser query: ${body.text}` : body.text

      // Inject patient demographics into chat context
      if (patientHash) {
        const patient = await (prisma as any).patientRecord.findFirst({
          where: { hash: patientHash, userId },
        })
        if (patient) {
          const parts: string[] = ['## Current Patient Context']
          if (patient.initials) parts.push(`- Name: ${patient.initials}`)
          if (patient.age) parts.push(`- Age: ${patient.age}`)
          if (patient.sex) parts.push(`- Sex: ${patient.sex}`)
          if (patient.chiefComplaint) parts.push(`- Chief Complaint: ${patient.chiefComplaint}`)
          if (parts.length > 1) {
            send({ type: 'context_info', text: parts.join('\n'), kind: 'patient_context' })
            fullMessage = parts.join('\n') + '\n\n' + fullMessage
          }
        }
      }

      // Always include patient roster so AI knows the user's patient list
      const allPatients = await (prisma as any).patientRecord.findMany({
        where: { userId },
        orderBy: { createdAt: 'desc' },
        take: 50,
      })
      if (allPatients.length > 0) {
        const roster = allPatients.map((p: any) => {
          const parts = [`- ${p.initials || 'Unknown'}`]
          if (p.age) parts.push(`${p.age}y/o`)
          if (p.sex) parts.push(p.sex)
          if (p.chiefComplaint) parts.push(`CC: ${p.chiefComplaint}`)
          return parts.join(', ')
        }).join('\n')
        send({ type: 'context_info', text: `## Patient Roster (${allPatients.length} patients)\n${roster}`, kind: 'patient_roster' })
        fullMessage = `## Patient Roster (${allPatients.length} patients)\n${roster}\n\n` + fullMessage
      } else {
        fullMessage = '## Patient Roster\nNo patients registered yet.\n\n' + fullMessage
      }

      // #2: Weighted attention context projection
      const projected = await ctx.orchestrator['projection'].project({
        userId, patientHash, sessionId: sid,
        persona: 'You are Heurion, a clinical AI assistant for oncology research. Be concise, evidence-based, and reference relevant patient data and accumulated knowledge. Only reference patients that appear in the Patient Roster above. Do not invent or hallucinate patient names, diagnoses, or clinical details.',
        facts: ctx.facts.all(), episodes: ctx.episodes.all(), skills: ctx.skills.all(),
      })
      send({ type: 'context_info', text: projected.budget.map((b: any) => `${b.layer}: ${b.tokens}t/${b.items}i`).join(' | '), kind: 'projection' })

      // #5: Include research study context
      const studies = await (prisma as any).researchStudy.findMany({
        where: { userId },
        orderBy: { updatedAt: 'desc' },
        take: 10,
      })
      let studyContext = ''
      if (studies.length > 0) {
        studyContext = '\n## Active Research Studies\n'
        for (const s of studies) {
          studyContext += `- **${s.shortCode}**: ${s.name}\n`
          if (s.protocol) {
            studyContext += `  Protocol snippet: ${s.protocol.slice(0, 500).replace(/\n/g, ' ')}\n`
          }
        }
        studyContext += '\nWhen asked about studies, reference these. When asked about details not in context, suggest importing the full protocol.\n'
      }

      // #5: Conversation history from event log (last 20 turns)
      const history = ctx.eventLog.query({ sessionId: sid, limit: 40 }).reverse()
      const messages: Array<{ role: 'system' | 'user' | 'assistant'; content: string }> = [
        { role: 'system', content: projected.systemPrompt + studyContext },
      ]
      for (const evt of history) {
        if (evt.eventType === 'user_message') messages.push({ role: 'user', content: evt.content })
        else if (evt.eventType === 'assistant_response') messages.push({ role: 'assistant', content: evt.content })
      }
      messages.push({ role: 'user' as const, content: fullMessage })

      // Stream response
      let fullResponse = ''
      send({ type: 'reasoning_chunk', text: 'Thinking...' })

      for await (const chunk of deepseekStream(messages, apiKey)) {
        fullResponse += chunk
        send({ type: 'final_answer_chunk', text: chunk })
      }

      // Log to event log
      ctx.eventLog.append({
        timestamp: Date.now() / 1000, eventType: 'user_message', content: body.text,
        metadata: { patientHash }, agentId: userId, sessionId: sid,
      })
      ctx.eventLog.append({
        timestamp: Date.now() / 1000, eventType: 'assistant_response', content: fullResponse,
        metadata: {}, agentId: userId, sessionId: sid,
      })

      // #2: Extract takeaway + evolve facts + analyze patient chat
      ctx.orchestrator.postTurn(userId, sid, body.text).catch(() => {})

      // Step 2: Analyze patient chat + attachments for clinical findings
      if (patientHash) {
        const analysisText = attachmentText
          ? `[FILE CONTENT]\n${attachmentText}\n[CHAT]\nUser: ${body.text}\nAI: ${fullResponse}`
          : `User: ${body.text}\nAI: ${fullResponse}`
        analyzeChatForPatient(userId, patientHash, analysisText)
          .then(findings => updatePatientFromFindings(userId, patientHash, findings))
          .catch(() => {})
      }

      // Update session
      await prisma.session.upsert({
        where: { id: sid },
        update: { lastMessageAt: new Date().toISOString(), messageCount: { increment: 1 } },
        create: { id: sid, userId, title: body.text.slice(0, 50), createdAt: new Date().toISOString() },
      })

      send({ type: 'citations', items: [] })
      send({ type: 'turn_complete', assistant_event_idx: ctx.eventLog.count() })
    } catch (err: any) {
      send({ type: 'error', message: err.message || 'Chat failed' })
    } finally {
      reply.raw.end()
    }
  })

  // #6: Memory export
  app.get('/api/v1/memory/export', async (request, reply) => {
    const ctx = getUserContext(request.user!.userId)
    reply.header('Content-Type', 'application/json')
    reply.header('Content-Disposition', 'attachment; filename="heurion-memory.json"')
    return {
      exported_at: new Date().toISOString(),
      facts: ctx.facts.all(),
      episodes: ctx.episodes.all(),
      skills: ctx.skills.all(),
      event_log_count: ctx.eventLog.count(),
    }
  })

  // #6: Memory import
  app.post('/api/v1/memory/import', async (request, reply) => {
    const ctx = getUserContext(request.user!.userId)
    const data = request.body as any
    if (!data) return reply.status(400).send({ error: 'No data provided' })
    let imported = 0
    if (data.facts && Array.isArray(data.facts)) {
      for (const f of data.facts) { ctx.facts.add(f); imported++ }
    }
    if (data.episodes && Array.isArray(data.episodes)) {
      for (const e of data.episodes) { ctx.episodes.upsert(e.sessionId || '', e.summary || '', e.turnCount || 0); imported++ }
    }
    ctx.facts.commit()
    ctx.episodes.commit()
    return { imported, facts_count: ctx.facts.all().length, episodes_count: ctx.episodes.all().length }
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
