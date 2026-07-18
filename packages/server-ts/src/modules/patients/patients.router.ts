import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard.js'
import prisma from '../../common/prisma.js'
import crypto from 'crypto'
import fs from 'fs'
import path from 'path'
import { quickScanDicom, renderDicomSlice } from './dicom-scanner.js'

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

  app.get('/api/v1/dicom/studies/:studyId', async (request) => {
    return { study_id: (request.params as any).studyId, patient_hash: '', modality: 'CT', series: [] }
  })

  app.get('/api/v1/dicom/studies/:studyId/series/:seriesIdx/render', async (request, reply) => {
    const studyId = (request.params as any).studyId
    const png = await renderDicomSlice(request.user!.userId, studyId)
    if (png) {
      reply.header('Content-Type', 'image/png')
      reply.header('Cache-Control', 'public, max-age=3600')
      return png
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

  // ── Memory ──
  app.get('/api/v1/memory/patient/:patientHash/projection', async () => ({ findings: [], medications: [], timeline: [] }))
  app.get('/api/v1/memory/patient/:patientHash/findings', async () => ({ findings: [] }))
  app.get('/api/v1/memory/patient/:patientHash/timeline', async () => ({ entries: [] }))
}
