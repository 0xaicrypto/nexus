import { describe, test, expect } from 'vitest'
import { getApp, authHeader } from './setup.js'
import fs from 'fs'

describe('Chat & Sessions', () => {

  test('list sessions (may be empty)', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/sessions',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    expect(JSON.parse(res.payload).sessions).toBeDefined()
  })

  test('create session', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/sessions',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'Test Session' },
    })
    expect(res.statusCode).toBe(200)
    const body = JSON.parse(res.payload)
    expect(body.id).toBeDefined()
    expect(body.message_count).toBe(0)
  })

  test('agent state', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/agent/state',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    const body = JSON.parse(res.payload)
    expect(body.user_id).toBeDefined()
    expect(body.server_time).toBeDefined()
  })

  test('agent timeline', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/agent/timeline?limit=5',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    expect(JSON.parse(res.payload).items).toBeDefined()
  })

  test('agent messages for session', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/agent/messages?session_id=test_123&limit=10',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    const body = JSON.parse(res.payload)
    expect(body.messages).toBeDefined()
    expect(body.total).toBeDefined()
  })
})

describe('Skills', () => {

  test('list installed skills (may be empty)', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/skills',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    expect(JSON.parse(res.payload).skills).toBeDefined()
  })

  test('search skills with pagination', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/skills/search?source=all&page=1&page_size=5',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    const body = JSON.parse(res.payload)
    expect(body.results).toBeDefined()
    expect(body.total).toBeGreaterThan(0)
    expect(body.page).toBe(1)
    expect(body.total_pages).toBeGreaterThan(0)
    expect(body.results.length).toBeLessThanOrEqual(5)
  })

  test('search by source', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/skills/search?source=anthropic',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    const results = JSON.parse(res.payload).results
    expect(results.every((r: any) => r.source === 'anthropic')).toBe(true)
  })

  test('install and uninstall skill', async () => {
    const app = await getApp()
    // Install
    const install = await app.inject({
      method: 'POST', url: '/api/v1/skills/install',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { identifier: 'official/clinical-summary' },
    })
    expect(install.statusCode).toBe(200)

    // Verify installed
    const list = await app.inject({
      method: 'GET', url: '/api/v1/skills',
      headers: await authHeader(),
    })
    const installed = JSON.parse(list.payload).skills
    expect(installed.find((s: any) => s.name === 'Clinical Summary')).toBeTruthy()

    // Uninstall
    const uninstall = await app.inject({
      method: 'DELETE', url: '/api/v1/skills/Clinical%20Summary',
      headers: await authHeader(),
    })
    expect(uninstall.statusCode).toBe(200)
  })
})

describe('Settings & Admin', () => {

  test('get llm status', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/settings/llm',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    const body = JSON.parse(res.payload)
    expect(body.provider).toBeDefined()
  })

  test('list admin users (requires admin)', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/admin/users',
      headers: await authHeader(),
    })
    // May be 200 (if first user is admin) or 403 (if not)
    expect([200, 403]).toContain(res.statusCode)
  })
})

describe('Memory', () => {

  test('export memory', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/memory/export',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    const body = JSON.parse(res.payload)
    expect(body.facts).toBeDefined()
    expect(body.episodes).toBeDefined()
    expect(body.event_log_count).toBeDefined()
  })

  test('import memory', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/memory/import',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { facts: [], episodes: [] },
    })
    expect(res.statusCode).toBe(200)
    const body = JSON.parse(res.payload)
    expect(body.imported).toBe(0)
  })
})
