import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard.js'

/**
 * Stub endpoints that proxy was forwarding to Python.
 * All return empty/mock data so the TS backend is fully self-contained.
 */
export async function stubRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  // ── Archive patient ──
  app.post('/api/v1/dicom/patients/:hash/archive', async (request) => {
    return { archived: true, patient_hash: (request.params as any).hash }
  })

  // ── Send DICOM slice to agent ──
  app.post('/api/v1/dicom/send-to-agent', async () => {
    return { ok: true }
  })

  // ── Report download ──
  app.get('/api/v1/report/pdf/:hash', async (request, reply) => {
    reply.header('Content-Type', 'application/pdf')
    return Buffer.from('%PDF-stub', 'utf-8')
  })

  // ── Memory medications ──
  app.get('/api/v1/memory/patient/:patientHash/medications', async () => {
    return { medications: [] }
  })

  // ── Workflows ──
  app.get('/api/v1/workflows', async () => {
    return { workflows: [] }
  })
  app.get('/api/v1/workflows/packs', async () => {
    return { packs: [] }
  })
  app.post('/api/v1/workflows/packs/:id/install', async (request) => {
    return { installed: true, pack_id: (request.params as any).id }
  })
  app.get('/api/v1/workflows/runs', async () => {
    return { runs: [] }
  })

  // ── Schedule ──
  app.get('/api/v1/schedule/list', async (request) => {
    return { tasks: [] }
  })
  app.delete('/api/v1/schedule/:id', async (request) => {
    return { task_id: (request.params as any).id }
  })

  // ── Export ──
  app.post('/api/v1/export/bundle', async (request, reply) => {
    reply.header('Content-Type', 'application/json')
    return { bundle: {}, exported_at: new Date().toISOString() }
  })

  // ── Sandbox ──
  app.post('/api/v1/sandbox/execute', async (request) => {
    return { output: '[sandbox stub]', exit_code: 0 }
  })

  // ── Chat files ──
  app.get('/api/v1/chat/files', async () => {
    return { files: [] }
  })

  // ── Feedback ──
  app.post('/feedback', async () => {
    return { ok: true }
  })
}
