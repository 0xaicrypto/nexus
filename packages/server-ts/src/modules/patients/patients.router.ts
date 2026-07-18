import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard.js'

export async function patientsRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  // ── DICOM patients list (frontend: /api/v1/dicom/patients/full) ──
  app.get('/api/v1/dicom/patients/full', async (request) => {
    return []  // no patients yet — data lives in Python event-sourcing
  })

  // ── DICOM patient detail ──
  app.get('/api/v1/dicom/patients/:hash/detail', async (request) => {
    const { hash } = request.params as any
    return {
      patient_hash: hash,
      initials: 'PT',
      created_at: new Date().toISOString(),
      study_count: 0,
    }
  })

  // ── DICOM patient delete ──
  app.delete('/api/v1/dicom/patients/:hash', async (request) => {
    return { deleted: true }
  })

  // ── DICOM patient studies ──
  app.get('/api/v1/dicom/patients/:patientHash/studies', async () => {
    return { studies: [] }
  })

  // ── DICOM study detail ──
  app.get('/api/v1/dicom/studies/:studyId', async (request) => {
    const { studyId } = request.params as any
    return { study_id: studyId, patient_hash: '', modality: 'CT', series: [] }
  })

  // ── DICOM render slice (stub) ──
  app.get('/api/v1/dicom/studies/:studyId/series/:seriesIdx/render', async (request, reply) => {
    reply.header('Content-Type', 'image/png')
    return Buffer.alloc(1)
  })

  // ── Memory projection (frontend: /api/v1/memory/patient/:hash/projection) ──
  app.get('/api/v1/memory/patient/:patientHash/projection', async (request) => {
    return { findings: [], medications: [], timeline: [] }
  })

  // ── Memory findings ──
  app.get('/api/v1/memory/patient/:patientHash/findings', async () => {
    return { findings: [] }
  })

  // ── Memory timeline ──
  app.get('/api/v1/memory/patient/:patientHash/timeline', async () => {
    return { entries: [] }
  })

  // ── Manual patient register ──
  app.post('/api/v1/dicom/patients/register-manual', async (request) => {
    const body = request.body as any
    const hash = `patient_${Math.random().toString(36).slice(2, 10)}`
    return { patient_hash: hash, initials: body.initials || 'PT', created_at: new Date().toISOString() }
  })

  // ── Quick scan ──
  app.post('/api/v1/dicom/studies/:studyId/quick-scan', async () => {
    return { ok: true, status: 'queued' }
  })

  // ── Report PDF ──
  app.post('/api/v1/report/pdf', async (request) => {
    return { hash: `report_${Math.random().toString(36).slice(2, 8)}` }
  })

  // ── File uploads list ──
  app.get('/api/v1/files/uploads', async () => {
    return []
  })
}
