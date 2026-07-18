import { HttpTransport } from '../core/http-client.js'
import type { AdminUser } from '../types.js'

export class AdminModule {
  constructor(private http: HttpTransport) {}

  listUsers() { return this.http.get<{ users: AdminUser[] }>('/api/v1/admin/users') }
  disableUser(userId: string) { return this.http.post(`/api/v1/admin/users/${userId}/disable`) }
  enableUser(userId: string) { return this.http.post(`/api/v1/admin/users/${userId}/enable`) }
  resetPassword(userId: string, newPassword: string) {
    return this.http.post(`/api/v1/admin/users/${userId}/reset-password`, { new_password: newPassword })
  }
}
