import { HttpTransport } from '../core/http-client.js'
import type { FileItem } from '../types.js'

export class FilesModule {
  constructor(private http: HttpTransport) {}

  async upload(file: File | Blob, patientHash?: string): Promise<FileItem> {
    const form = new FormData()
    form.append('file', file)
    if (patientHash) form.append('patient_hash', patientHash)

    const token = this.http.getToken()
    const res = await fetch(`${this.http.baseUrl}/api/v1/files/upload`, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    })
    if (!res.ok) throw new Error(`Upload failed: ${res.status}`)
    return res.json()
  }

  list(patientHash?: string) {
    const params = patientHash ? { patient_hash: patientHash } : undefined
    return this.http.get<FileItem[]>('/api/v1/files', params as any)
  }

  delete(fileId: string | number) { return this.http.del(`/api/v1/files/${fileId}`) }
}
