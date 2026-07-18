import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard'
import { ResearchService } from './research.service'
import { createStudySchema, enrollPatientSchema } from './research.dto'

const service = new ResearchService()

export async function researchRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  // ── Studies ──
  app.get('/api/v1/research/studies', async (request) =>
    service.listStudies(request.user!.userId))

  app.post('/api/v1/research/studies', async (request, reply) => {
    const body = createStudySchema.parse(request.body)
    return service.createStudy(request.user!.userId, body.name, body.shortCode)
  })

  app.get('/api/v1/research/studies/:studyId', async (request, reply) => {
    const study = await service.getStudy(request.user!.userId, (request.params as any).studyId)
    if (!study) return reply.status(404).send({ error: 'Study not found' })
    return study
  })

  // ── Roster ──
  app.get('/api/v1/research/studies/:studyId/roster', async (request) =>
    service.getRoster((request.params as any).studyId))

  app.post('/api/v1/research/studies/:studyId/enrollments', async (request, reply) => {
    const body = enrollPatientSchema.parse(request.body)
    return service.enroll((request.params as any).studyId, body.patientHash, body.arm)
  })

  app.delete('/api/v1/research/studies/:studyId/enrollments/:patientHash', async (request) => {
    const { studyId, patientHash } = request.params as any
    return { ok: await service.unenroll(studyId, patientHash) }
  })

  // ── Eligibility ──
  app.get('/api/v1/research/studies/:studyId/eligibility', async (request) =>
    service.getEligibility((request.params as any).studyId))

  app.post('/api/v1/research/studies/:studyId/eligibility/rescan', async (request) =>
    service.rescanEligibility((request.params as any).studyId))

  // ── Safety Observations ──
  app.get('/api/v1/research/studies/:studyId/observations', async (request) =>
    service.getObservations((request.params as any).studyId))

  app.post('/api/v1/research/studies/:studyId/observations/:obsId/confirm', async (request, reply) => {
    const { studyId, obsId } = request.params as any
    const obs = await service.confirmObservation(studyId, obsId, request.body as any)
    if (!obs) return reply.status(404).send({ error: 'Observation not found' })
    return obs
  })

  app.get('/api/v1/research/studies/:studyId/safety/stop-rule-status', async (request) =>
    service.getSafetyStatus((request.params as any).studyId))

  // ── Assessments ──
  app.get('/api/v1/research/studies/:studyId/assessments', async (request) =>
    service.getAssessments((request.params as any).studyId))

  app.post('/api/v1/research/studies/:studyId/assessments/:visitName/complete', async (request) => {
    const { studyId, visitName } = request.params as any
    return { ok: await service.completeAssessment(studyId, visitName) }
  })
}
