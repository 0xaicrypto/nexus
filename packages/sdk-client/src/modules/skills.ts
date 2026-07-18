import { HttpTransport } from '../core/http-client.js'
import type { InstalledSkill, SearchResult } from '../types.js'

export class SkillsModule {
  constructor(private http: HttpTransport) {}

  list() { return this.http.get<{ skills: InstalledSkill[] }>('/api/v1/skills') }
  search(query: string, source = 'all') {
    return this.http.get<{ results: SearchResult[] }>('/api/v1/skills/search', { query, source })
  }
  install(identifier: string) { return this.http.post<{ name: string }>('/api/v1/skills/install', { identifier }) }
  toggle(name: string, enabled: boolean) {
    return this.http.post(`/api/v1/skills/${name}/toggle`, { enabled })
  }
  uninstall(name: string) { return this.http.del(`/api/v1/skills/${name}`) }
}
