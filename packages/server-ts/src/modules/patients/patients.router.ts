import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard.js'
import prisma from '../../common/prisma.js'
import crypto from 'crypto'
import fs from 'fs'
import path from 'path'
import { quickScanDicom, renderDicomSlice } from './dicom-scanner.js'
import { getUserContext } from '../chat/user-context.js'

function uid() { return crypto.randomBytes(8).toString('hex') }

export async function patientsRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  // ── List patients (frontend: /api/v1/dicom/patients/full) ──
  app.get('/api/v1/dicom/patients/full', async (request) => {
    const records = await (prisma as any).patientRecord.findMany({
      where: { userId: request.user!.userId },
      orderBy: { createdAt: 'desc' },
    })
    return records.map((r: any) => ({
      patient_hash: r.hash,
      initials: r.initials,
      age_value: r.age || undefined,
      age_group: r.age ? (r.age < 18 ? 'pediatric' : r.age > 65 ? 'geriatric' : 'adult') : undefined,
      sex: r.sex || undefined,
      chief_complaint: r.chiefComplaint || undefined,
      created_at: r.createdAt,
      study_count: 0,
      source: r.source || 'manual',
    }))
  })

  // ── Patient detail ──
  app.get('/api/v1/dicom/patients/:hash/detail', async (request, reply) => {
    const { hash } = request.params as any
    const r = await (prisma as any).patientRecord.findFirst({ where: { hash, userId: request.user!.userId } })
    if (!r) return reply.status(404).send({ error: 'Patient not found' })
    return {
      patient_hash: r.hash, initials: r.initials,
      age_value: r.age || undefined, sex: r.sex || undefined,
      chief_complaint: r.chiefComplaint || undefined,
      created_at: r.createdAt, updated_at: r.updatedAt,
      study_count: 0,
    }
  })

  // ── Register manual ──
  app.post('/api/v1/dicom/patients/register-manual', async (request) => {
    const body = request.body as any
    const hash = `patient_${uid()}`
    const now = new Date().toISOString()
    await (prisma as any).patientRecord.create({
      data: {
        hash, userId: request.user!.userId,
        initials: body.initials || '',
        age: body.age || 0,
        sex: body.sex || '',
        chiefComplaint: body.chief_complaint || '',
        source: 'manual', createdAt: now, updatedAt: now,
      },
    })
    return { patient_hash: hash, initials: body.initials, created_at: now }
  })

  // ── Delete ──
  app.delete('/api/v1/dicom/patients/:hash', async (request) => {
    const { hash } = request.params as any
    await (prisma as any).patientRecord.deleteMany({ where: { hash, userId: request.user!.userId } })
    return { deleted: true }
  })

  // ── Studies (stub) ──
  app.get('/api/v1/dicom/patients/:patientHash/studies', async (request) => {
    // Return uploaded files as DICOM studies for this patient
    const dir = path.join(process.env.TWIN_BASE_DIR || '.nexus/twins', (request as any).user?.userId || '', 'uploads')
    const files: Array<{study_id: string; modality: string; series_count: number; created_at: string}> = []
    if (fs.existsSync(dir)) {
      for (const f of fs.readdirSync(dir)) {
        if (f.endsWith('.dcm')) {
          files.push({
            study_id: f.replace('.dcm', ''),
            modality: 'CT',
            series_count: 1,
            created_at: new Date().toISOString(),
          })
        }
      }
    }
    // Frontend expects a bare array, not { studies: [...] }
    return files
  })

  // Study detail with series info for DICOM viewer
  app.get('/api/v1/dicom/studies/:studyId', async (request) => {
    const studyId = (request.params as any).studyId
    const findings = quickScanDicom(request.user!.userId, studyId)

    // Extract info from DICOM findings
    const studyFinding = findings.find(f => f.type === 'study')
    const imageFinding = findings.find(f => f.type === 'image')

    const rowsCols = imageFinding?.content.match(/(\d+)x(\d+)/)
    const sliceCount = 1 // Single slice DICOM
    const seriesCount = 1

    return {
      study_id: studyId,
      modality: 'CT',
      body_part: 'CHEST',
      series_count: seriesCount,
      slice_count: sliceCount,
      created_at: new Date().toISOString(),
      series: [{
        series_uid: `${studyId}_series_0`,
        series_description: studyFinding?.content || 'CT Series',
        slice_count: sliceCount,
        rows: rowsCols ? parseInt(rowsCols[1]) : 512,
        cols: rowsCols ? parseInt(rowsCols[2]) : 512,
      }],
    }
  })

  app.get('/api/v1/dicom/studies/:studyId/series/:seriesIdx/render', async (request, reply) => {
    const studyId = (request.params as any).studyId
    const bmp = renderDicomSlice(request.user!.userId, studyId)
    if (bmp) {
      reply.header('Content-Type', 'image/bmp')
      reply.header('Cache-Control', 'public, max-age=3600')
      return bmp
    }
    reply.header('Content-Type', 'image/png')
    return Buffer.alloc(1)
  })

  // #2: Quick Scan + update patient profile
  app.post('/api/v1/dicom/studies/:studyId/quick-scan', async (request) => {
    const studyId = (request.params as any).studyId
    const userId = request.user!.userId
    const findings = quickScanDicom(userId, studyId)

    // Update patient with scan findings
    const text = findings.filter((f: any) => f.type !== 'meta' && f.type !== 'error')
      .map((f: any) => f.content).join(' | ')
    if (text && text.length > 5) {
      const allPatients = await (prisma as any).patientRecord.findMany({ where: { userId }, orderBy: { createdAt: 'desc' }, take: 1 })
      if (allPatients.length > 0) {
        const existing = allPatients[0].chiefComplaint || ''
        if (!existing.includes(text.slice(0, 50))) {
          await (prisma as any).patientRecord.update({
            where: { hash: allPatients[0].hash },
            data: { chiefComplaint: (existing + '\n[Scan] ' + text.slice(0, 300)).trim(), updatedAt: new Date().toISOString() }
          })
        }
      }
    }

    return { ok: true, findings, study_id: studyId }
  })

  app.post('/api/v1/dicom/send-to-agent', async (request) => {
    return { ok: true }
  })

  // ── Memory projection — aggregate findings from patient data
  app.get('/api/v1/memory/patient/:patientHash/projection', async (request) => {
    const { patientHash } = request.params as any
    const userId = request.user!.userId

    // Get patient record
    const patient = await (prisma as any).patientRecord.findFirst({ where: { hash: patientHash, userId } })
    if (!patient) return { findings: [], medications: [], timeline: [] }

    // Parse findings from chief_complaint
    const complaint = patient.chiefComplaint || ''
    const findings: Array<{ node_id: string; node_type: string; content: string }> = []
    const medications: Array<{ node_id: string; node_type: string; content: string }> = []
    const timeline: Array<{ event_id: string; event_type: string; content: string; timestamp: string }> = []

    if (complaint) {
      // Extract [diagnosis], [imaging], [lab_result] etc. from the complaint text
      const tags = complaint.match(/\[(\w+)\]\s*([^\[\]]+)/g) || []
      for (const tag of tags) {
        const m = tag.match(/\[(\w+)\]\s*(.+)/)
        if (!m) continue
        const [, type, content] = m
        if (type === 'medication') {
          medications.push({ node_id: `med_${medications.length}`, node_type: 'medication', content: content.trim() })
        } else {
          findings.push({ node_id: `f_${findings.length}`, node_type: type, content: content.trim() })
        }
      }
      // Timeline entry for creation
      timeline.push({
        event_id: 'create', event_type: 'patient_created',
        content: `Patient profile updated with ${findings.length} findings`,
        timestamp: patient.createdAt,
      })
    }

    // Also get chat events for this patient
    const ctx = getUserContext(userId)
    const events = ctx.eventLog.query({ limit: 50 })
    for (const evt of events) {
      if (evt.metadata?.patientHash === patientHash || evt.content?.includes(patientHash)) {
        timeline.push({
          event_id: `evt_${evt.idx}`,
          event_type: evt.eventType,
          content: evt.content.slice(0, 100),
          timestamp: new Date(evt.timestamp * 1000).toISOString(),
        })
      }
    }

    return { findings, medications, timeline }
  })

  app.get('/api/v1/memory/patient/:patientHash/findings', async (request) => {
    const proj = await getPatientProjection(request)
    return { findings: proj.findings }
  })

  app.get('/api/v1/memory/patient/:patientHash/timeline', async (request) => {
    const proj = await getPatientProjection(request)
    return { entries: proj.timeline }
  })

  async function getPatientProjection(request: any) {
    const { patientHash } = request.params as any
    const userId = request.user!.userId
    const patient = await (prisma as any).patientRecord.findFirst({ where: { hash: patientHash, userId } })
    if (!patient) return { findings: [], timeline: [] }
    const complaint = patient.chiefComplaint || ''
    const findings: Array<{ node_id: string; node_type: string; content: string }> = []
    const tags = complaint.match(/\[(\w+)\]\s*([^\[\]]+)/g) || []
    for (const tag of tags) {
      const m = tag.match(/\[(\w+)\]\s*(.+)/)
      if (!m) continue
      findings.push({ node_id: `f_${findings.length}`, node_type: m[1], content: m[2].trim() })
    }
    const ctx = getUserContext(userId)
    const events = ctx.eventLog.query({ limit: 50 })
    const timeline = events
      .filter((e: any) => e.metadata?.patientHash === patientHash)
      .map((e: any) => ({
        event_id: `evt_${e.idx}`,
        event_type: e.eventType,
        content: e.content.slice(0, 100),
        timestamp: new Date(e.timestamp * 1000).toISOString(),
      }))
    return { findings, timeline }
  }
}
