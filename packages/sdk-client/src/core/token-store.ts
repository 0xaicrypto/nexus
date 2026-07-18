export interface TokenStore {
  get(): string | null
  set(token: string): void
  clear(): void
}

export const localStorageStore: TokenStore = {
  get: () => { try { return localStorage.getItem('heurion-token') } catch { return null } },
  set: (t) => { try { localStorage.setItem('heurion-token', t) } catch {} },
  clear: () => { try { localStorage.removeItem('heurion-token') } catch {} },
}

let _memToken: string | null = null
export const memoryStore: TokenStore = {
  get: () => _memToken,
  set: (t) => { _memToken = t },
  clear: () => { _memToken = null },
}
