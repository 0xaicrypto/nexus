import fs from 'fs'
import path from 'path'

export class VersionedStore {
  private baseDir: string
  private width: number

  constructor(baseDir: string, width = 4) {
    this.baseDir = path.resolve(baseDir)
    fs.mkdirSync(this.baseDir, { recursive: true })
    this.width = width
  }

  current(): unknown | null {
    const ptr = this.readPointer()
    if (!ptr) return null
    return this.readVersion(ptr.version)
  }

  currentVersion(): string | null {
    const ptr = this.readPointer()
    return ptr?.version ?? null
  }

  propose(data: unknown): string {
    const highest = this.highestExisting()
    const label = `v${String(highest + 1).padStart(this.width, '0')}`
    this.writeVersion(label, data)
    this.writePointer(label)
    return label
  }

  rollback(version: string): string {
    if (!this.versionExists(version)) {
      throw new Error(`Version ${version} not found in ${this.baseDir}`)
    }
    const prev = this.currentVersion() || ''
    this.writePointer(version)
    return prev
  }

  history(limit?: number): Array<{ version: string; createdAt: number }> {
    const records: Array<{ version: string; createdAt: number }> = []
    for (const entry of fs.readdirSync(this.baseDir)) {
      const match = entry.match(/^v(\d+)\.json$/)
      if (!match) continue
      records.push({
        version: entry.replace('.json', ''),
        createdAt: fs.statSync(path.join(this.baseDir, entry)).mtimeMs,
      })
    }
    records.sort((a, b) => parseInt(a.version.slice(1)) - parseInt(b.version.slice(1)))
    return limit ? records.slice(0, limit) : records
  }

  private pointerPath() { return path.join(this.baseDir, '_current.json') }

  private readPointer(): { version: string; updatedAt: number } | null {
    const p = this.pointerPath()
    if (!fs.existsSync(p)) return null
    return JSON.parse(fs.readFileSync(p, 'utf-8'))
  }

  private writePointer(version: string) {
    const tmp = this.pointerPath() + '.tmp'
    fs.writeFileSync(tmp, JSON.stringify({ version, updatedAt: Date.now() / 1000 }))
    fs.renameSync(tmp, this.pointerPath())
  }

  private readVersion(version: string): unknown | null {
    const p = path.join(this.baseDir, `${version}.json`)
    if (!fs.existsSync(p)) return null
    return JSON.parse(fs.readFileSync(p, 'utf-8'))
  }

  private writeVersion(version: string, data: unknown) {
    const p = path.join(this.baseDir, `${version}.json`)
    if (fs.existsSync(p)) throw new Error(`Version ${version} already exists`)
    fs.writeFileSync(p, JSON.stringify(data, null, 2), 'utf-8')
  }

  private versionExists(version: string): boolean {
    return fs.existsSync(path.join(this.baseDir, `${version}.json`))
  }

  private highestExisting(): number {
    let highest = 0
    for (const entry of fs.readdirSync(this.baseDir)) {
      const match = entry.match(/^v(\d+)\.json$/)
      if (match) highest = Math.max(highest, parseInt(match[1]))
    }
    return highest
  }
}
