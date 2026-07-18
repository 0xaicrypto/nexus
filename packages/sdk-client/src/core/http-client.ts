export class HeurionError extends Error {
  constructor(public status: number, public body: string, public path: string) {
    super(`${path} → ${status}: ${body}`)
  }
}

export class HttpTransport {
  private tokenStore: TokenStore
  private _baseUrl: string

  constructor(baseUrl: string, tokenStore: TokenStore) {
    this._baseUrl = baseUrl.replace(/\/$/, '')
    this.tokenStore = tokenStore
  }

  get baseUrl() { return this._baseUrl }
  getToken() { return this.tokenStore.get() }

  async get<T>(path: string, params?: Record<string, string>): Promise<T> {
    const qs = params ? '?' + new URLSearchParams(params).toString() : ''
    return this.request<T>('GET', path + qs)
  }

  async post<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>('POST', path, body)
  }

  async put<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>('PUT', path, body)
  }

  async patch<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>('PATCH', path, body)
  }

  async del<T>(path: string): Promise<T> {
    return this.request<T>('DELETE', path)
  }

  async stream(path: string, body?: unknown): Promise<Response> {
    const token = this.tokenStore.get()
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (token) headers['Authorization'] = `Bearer ${token}`
    const res = await fetch(this._baseUrl + path, {
      method: 'POST', headers,
      body: body ? JSON.stringify(body) : undefined,
    })
    if (!res.ok) throw new HeurionError(res.status, await res.text().catch(() => ''), path)
    return res
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const token = this.tokenStore.get()
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (token) headers['Authorization'] = `Bearer ${token}`

    const res = await fetch(this._baseUrl + path, {
      method, headers,
      body: body ? JSON.stringify(body) : undefined,
    })
    if (!res.ok) throw new HeurionError(res.status, await res.text().catch(() => ''), path)
    return res.json()
  }
}

import { TokenStore } from './token-store.js'
