import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard'
import prisma from '../../common/prisma'
import { ResearchService } from './research.service'
import { createStudySchema, enrollPatientSchema } from './research.dto'
import { extractRulesFromProtocol, getPendingRules, confirmRule, rejectRule, getConfirmationStatus } from './protocol-extractor.js'

const service = new ResearchService()

// Transform Prisma camelCase → frontend snake_case
const toStudy = (s: any) => ({
  study_id: s.id, display_name: s.name, short_code: s.shortCode,
  status: 'active', created_at: s.createdAt, updated_at: s.updatedAt,
})
const toRoster = (e: any, p?: any) => ({
  patient_hash: e.patientHash,
  patient_id: e.patientHash,
  name: p?.name || '',
  initials: p?.initials || '',
  age_value: p?.age || undefined,
  sex: p?.sex || '',
  chief_complaint: p?.chiefComplaint || '',
  status: 'active',
  arm: e.arm,
  enrolled_at: e.enrolledAt,
})
const toScreening = (s: any, p?: any) => ({ patient_hash: s.patientHash, patient_id: s.patientHash, name: p?.name || '', initials: p?.initials || '', age_value: p?.age || undefined, sex: p?.sex || '', status: s.verdict, scanned_at: s.scannedAt, criteria_results: [] })
const toObservation = (o: any, p?: any) => ({ observation_id: o.id, patient_hash: o.patientHash, patient_id: o.patientHash, name: p?.name || '', initials: p?.initials || '', age_value: p?.age || undefined, sex: p?.sex || '', category: o.kind, ae_grade: o.grade, is_dlt: o.dlt === 1, confirmed: o.confirmed === 1, created_at: o.createdAt })
const toAssessment = (a: any, p?: any) => ({ visit_id: a.visit, patient_hash: a.patientHash, patient_id: a.patientHash, name: p?.name || '', initials: p?.initials || '', age_value: p?.age || undefined, sex: p?.sex || '', scheduled_at: a.dueAt, status: a.completedAt ? 'completed' : 'pending', completed_at: a.completedAt })

async function getPatientMap(hashes: string[], userId: string): Promise<Map<string, any>> {
  if (hashes.length === 0) return new Map()
  const patients = await (prisma as any).patientRecord.findMany({
    where: { hash: { in: hashes }, userId },
    select: { hash: true, name: true, initials: true, age: true, sex: true, chiefComplaint: true },
  })
  return new Map(patients.map((p: any) => [p.hash, p]))
}

export async function researchRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  app.get('/api/v1/research/studies', async (request) => {
    const studies = await service.listStudies(request.user!.userId)
    return studies.map(toStudy)
  })

  app.post('/api/v1/research/studies', async (request, reply) => {
    const body = createStudySchema.parse(request.body)
    const s = await service.createStudy(request.user!.userId, body.display_name, body.short_code)
    return toStudy(s)
  })

  app.get('/api/v1/research/studies/:studyId', async (request, reply) => {
    const s = await service.getStudy(request.user!.userId, (request.params as any).studyId)
    if (!s) return reply.status(404).send({ error: 'Study not found' })
    return { ...toStudy(s), description: '' }
  })

  app.get('/api/v1/research/studies/:studyId/roster', async (request) => {
    const studyId = (request.params as any).studyId
    const userId = request.user!.userId
    const enrollments = await service.getRoster(studyId)
    const patientMap = await getPatientMap(enrollments.map((e: any) => e.patientHash), userId)
    return enrollments.map((e: any) => toRoster(e, patientMap.get(e.patientHash)))
  })

  app.get('/api/v1/research/studies/:studyId/enrollments', async (request) => {
    const studyId = (request.params as any).studyId
    const userId = request.user!.userId
    const enrollments = await service.getRoster(studyId)
    const patientMap = await getPatientMap(enrollments.map((e: any) => e.patientHash), userId)
    return enrollments.map((e: any) => toRoster(e, patientMap.get(e.patientHash)))
  })

  app.post('/api/v1/research/studies/:studyId/enrollments', async (request) => {
    const body = enrollPatientSchema.parse(request.body)
    const studyId = (request.params as any).studyId
    const e = await service.enroll(studyId, body.patient_hash, body.arm)
    const userId = request.user!.userId
    const patientMap = await getPatientMap([e.patientHash], userId)
    return toRoster(e, patientMap.get(e.patientHash))
  })

  app.delete('/api/v1/research/studies/:studyId/enrollments/:patientHash', async (request) => {
    const { studyId, patientHash } = request.params as any
    return { ok: await service.unenroll(studyId, patientHash) }
  })

  app.get('/api/v1/research/studies/:studyId/eligibility', async (request) => {
    const studyId = (request.params as any).studyId
    const userId = request.user!.userId
    const screenings = await service.getEligibility(studyId)
    const patientMap = await getPatientMap(screenings.map((s: any) => s.patientHash), userId)
    return { screenings: screenings.map((s: any) => toScreening(s, patientMap.get(s.patientHash))) }
  })

  app.post('/api/v1/research/studies/:studyId/eligibility/rescan', async (request) =>
    service.rescanEligibility((request.params as any).studyId))

  app.get('/api/v1/research/studies/:studyId/observations', async (request) => {
    const studyId = (request.params as any).studyId
    const userId = request.user!.userId
    const observations = await service.getObservations(studyId)
    const patientMap = await getPatientMap(observations.map((o: any) => o.patientHash), userId)
    return observations.map((o: any) => toObservation(o, patientMap.get(o.patientHash)))
  })

  app.post('/api/v1/research/studies/:studyId/observations/:obsId/confirm', async (request, reply) => {
    const { studyId, obsId } = request.params as any
    const body = request.body as any
    const userId = request.user!.userId
    const o = await service.confirmObservation(studyId, obsId, {
      confirmed: body.confirmed ?? true,
      grade: body.ae_grade ?? body.grade,
      dlt: body.is_dlt ?? body.dlt,
      note: body.note,
    })
    if (!o) return reply.status(404).send({ error: 'Observation not found' })
    const patientMap = await getPatientMap([o.patientHash], userId)
    return toObservation(o, patientMap.get(o.patientHash))
  })

  app.get('/api/v1/research/studies/:studyId/safety/stop-rule-status', async (request) => {
    const status = await service.getSafetyStatus((request.params as any).studyId)
    return {
      triggered_rules: status.stopRules
        .filter(r => r.triggered)
        .map(r => ({ rule: r.name, description: r.detail || '' })),
    }
  })

  app.get('/api/v1/research/studies/:studyId/assessments', async (request) => {
    const studyId = (request.params as any).studyId
    const userId = request.user!.userId
    const assessments = await service.getAssessments(studyId)
    const patientMap = await getPatientMap(assessments.map((a: any) => a.patientHash), userId)
    return assessments.map((a: any) => toAssessment(a, patientMap.get(a.patientHash)))
  })

  app.post('/api/v1/research/studies/:studyId/assessments/:visitName/complete', async (request) => {
    const { studyId, visitName } = request.params as any
    return { ok: await service.completeAssessment(studyId, visitName) }
  })

  // Step 3 workflow: Import protocol text (from file upload or paste)
  app.post('/api/v1/research/studies/:studyId/import-protocol', async (request, reply) => {
    const { studyId } = request.params as any
    const { text } = request.body as any
    if (!text) return reply.status(400).send({ error: 'text required' })
    // Trigger AI extraction in background
    extractRulesFromProtocol(studyId, text).catch(() => {})
    return service.importProtocol(studyId, text)
  })

  // Extraction: AI extracts rules from protocol
  app.post('/api/v1/research/studies/:studyId/extract-rules', async (request, reply) => {
    const { studyId } = request.params as any
    const { text } = request.body as any
    if (!text) return reply.status(400).send({ error: 'text required' })
    const rules = await extractRulesFromProtocol(studyId, text)
    return { study_id: studyId, rules, status: getConfirmationStatus(studyId) }
  })

  // List pending extracted rules
  app.get('/api/v1/research/studies/:studyId/protocol-rules', async (request) => {
    const { studyId } = request.params as any
    return {
      rules: getPendingRules(studyId),
      status: getConfirmationStatus(studyId),
    }
  })

  // Doctor confirms a rule — also generate assessments for schedule rules
  app.post('/api/v1/research/studies/:studyId/protocol-rules/:ruleId/confirm', async (request, reply) => {
    const { studyId, ruleId } = request.params as any
    const rule = confirmRule(studyId, ruleId)
    if (!rule) return reply.status(404).send({ error: 'Rule not found' })

    // Generate assessment from confirmed schedule rule
    if (rule.category === 'schedule') {
      const match = rule.rule.match(/^(.+?)\s*\((.+?)\):\s*(.+)$/)
      if (match) {
        const study = await service.getStudy(request.user!.userId, studyId)
        const studyStart = study ? new Date(study.createdAt) : new Date()
        const days = parseTimingForRule(match[2], studyStart)
        if (days !== null) {
          const dueDate = new Date(studyStart)
          dueDate.setDate(dueDate.getDate() + days)
          const crypto = await import('crypto')
          await (prisma as any).researchAssessment.create({
            data: {
              id: `asmt_${crypto.randomBytes(8).toString('hex')}`,
              studyId, patientHash: '',
              visit: match[1].trim(), title: match[1].trim(),
              dueAt: dueDate.toISOString(),
            },
          })
        }
      }
    }

    return { rule, status: getConfirmationStatus(studyId) }
  })

  // Doctor rejects a rule
  app.delete('/api/v1/research/studies/:studyId/protocol-rules/:ruleId', async (request, reply) => {
    const { studyId, ruleId } = request.params as any
    const ok = rejectRule(studyId, ruleId)
    return { rejected: ok, study_id: studyId, status: getConfirmationStatus(studyId) }
  })
}

function parseTimingForRule(timing: string, studyStart: Date): number | null {
  const dayMatch = timing.match(/Day\s+(-?\d+)/)
  if (dayMatch) return parseInt(dayMatch[1])
  const weekMatch = timing.match(/every\s+(\d+)\s*week/i)
  if (weekMatch) return parseInt(weekMatch[1]) * 7
  const monthMatch = timing.match(/every\s+(\d+)\s*month/i)
  if (monthMatch) return parseInt(monthMatch[1]) * 30
  const cycleDay = timing.match(/cycle.*?Day\s+(\d+)/i)
  if (cycleDay) return parseInt(cycleDay[1])
  return null
}
