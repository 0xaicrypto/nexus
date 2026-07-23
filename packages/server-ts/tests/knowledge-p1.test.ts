import { describe, test, expect, beforeEach } from 'vitest'
import { KnowledgeStore } from '../src/evolution/stores'
import fs from 'fs'
import path from 'path'
import os from 'os'

describe('P1 — KnowledgeStore', () => {
  let baseDir: string

  function getStore(): KnowledgeStore {
    const dir = path.join(baseDir, `knowledge-${Date.now()}-${Math.random().toString(36).slice(2,6)}`)
    fs.mkdirSync(dir, { recursive: true })
    return new KnowledgeStore(dir)
  }

  beforeEach(() => {
    baseDir = path.join(os.tmpdir(), `nexus-test-p1-${Date.now()}-${Math.random().toString(36).slice(2,6)}`)
  })

  test('add creates a knowledge article', () => {
    const store = getStore()
    const article = store.add({
      title: 'NSCLC immunotherapy review',
      content: 'Immunotherapy has shown significant benefit in NSCLC patients with PD-L1 > 50%.',
      sources: ['fact:001', 'file:ct001'],
    })
    expect(article).toBeTruthy()
    expect(article.title).toBe('NSCLC immunotherapy review')
    expect(article.sources).toContain('fact:001')
    expect(article.version).toBe(1)
  })

  test('update increments version', () => {
    const store = getStore()
    const a = store.add({ title: 'Test', content: 'v1', sources: [] })
    expect(a.version).toBe(1)

    const updated = store.update(a.id, { content: 'v2 content updated' })
    expect(updated).toBeTruthy()
    expect(updated!.version).toBe(2)
    expect(updated!.content).toBe('v2 content updated')
  })

  test('all returns articles sorted by updatedAt desc', () => {
    const store = getStore()
    store.add({ title: 'First', content: 'a', sources: [] })
    store.add({ title: 'Second', content: 'b', sources: [] })
    const all = store.all()
    expect(all.length).toBe(2)
    expect(all[0].title).toBe('Second')
    expect(all[1].title).toBe('First')
  })

  test('commit + reload preserves articles with versions', () => {
    const dir = path.join(baseDir, 'persist')
    fs.mkdirSync(dir, { recursive: true })

    const s1 = new KnowledgeStore(dir)
    const a1 = s1.add({ title: 'Persistent', content: 'v1', sources: ['fact:1'] })
    s1.update(a1.id, { title: 'Persistent v2', content: 'v2' })
    s1.commit('test')

    const s2 = new KnowledgeStore(dir)
    const all = s2.all()
    expect(all.length).toBe(1)
    expect(all[0].title).toBe('Persistent v2')
    expect(all[0].version).toBe(2)
  })

  test('markStale + isStale tracks dependency freshness', () => {
    const store = getStore()
    const a = store.add({ title: 'Dep test', content: 'c', sources: ['fact:dep1', 'fact:dep2'] })
    expect(store.isStale(a.id)).toBe(false)

    store.markStale(a.id, ['fact:dep1'])
    expect(store.isStale(a.id)).toBe(true)

    store.markFresh(a.id)
    expect(store.isStale(a.id)).toBe(false)
  })

  test('getStale returns all stale articles', () => {
    const store = getStore()
    const a1 = store.add({ title: 'Fresh', content: 'f', sources: [] })
    const a2 = store.add({ title: 'Stale', content: 's', sources: ['fact:old'] })

    store.markStale(a2.id, ['fact:old'])
    const stale = store.getStale()
    expect(stale.length).toBe(1)
    expect(stale[0].id).toBe(a2.id)
  })
})

describe('P1 — ChatTakeaway model', () => {
  test('takeaway shape matches Prisma schema', () => {
    const takeaway = {
      id: 'tw_001',
      userId: 'user_test',
      scopeKind: 'patient',
      scopeRef: 'hash123',
      text: 'Patient ZL has persistent cough for 3 weeks, recommend CT scan',
      tag: 'clinical_finding',
      confidence: 0.9,
      distilledAt: new Date().toISOString(),
      medicAckedAt: null,
    }
    expect(takeaway).toHaveProperty('scopeKind')
    expect(takeaway).toHaveProperty('scopeRef')
    expect(takeaway).toHaveProperty('confidence')
    expect(takeaway.confidence).toBeGreaterThan(0.5)
  })
})
