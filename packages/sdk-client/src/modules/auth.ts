import { HttpTransport } from '../core/http-client.js'
import { TokenStore } from '../core/token-store.js'
import type { AuthSession, UserProfile } from '../types.js'

export class AuthModule {
  constructor(private http: HttpTransport, private store: TokenStore) {}

  async register(input: { username: string; password: string; displayName?: string }): Promise<AuthSession> {
    const body: Record<string, string> = { username: input.username, password: input.password }
    if (input.displayName) body.display_name = input.displayName
    const s = await this.http.post<AuthSession>('/api/v1/auth/register', body)
    this.store.set(s.jwt_token)
    return s
  }

  async login(username: string, password: string): Promise<AuthSession> {
    const s = await this.http.post<AuthSession>('/api/v1/auth/login', { username, password })
    this.store.set(s.jwt_token)
    return s
  }

  logout() { this.store.clear() }

  getProfile() { return this.http.get<UserProfile>('/api/v1/user/profile') }
  updateProfile(data: Record<string, string>) { return this.http.patch<UserProfile>('/api/v1/user/profile', data) }
}
