import { HttpTransport } from '../core/http-client.js'
import type { Patient, PatientDetail } from '../types.js'

export class PatientsModule {
  constructor(private http: HttpTransport) {}

  list() { return this.http.get<Patient[]>('/api/v1/dicom/patients/full') }
  get(hash: string) { return this.http.get<PatientDetail>(`/api/v1/dicom/patients/${hash}/detail`) }
  delete(hash: string) { return this.http.del(`/api/v1/dicom/patients/${hash}`) }
  register(data: Record<string, unknown>) {
    return this.http.post<Patient>('/api/v1/dicom/patients/register-manual', data)
  }
  getStudies(hash: string) {
    return this.http.get<{ studies: unknown[] }>(`/api/v1/dicom/patients/${hash}/studies`)
  }
}
