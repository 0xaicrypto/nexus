import { describe, test, expect } from 'vitest'
import { getApp, authHeader } from './setup.js'

describe('Documents', () => {
  test('create document with title', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/docs',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'Clinical Note' },
    })
    expect(res.statusCode).toBe(200)
    const body = JSON.parse(res.payload)
    expect(body.id).toBeTruthy()
    expect(body.title).toBe('Clinical Note')
    expect(body.body).toBe('')
    expect(body.created_at).toBeTruthy()
  })

  test('create document without title defaults to Untitled', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/docs',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: {},
    })
    expect(res.statusCode).toBe(200)
    expect(JSON.parse(res.payload).title).toBeTruthy()
  })

  test('list documents', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/docs',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    const body = JSON.parse(res.payload)
    expect(body.docs).toBeDefined()
    expect(Array.isArray(body.docs)).toBe(true)
  })

  test('update document body and verify', async () => {
    const app = await getApp()
    // Create
    const create = await app.inject({
      method: 'POST', url: '/api/v1/docs',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'Editable' },
    })
    const docId = JSON.parse(create.payload).id

    // Update
    const update = await app.inject({
      method: 'PUT', url: `/api/v1/docs/${docId}`,
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'Updated', body: '# Findings\nNo abnormalities detected.' },
    })
    expect(update.statusCode).toBe(200)
    const body = JSON.parse(update.payload)
    expect(body.title).toBe('Updated')
    expect(body.body).toContain('Findings')

    // Verify GET returns updated content
    const get = await app.inject({
      method: 'GET', url: `/api/v1/docs/${docId}`,
      headers: await authHeader(),
    })
    expect(JSON.parse(get.payload).body).toContain('Findings')
  })

  test('phi scan detects SSN and names', async () => {
    const app = await getApp()
    const create = await app.inject({
      method: 'POST', url: '/api/v1/docs',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'PHI Test' },
    })
    const docId = JSON.parse(create.payload).id

    // Add content with PHI
    await app.inject({
      method: 'PUT', url: `/api/v1/docs/${docId}`,
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { body: 'Patient John Smith. MRN: 123-45-6789' },
    })

    const res = await app.inject({
      method: 'POST', url: `/api/v1/docs/${docId}/phi-scan`,
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    const findings = JSON.parse(res.payload).findings
    expect(findings.some((f: any) => f.kind === 'SSN')).toBe(true)
    expect(findings.some((f: any) => f.kind === 'Name')).toBe(true)
    expect(findings.every((f: any) => typeof f.suggestion === 'string' && f.suggestion.length > 0)).toBe(true)
  })

  test('save creates a snapshot when body changes', async () => {
    const app = await getApp()
    const create = await app.inject({
      method: 'POST', url: '/api/v1/docs',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'Snapshot Test' },
    })
    const docId = JSON.parse(create.payload).id

    // First update
    await app.inject({
      method: 'PUT', url: `/api/v1/docs/${docId}`,
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { body: 'First draft content.' },
    })

    const res = await app.inject({
      method: 'GET', url: `/api/v1/docs/${docId}/snapshots`,
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    const snapshots = JSON.parse(res.payload).snapshots
    expect(snapshots.length).toBeGreaterThanOrEqual(1)
    expect(snapshots[0].body).toBe('')
  })

  test('non-existent document returns 404', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/docs/nonexistent_doc',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(404)
  })

  test('doc chat endpoint responds with SSE', async () => {
    const app = await getApp()
    const create = await app.inject({
      method: 'POST', url: '/api/v1/docs',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'Chat Test', body: 'Test content' },
    })
    const docId = JSON.parse(create.payload).id

    const res = await app.inject({
      method: 'POST', url: `/api/v1/docs/${docId}/chat`,
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: JSON.stringify({ message: 'Summarize this doc' }),
    })
    expect(res.statusCode).toBe(200)
    // SSE response should start with data: prefix
    expect(res.payload.startsWith('data: ')).toBe(true)
  })

  test('doc polish endpoint responds with SSE', async () => {
    const app = await getApp()
    const create = await app.inject({
      method: 'POST', url: '/api/v1/docs',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'Polish Test', body: 'Need polish' },
    })
    const docId = JSON.parse(create.payload).id

    const res = await app.inject({
      method: 'POST', url: `/api/v1/docs/${docId}/polish`,
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: JSON.stringify({ selection: 'Need polish' }),
    })
    expect(res.statusCode).toBe(200)
    expect(res.payload.startsWith('data: ')).toBe(true)
  })

  test('export docx returns binary docx', async () => {
    const app = await getApp()
    const create = await app.inject({
      method: 'POST', url: '/api/v1/docs',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'Export Test', body: 'Export me' },
    })
    const docId = JSON.parse(create.payload).id

    const res = await app.inject({
      method: 'POST', url: `/api/v1/docs/${docId}/export`,
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    expect(res.headers['content-type']).toContain('application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    expect(Buffer.from(res.payload).length).toBeGreaterThan(0)
  })

  test('add and list references', async () => {
    const app = await getApp()
    const create = await app.inject({
      method: 'POST', url: '/api/v1/docs',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'Ref Test' },
    })
    const docId = JSON.parse(create.payload).id

    const add = await app.inject({
      method: 'POST', url: `/api/v1/docs/${docId}/references`,
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: JSON.stringify({ kind: 'guideline', content: 'NCCN', label: 'NSCLC' }),
    })
    expect(add.statusCode).toBe(200)
    const refId = JSON.parse(add.payload).reference_id
    expect(refId).toBeTruthy()

    const list = await app.inject({
      method: 'GET', url: `/api/v1/docs/${docId}/references`,
      headers: await authHeader(),
    })
    expect(list.statusCode).toBe(200)
    const refs = JSON.parse(list.payload).references
    expect(refs.some((r: any) => r.reference_id === refId && r.content === 'NCCN')).toBe(true)
  })

  test('delete document removes it', async () => {
    const app = await getApp()
    const create = await app.inject({
      method: 'POST', url: '/api/v1/docs',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'To Delete' },
    })
    const docId = JSON.parse(create.payload).id

    const del = await app.inject({
      method: 'DELETE', url: `/api/v1/docs/${docId}`,
      headers: await authHeader(),
    })
    expect(del.statusCode).toBe(200)

    const get = await app.inject({
      method: 'GET', url: `/api/v1/docs/${docId}`,
      headers: await authHeader(),
    })
    expect(get.statusCode).toBe(404)
  })
})
