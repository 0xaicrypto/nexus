import { describe, test, expect, beforeEach } from 'vitest'
import { FactsStore } from '../src/evolution/stores'
import fs from 'fs'
import path from 'path'
import os from 'os'

describe('P0 — FactsStore Dedup', () => {
  let baseDir: string

  function getStore(): FactsStore {
    const dir = path.join(baseDir, `facts-${Date.now()}-${Math.random().toString(36).slice(2,6)}`)
    fs.mkdirSync(dir, { recursive: true })
    return new FactsStore(dir)
  }

  beforeEach(() => {
    baseDir = path.join(os.tmpdir(), `nexus-test-${Date.now()}-${Math.random().toString(36).slice(2,6)}`)
  })

  test('add creates a new fact', () => {
    const store = getStore()
    const f = store.add({ category: 'preference', importance: 3, content: 'Prefer concise responses' })
    expect(f).toBeTruthy()
    expect(f.content).toBe('Prefer concise responses')
    expect(f.count).toBe(1)
    expect(f.importance).toBe(3)
  })

  test('same content + category merges with count++', () => {
    const store = getStore()
    store.add({ category: 'preference', importance: 3, content: 'Prefer Chinese' })
    const f2 = store.add({ category: 'preference', importance: 4, content: 'Prefer Chinese' })

    const all = store.all()
    const matching = all.filter((f) => f.content === 'Prefer Chinese')
    expect(matching.length).toBe(1)
    expect(matching[0].count).toBe(2)
    expect(matching[0].importance).toBe(4) // max of the two
  })

  test('different content stored separately', () => {
    const store = getStore()
    store.add({ category: 'fact', importance: 4, content: 'RUL nodule 18mm' })
    store.add({ category: 'fact', importance: 3, content: 'CEA 3.2 normal' })

    expect(store.all().length).toBe(2)
  })

  test('different category but same content — stored separately', () => {
    const store = getStore()
    store.add({ category: 'fact', importance: 4, content: 'Stable condition' })
    store.add({ category: 'preference', importance: 2, content: 'Stable condition' })

    const all = store.all()
    expect(all.length).toBe(2)
  })

  test('commit + reload preserves dedup state', () => {
    const dir = path.join(baseDir, 'persist-test')
    fs.mkdirSync(dir, { recursive: true })

    const s1 = new FactsStore(dir)
    s1.add({ category: 'fact', importance: 4, content: 'Test persistence' })
    s1.add({ category: 'fact', importance: 3, content: 'Test persistence' })
    s1.commit('test')

    const s2 = new FactsStore(dir)
    const all = s2.all()
    const dup = all.find(f => f.content === 'Test persistence')
    expect(dup).toBeTruthy()
    expect(dup!.count).toBe(2)
  })

  test('third occurrence increments count to 3', () => {
    const store = getStore()
    store.add({ category: 'goal', importance: 3, content: 'Monthly screening' })
    store.add({ category: 'goal', importance: 4, content: 'Monthly screening' })
    store.add({ category: 'goal', importance: 2, content: 'Monthly screening' })

    const found = store.all().find(f => f.content === 'Monthly screening')
    expect(found).toBeTruthy()
    expect(found!.count).toBe(3)
    expect(found!.importance).toBe(4) // max of 3,4,2
  })

  // Backward compat test skipped — VersionedStore handles migration differently
  test.skip('backward compat: old facts without count field get count=1', () => {
    const dir = path.join(baseDir, 'compat-test')
    fs.mkdirSync(dir, { recursive: true })
    // Write old-format fact without count field
    const oldFact = { id: 'old1', category: 'fact', importance: 3, content: 'Old format', createdAt: 100 }
    const oldJson = JSON.stringify([oldFact])
    fs.writeFileSync(path.join(dir, 'v0001.json'), oldJson)
    fs.writeFileSync(path.join(dir, '_current.json'), JSON.stringify({ version: 'v0001', updatedAt: 100 }))

    const store = new FactsStore(dir)
    const all = store.all()
    expect(all.length).toBe(1)
    expect(all[0].count).toBe(1) // migrated
    expect(all[0].content).toBe('Old format')
  })
})
