import prisma from '../../common/prisma'
import crypto from 'crypto'

function uid() { return crypto.randomBytes(8).toString('hex') }

export class ResearchService {
  async listStudies(userId: string) {
    return prisma.researchStudy.findMany({ where: { userId }, orderBy: { updatedAt: 'desc' } })
  }

  async createStudy(userId: string, name: string, shortCode: string) {
    const id = `study_${uid()}`
    const now = new Date().toISOString()
    return prisma.researchStudy.create({
      data: { id, userId, name, shortCode, createdAt: now, updatedAt: now },
    })
  }

  async getStudy(userId: string, studyId: string) {
    return prisma.researchStudy.findFirst({ where: { id: studyId, userId } })
  }

  async getRoster(studyId: string) {
    return prisma.researchEnrollment.findMany({ where: { studyId, unenrolledAt: null } })
  }

  async enroll(studyId: string, patientHash: string, arm = 'default') {
    return prisma.researchEnrollment.upsert({
      where: { studyId_patientHash: { studyId, patientHash } },
      update: { arm, unenrolledAt: null, enrolledAt: new Date().toISOString() },
      create: { studyId, patientHash, arm, enrolledAt: new Date().toISOString() },
    })
  }

  async unenroll(studyId: string, patientHash: string) {
    await prisma.researchEnrollment.update({
      where: { studyId_patientHash: { studyId, patientHash } },
      data: { unenrolledAt: new Date().toISOString() },
    })
    return true
  }

  async getEligibility(studyId: string) {
    return prisma.researchScreening.findMany({ where: { studyId } })
  }

  async rescanEligibility(studyId: string) {
    const roster = await prisma.researchEnrollment.findMany({ where: { studyId, unenrolledAt: null } })
    const now = new Date().toISOString()
    for (const e of roster) {
      await prisma.researchScreening.create({
        data: { id: uid(), studyId, patientHash: e.patientHash, verdict: 'pending', scannedAt: now },
      })
    }
    return { scanned: roster.length }
  }

  async getObservations(studyId: string) {
    return prisma.researchObservation.findMany({ where: { studyId } })
  }

  async confirmObservation(studyId: string, obsId: string, data: { confirmed: boolean; grade?: number; dlt?: boolean; note?: string }) {
    const existing = await prisma.researchObservation.findFirst({ where: { id: obsId, studyId } })
    if (!existing) return null
    return prisma.researchObservation.update({
      where: { id: obsId },
      data: {
        grade: data.grade ?? existing.grade,
        dlt: data.dlt !== undefined ? (data.dlt ? 1 : 0) : existing.dlt,
        confirmed: data.confirmed ? 1 : 0,
        note: data.note ?? existing.note,
      },
    })
  }

  async getSafetyStatus(studyId: string) {
    const obs = await prisma.researchObservation.findMany({ where: { studyId } })
    const dltCount = obs.filter(o => o.dlt === 1 && o.confirmed === 1).length
    const roster = await prisma.researchEnrollment.findMany({ where: { studyId, unenrolledAt: null } })
    return {
      stopRules: [
        { name: 'DLT rate > 33%', triggered: roster.length > 0 && dltCount / roster.length > 0.33, detail: `${dltCount}/${roster.length} DLTs` },
        { name: 'Any Grade 5 AE', triggered: obs.some(o => o.grade === 5), detail: obs.find(o => o.grade === 5)?.kind },
        { name: '≥3 Grade 4 AEs', triggered: obs.filter(o => o.grade === 4).length >= 3, detail: `${obs.filter(o => o.grade === 4).length} Grade 4 AEs` },
      ],
    }
  }

  async getAssessments(studyId: string) {
    return prisma.researchAssessment.findMany({ where: { studyId } })
  }

  async completeAssessment(studyId: string, visitName: string) {
    const a = await prisma.researchAssessment.findFirst({ where: { studyId, visit: visitName, completedAt: null } })
    if (!a) return false
    await prisma.researchAssessment.update({ where: { id: a.id }, data: { completedAt: new Date().toISOString() } })
    return true
  }

  async importProtocol(studyId: string, text: string) {
    await (prisma as any).researchStudy.update({ where: { id: studyId }, data: { protocol: text } })
    return { imported: true, study_id: studyId, content_length: text.length }
  }
}
