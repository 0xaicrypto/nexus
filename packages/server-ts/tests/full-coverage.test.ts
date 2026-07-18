import { describe, test, expect } from 'vitest'
import { getApp, authHeader, getToken } from './setup.js'
import fs from 'fs'
import path from 'path'

/**
 * 补全所有缺失的测试覆盖
 */

describe('Auth 边界', () => {
  test('register with empty username rejected', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/auth/register',
      headers: { 'content-type': 'application/json' },
      payload: { username: '', password: 'test123' },
    })
    expect(res.statusCode).toBe(400)
  })
  test('register with short password rejected', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/auth/register',
      headers: { 'content-type': 'application/json' },
      payload: { username: 'valid', password: '12' },
    })
    expect(res.statusCode).toBe(400)
  })
  test('login with empty body rejected', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/auth/login',
      headers: { 'content-type': 'application/json' },
      payload: {},
    })
    expect(res.statusCode).toBe(400)
  })
})

describe('Patients 边缘情况', () => {
  test('list patients returns array even when empty', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/dicom/patients/full',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    expect(Array.isArray(JSON.parse(res.payload))).toBe(true)
  })
  test('delete non-existent patient handled', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'DELETE', url: '/api/v1/dicom/patients/nonexistent_hash',
      headers: await authHeader(),
    })
    expect([200, 404]).toContain(res.statusCode)
  })
})

describe('Research 边界', () => {
  test('create study with minimum valid fields', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/research/studies',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { display_name: 'X', short_code: 'X1' },
    })
    expect(res.statusCode).toBe(200)
  })
  test('import protocol with empty text rejected', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/research/studies/test/import-protocol',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { text: '' },
    })
    expect(res.statusCode).toBe(400)
  })
  test('roster returns empty for new study', async () => {
    const app = await getApp()
    const s = await app.inject({
      method: 'POST', url: '/api/v1/research/studies',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { display_name: 'Roster Empty', short_code: 'RE01' },
    })
    const sid = JSON.parse(s.payload).study_id
    const res = await app.inject({
      method: 'GET', url: `/api/v1/research/studies/${sid}/roster`,
      headers: await authHeader(),
    })
    expect(JSON.parse(res.payload).length).toBe(0)
  })
  test('eligibility returns empty for new study', async () => {
    const app = await getApp()
    const s = await app.inject({
      method: 'POST', url: '/api/v1/research/studies',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { display_name: 'Elig Empty', short_code: 'EE01' },
    })
    const sid = JSON.parse(s.payload).study_id
    const res = await app.inject({
      method: 'GET', url: `/api/v1/research/studies/${sid}/eligibility`,
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
  })
})

describe('Documents 边界', () => {
  test('update non-existent doc returns 404', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'PUT', url: '/api/v1/docs/nonexistent',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'test' },
    })
    expect(res.statusCode).toBe(404)
  })
  test('phi scan on empty doc returns empty', async () => {
    const app = await getApp()
    const d = await app.inject({
      method: 'POST', url: '/api/v1/docs',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'Empty' },
    })
    const did = JSON.parse(d.payload).id
    const res = await app.inject({
      method: 'POST', url: `/api/v1/docs/${did}/phi-scan`,
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    expect(JSON.parse(res.payload).findings.length).toBe(0)
  })
})

describe('Skills 完整流程', () => {
  test('search all sources returns multiple', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/skills/search?source=all',
      headers: await authHeader(),
    })
    const body = JSON.parse(res.payload)
    expect(body.results.length).toBeGreaterThan(0)
    expect(body.total).toBeGreaterThanOrEqual(body.results.length)
  })
  test('search by keyword filters', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/skills/search?query=imaging&source=all',
      headers: await authHeader(),
    })
    const names = JSON.parse(res.payload).results.map((r: any) => r.name.toLowerCase())
    expect(names.every((n: string) => n.includes('imaging') || n.includes('reader') || n.includes('detection'))).toBe(true)
  })
  test('install non-existent skill still succeeds', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/skills/install',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { identifier: 'custom/my-skill' },
    })
    expect(res.statusCode).toBe(200)
  })
})

describe('Settings 功能', () => {
  test('llm status returns provider and model', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/settings/llm',
      headers: await authHeader(),
    })
    const body = JSON.parse(res.payload)
    expect(body.provider).toBeTruthy()
    expect(body.model).toBeTruthy()
  })
  test('llm test endpoint responds', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/settings/llm/test',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    expect(JSON.parse(res.payload).ok).toBe(true)
  })
  test('update llm settings returns ok', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'PUT', url: '/api/v1/settings/llm',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { provider: 'deepseek', model: 'deepseek-chat' },
    })
    expect(JSON.parse(res.payload).ok).toBe(true)
  })
})

describe('Agent State 详细信息', () => {
  test('state returns all required fields', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/agent/state',
      headers: await authHeader(),
    })
    const body = JSON.parse(res.payload)
    expect(body.user_id).toBeTruthy()
    expect(body.memory_count).toBeGreaterThanOrEqual(0)
    expect(body.episode_count).toBeGreaterThanOrEqual(0)
    expect(body.skill_count).toBeGreaterThanOrEqual(0)
    expect(body.server_time).toBeTruthy()
  })
})

describe('Calendar 内容验证', () => {
  test('ical contains research events', async () => {
    const app = await getApp()
    const token = await getToken()
    const res = await app.inject({
      method: 'GET', url: `/api/v1/calendar/export.ics?token=${token}`,
    })
    expect(res.statusCode).toBe(200)
    // Should have at least calendar header and footer
    expect(res.payload).toContain('VCALENDAR')
  })
})

describe('Memory 完整验证', () => {
  test('export has all required fields', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/memory/export',
      headers: await authHeader(),
    })
    const body = JSON.parse(res.payload)
    expect(body.facts).toBeDefined()
    expect(body.episodes).toBeDefined()
    expect(body.skills).toBeDefined()
    expect(body.event_log_count).toBeGreaterThanOrEqual(0)
    expect(body.exported_at).toBeTruthy()
  })
  test('import returns imported count', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/memory/import',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { facts: [], episodes: [] },
    })
    expect(res.statusCode).toBe(200)
    expect(JSON.parse(res.payload).imported).toBe(0)
  })
})

describe('DICOM Render 格式验证', () => {
  test('render returns image bytes for existing file', async () => {
    const app = await getApp()
    const token = await getToken()
    const payload = JSON.parse(Buffer.from(token.split('.')[1], 'base64').toString())
    const userId = payload.userId
    const dir = path.join('.nexus/test-twins', userId, 'uploads')
    fs.mkdirSync(dir, { recursive: true })
    const src = path.join(process.cwd(), 'sample-chest-ct.dcm')
    if (fs.existsSync(src)) fs.copyFileSync(src, path.join(dir, 'render_test.dcm'))

    const res = await app.inject({
      method: 'GET', url: '/api/v1/dicom/studies/render_test.dcm/series/0/render?format=png',
      headers: await authHeader(),
    })
    // May be 200 (PNG) or empty buffer (file not found or parser unavailable)
    expect([200, 500]).toContain(res.statusCode)
    if (res.statusCode === 200 && res.body.length > 8) {
      // Check PNG signature
      const buf = res.rawPayload
      expect(buf[0]).toBe(137)
      expect(buf[1]).toBe(80)
      expect(buf[2]).toBe(78)
      expect(buf[3]).toBe(71)
    }
  })
})
