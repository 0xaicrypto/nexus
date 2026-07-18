import { HttpTransport } from '../core/http-client.js'
import type { Study, RosterEntry, Screening, Observation, SafetyStatus, Assessment } from '../types.js'

export class ResearchModule {
  constructor(private http: HttpTransport) {}

  listStudies() { return this.http.get<Study[]>('/api/v1/research/studies') }
  createStudy(name: string, shortCode: string) {
    return this.http.post<Study>('/api/v1/research/studies', { name, shortCode })
  }
  getStudy(id: string) { return this.http.get<Study>(`/api/v1/research/studies/${id}`) }

  getRoster(studyId: string) { return this.http.get<RosterEntry[]>(`/api/v1/research/studies/${studyId}/roster`) }
  enroll(studyId: string, patientHash: string, arm?: string) {
    return this.http.post(`/api/v1/research/studies/${studyId}/enrollments`, { patient_hash: patientHash, arm })
  }
  unenroll(studyId: string, patientHash: string) {
    return this.http.del(`/api/v1/research/studies/${studyId}/enrollments/${patientHash}`)
  }

  getEligibility(studyId: string) { return this.http.get<Screening[]>(`/api/v1/research/studies/${studyId}/eligibility`) }
  scanEligibility(studyId: string) {
    return this.http.post<{ scanned: number }>(`/api/v1/research/studies/${studyId}/eligibility/rescan`)
  }

  getObservations(studyId: string) { return this.http.get<Observation[]>(`/api/v1/research/studies/${studyId}/observations`) }
  confirmObservation(studyId: string, obsId: string, data: Record<string, unknown>) {
    return this.http.post(`/api/v1/research/studies/${studyId}/observations/${obsId}/confirm`, data)
  }
  getSafety(studyId: string) {
    return this.http.get<SafetyStatus>(`/api/v1/research/studies/${studyId}/safety/stop-rule-status`)
  }

  getAssessments(studyId: string) { return this.http.get<Assessment[]>(`/api/v1/research/studies/${studyId}/assessments`) }
  completeAssessment(studyId: string, visitId: string) {
    return this.http.post(`/api/v1/research/studies/${studyId}/assessments/${visitId}/complete`)
  }
}
