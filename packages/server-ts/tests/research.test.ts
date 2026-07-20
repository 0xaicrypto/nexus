import { describe, test, expect } from 'vitest'
import { getApp, authHeader } from './setup.js'

describe('Research', () => {
  test('create study with valid data', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/research/studies',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { display_name: 'Lung Cancer Phase II', short_code: 'LC002' },
    })
    expect(res.statusCode).toBe(200)
    const body = JSON.parse(res.payload)
    expect(body.study_id).toBeTruthy()
    expect(body.display_name).toBe('Lung Cancer Phase II')
    expect(body.short_code).toBe('LC002')
  })

  test('reject space in short_code', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/research/studies',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { display_name: 'Bad', short_code: 'code with spaces' },
    })
    expect(res.statusCode).toBe(400)
  })

  test('reject missing short_code', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/research/studies',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { display_name: 'Incomplete' },
    })
    expect(res.statusCode).toBe(400)
  })

  test('reject empty display_name', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'POST', url: '/api/v1/research/studies',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { display_name: '', short_code: 'EMPTY' },
    })
    expect(res.statusCode).toBe(400)
  })

  test('enroll and unenroll patient', async () => {
    const app = await getApp()
    // Create a patient first
    const patient = await app.inject({
      method: 'POST', url: '/api/v1/dicom/patients/register-manual',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { initials: 'TP', age: 45, sex: 'M', chief_complaint: 'Test' },
    })
    const patientHash = JSON.parse(patient.payload).patient_hash

    // Create study first
    const study = await app.inject({
      method: 'POST', url: '/api/v1/research/studies',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { display_name: 'Enrollment Test', short_code: 'ET001' },
    })
    const studyId = JSON.parse(study.payload).study_id

    // Enroll
    const enroll = await app.inject({
      method: 'POST', url: `/api/v1/research/studies/${studyId}/enrollments`,
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { patient_hash: patientHash, arm: 'Arm A' },
    })
    expect(enroll.statusCode).toBe(200)
    expect(JSON.parse(enroll.payload).patient_hash).toBe(patientHash)

    // Roster should have 1
    const roster = await app.inject({
      method: 'GET', url: `/api/v1/research/studies/${studyId}/roster`,
      headers: await authHeader(),
    })
    expect(JSON.parse(roster.payload).length).toBe(1)

    // Unenroll
    const unenroll = await app.inject({
      method: 'DELETE', url: `/api/v1/research/studies/${studyId}/enrollments/${patientHash}`,
      headers: await authHeader(),
    })
    expect(unenroll.statusCode).toBe(200)

    // Roster should be empty now
    const empty = await app.inject({
      method: 'GET', url: `/api/v1/research/studies/${studyId}/roster`,
      headers: await authHeader(),
    })
    expect(JSON.parse(empty.payload).length).toBe(0)
  })

  test('get study detail returns full data', async () => {
    const app = await getApp()
    const study = await app.inject({
      method: 'POST', url: '/api/v1/research/studies',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { display_name: 'Detail Test', short_code: 'DT001' },
    })
    const studyId = JSON.parse(study.payload).study_id

    const res = await app.inject({
      method: 'GET', url: `/api/v1/research/studies/${studyId}`,
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    const body = JSON.parse(res.payload)
    expect(body.study_id).toBe(studyId)
    expect(body.display_name).toBe('Detail Test')
  })

  test('safety status returns rules', async () => {
    const app = await getApp()
    const study = await app.inject({
      method: 'POST', url: '/api/v1/research/studies',
      headers: { ...await authHeader(), 'content-type': 'application/json' },
      payload: { display_name: 'Safety Test', short_code: 'ST001' },
    })
    const studyId = JSON.parse(study.payload).study_id

    const res = await app.inject({
      method: 'GET', url: `/api/v1/research/studies/${studyId}/safety/stop-rule-status`,
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(200)
    const body = JSON.parse(res.payload)
    expect(body.triggered_rules).toBeDefined()
  })

  test('non-existent study returns 404', async () => {
    const app = await getApp()
    const res = await app.inject({
      method: 'GET', url: '/api/v1/research/studies/nonexistent_study_id_xyz',
      headers: await authHeader(),
    })
    expect(res.statusCode).toBe(404)
  })
})
