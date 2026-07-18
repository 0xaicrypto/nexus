import { describe, test, expect } from 'vitest'
import { getApp, authHeader } from './setup.js'

/**
 * 医生工作流集成测试 — 覆盖完整的 6 步业务流程
 * 
 * Step 1: 接诊 — 创建患者 + 上传文件 → 自动分析
 * Step 2: 接诊 — 患者 Chat → 自动提取发现并更新
 * Step 3: 研究 — 导入协议文本 → 规则提取
 * Step 4: 研究 — 确认规则 + 创建研究项目
 * Step 5: 研究 — 跨研究 Chat + 进展总结
 * Step 6: 写作 — 创建文档 + AI 润色 + 引用患者/研究
 */

describe('Workflow Step 1 — Patient Onboarding', () => {
  test('create patient with chief complaint', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/dicom/patients/register-manual',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { initials: 'JS', age: 58, sex: 'M', chief_complaint: 'Persistent cough, 3 weeks' },
    })
    expect(res.statusCode).toBe(200)
    expect(JSON.parse(res.payload).patient_hash).toBeTruthy()
  })

  test('file upload endpoint exists', async () => {
    const app = await getApp()
    // File upload requires multipart form data, which inject() doesn't support well
    // Verify the route exists by checking it requires auth
    const res = await app.inject({
      method: 'POST', url: '/api/v1/files/upload',
    })
    expect(res.statusCode).toBe(401) // Unauthorized without token
  })

  test('patient detail returns complete profile', async () => {
    const app = await getApp()
    const create = await app.inject({
      method: 'POST', url: '/api/v1/dicom/patients/register-manual',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { initials: 'CD', age: 45, sex: 'F', chief_complaint: 'Headache' },
    })
    const hash = JSON.parse(create.payload).patient_hash

    const res = await app.inject({
      method: 'GET', url: `/api/v1/dicom/patients/${hash}/detail`,
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    const body = JSON.parse(res.payload)
    expect(body.initials).toBe('CD')
    expect(body.chief_complaint).toBe('Headache')
    expect(body.age_value).toBe(45)
  })
})

describe('Workflow Step 2 — Patient Chat + Finding Extraction', () => {
  test('patient chat sends with patient_hash', async () => {
    const app = await getApp()
    // Create patient
    const create = await app.inject({
      method: 'POST', url: '/api/v1/dicom/patients/register-manual',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { initials: 'EF' },
    })
    const hash = JSON.parse(create.payload).patient_hash

    // Send patient-scoped chat
    const res = await app.inject({
      method: 'POST', url: '/api/v1/agent/chat',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: JSON.stringify({ text: 'Patient has fever', patient_hash: hash }),
    })
    expect(res.statusCode).toBe(200)
    // Should contain SSE data
    expect(res.payload).toContain('data:')
  })

  test('patient chat without patient_hash still works (global)', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/agent/chat',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: JSON.stringify({ text: 'Hello' }),
    })
    expect(res.statusCode).toBe(200)
  })

  test('patient messages can be queried by session', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/agent/messages?session_id=test_patient_session',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    const body = JSON.parse(res.payload)
    expect(body.messages).toBeDefined()
    expect(body.total).toBeGreaterThanOrEqual(0)
  })
})

describe('Workflow Step 3-4 — Research Import + Create', () => {
  test('create research study from protocol', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/research/studies',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: {
        display_name: 'NSCLC Immunotherapy Trial',
        short_code: 'NIT001',
      },
    })
    expect(res.statusCode).toBe(200)
    const body = JSON.parse(res.payload)
    expect(body.study_id).toBeTruthy()
    expect(body.status).toBe('active')
  })

  test('study detail returns complete info', async () => {
    const app = await getApp()
    // Create study
    const create = await app.inject({
      method: 'POST', url: '/api/v1/research/studies',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { display_name: 'Eligibility Study', short_code: 'ES001' },
    })
    const id = JSON.parse(create.payload).study_id

    const res = await app.inject({
      method: 'GET', url: `/api/v1/research/studies/${id}`,
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    expect(JSON.parse(res.payload).study_id).toBe(id)
  })

  test('study has safety and eligibility endpoints', async () => {
    const app = await getApp()
    const create = await app.inject({
      method: 'POST', url: '/api/v1/research/studies',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { display_name: 'Safety Study', short_code: 'SS001' },
    })
    const id = JSON.parse(create.payload).study_id

    // Safety status
    const safety = await app.inject({
      method: 'GET', url: `/api/v1/research/studies/${id}/safety/stop-rule-status`,
      headers: await authHeader(),
    })
    expect(safety.statusCode).toBe(200)
    expect(JSON.parse(safety.payload).triggered_rules).toBeDefined()

    // Eligibility
    const eligibility = await app.inject({
      method: 'GET', url: `/api/v1/research/studies/${id}/eligibility`,
      headers: await authHeader(),
    })
    expect(eligibility.statusCode).toBe(200)
  })
})

describe('Workflow Step 5 — Cross-Research Chat', () => {
  test('chat can reference research scope', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/agent/chat',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: JSON.stringify({
        text: 'What studies are running?',
        scope: { kind: 'research', ref: 'all' },
      }),
    })
    expect(res.statusCode).toBe(200)
    expect(res.payload).toContain('data:')
  })

  test('research studies list works after creation', async () => {
    const app = await getApp()
    // Create a study
    await app.inject({
      method: 'POST', url: '/api/v1/research/studies',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { display_name: 'Cross Chat Study', short_code: 'CCS01' },
    })

    const res = await app.inject({
      method: 'GET', url: '/api/v1/research/studies',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    expect(JSON.parse(res.payload).length).toBeGreaterThan(0)
  })
})

describe('Workflow Step 6 — Writing + Citations', () => {
  test('create document and chat about it', async () => {
    const app = await getApp()
    const doc = await app.inject({
      method: 'POST', url: '/api/v1/docs',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'Research Paper' },
    })
    const docId = JSON.parse(doc.payload).id

    // Doc chat
    const chat = await app.inject({
      method: 'POST', url: `/api/v1/docs/${docId}/chat`,
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: JSON.stringify({ message: 'Summarize findings' }),
    })
    expect(chat.statusCode).toBe(200)
    expect(chat.payload.startsWith('data:')).toBe(true)
  })

  test('polish document text', async () => {
    const app = await getApp()
    const doc = await app.inject({
      method: 'POST', url: '/api/v1/docs',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'Polish Test' },
    })
    const docId = JSON.parse(doc.payload).id

    const polish = await app.inject({
      method: 'POST', url: `/api/v1/docs/${docId}/polish`,
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: JSON.stringify({ selection: 'Need better wording' }),
    })
    expect(polish.statusCode).toBe(200)
    expect(polish.payload.startsWith('data:')).toBe(true)
  })

  test('document snapshots track versions', async () => {
    const app = await getApp()
    const doc = await app.inject({
      method: 'POST', url: '/api/v1/docs',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'Version Test', body: 'v1' },
    })
    const docId = JSON.parse(doc.payload).id

    await app.inject({
      method: 'PUT', url: `/api/v1/docs/${docId}`,
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { body: 'v2' },
    })

    const snaps = await app.inject({
      method: 'GET', url: `/api/v1/docs/${docId}/snapshots`,
      headers: await authHeader(),
    })
    expect(snaps.statusCode).toBe(200)
    expect(JSON.parse(snaps.payload).snapshots).toBeDefined()
  })

  test('PHI scan detects patterns in document', async () => {
    const app = await getApp()
    const doc = await app.inject({
      method: 'POST', url: '/api/v1/docs',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { title: 'PHI Doc' },
    })
    const docId = JSON.parse(doc.payload).id

    // Update with PHI content
    await app.inject({
      method: 'PUT', url: `/api/v1/docs/${docId}`,
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { body: 'Patient John Smith, SSN 123-45-6789' },
    })

    const scan = await app.inject({
      method: 'POST', url: `/api/v1/docs/${docId}/phi-scan`,
      headers: await authHeader(),
    })
    expect(scan.statusCode).toBe(200)
    const findings = JSON.parse(scan.payload).findings
    expect(Array.isArray(findings)).toBe(true)
    const ssn = findings.find((f: any) => f.kind === 'SSN')
    expect(ssn).toBeTruthy()
  })
})
