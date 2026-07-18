import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard.js'
import prisma from '../../common/prisma.js'

// Skill catalog
const CATALOG = [
  { identifier: 'official/clinical-summary', name: 'Clinical Summary', description: 'Generate structured clinical summaries from patient data', source: 'official', version: '1.0', author: 'Heurion' },
  { identifier: 'official/safety-monitor', name: 'Safety Monitor', description: 'Track adverse events and DLTs across study arms', source: 'official', version: '1.0', author: 'Heurion' },
  { identifier: 'official/eligibility-check', name: 'Eligibility Check', description: 'Auto-check patient eligibility against protocol criteria', source: 'official', version: '1.0', author: 'Heurion' },
  { identifier: 'github/imaging-report', name: 'Imaging Report', description: 'Generate structured radiology reports from DICOM findings', source: 'github', version: '0.9', author: 'community' },
  { identifier: 'github/med-review', name: 'Medication Review', description: 'Review medication lists for interactions and contraindications', source: 'github', version: '0.8', author: 'community' },
  { identifier: 'github/trial-matching', name: 'Trial Matching', description: 'Match patients to eligible clinical trials based on profile', source: 'github', version: '0.7', author: 'community' },
  { identifier: 'anthropic/diagnostic-reasoning', name: 'Diagnostic Reasoning', description: 'Step-by-step differential diagnosis from findings', source: 'anthropic', version: '1.0', author: 'Anthropic' },
  { identifier: 'anthropic/patient-education', name: 'Patient Education', description: 'Generate patient-friendly explanations of conditions', source: 'anthropic', version: '1.0', author: 'Anthropic' },
]

export async function skillsRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  // ── List installed (frontend expects { skills: [...] }) ──
  app.get('/api/v1/skills', async (request) => {
    const prefs = await (prisma as any).userSkillPref.findMany({
      where: { userId: request.user!.userId },
    })
    const installed = new Set(prefs.map((p: any) => p.skillName))
    const enabledMap = new Map(prefs.map((p: any) => [p.skillName, p.enabled === 1]))
    const skills = CATALOG
      .filter(s => installed.has(s.name))
      .map(s => ({
        name: s.name,
        title: s.name,
        description: s.description,
        version: s.version,
        author: s.author,
        enabled: enabledMap.get(s.name) ?? true,
      }))
    return { skills }
  })

  // ── Search marketplace (frontend expects { results: [...] }) ──
  app.get('/api/v1/skills/search', async (request) => {
    const { query, source } = request.query as any
    const q = (query || '').toLowerCase()
    const src = source || 'all'
    const prefs = await (prisma as any).userSkillPref.findMany({
      where: { userId: request.user!.userId },
    })
    const installed = new Set(prefs.map((p: any) => p.skillName))
    const results = CATALOG
      .filter(s => src === 'all' || s.source === src)
      .filter(s => !q || s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q))
      .map(s => ({
        identifier: s.identifier,
        name: s.name,
        description: s.description,
        source: s.source,
        installed: installed.has(s.name),
        version: s.version,
        author: s.author,
      }))
    return { results }
  })

  // ── Install ──
  app.post('/api/v1/skills/install', async (request) => {
    const { identifier } = request.body as any
    const skill = CATALOG.find(s => s.identifier === identifier)
    const name = skill?.name || identifier.split('/').pop()
    const source = skill?.source || 'manual'
    const now = new Date().toISOString()
    await (prisma as any).userSkillPref.upsert({
      where: { userId_skillName: { userId: request.user!.userId, skillName: name } },
      update: { enabled: 1 },
      create: { userId: request.user!.userId, skillName: name, enabled: 1, autoApply: 0, source, createdAt: now },
    })
    return { name }
  })

  // ── Toggle ──
  app.post('/api/v1/skills/:name/toggle', async (request) => {
    const { name } = request.params as any
    const { enabled } = request.body as any
    await (prisma as any).userSkillPref.upsert({
      where: { userId_skillName: { userId: request.user!.userId, skillName: name } },
      update: { enabled: enabled ? 1 : 0 },
      create: { userId: request.user!.userId, skillName: name, enabled: enabled ? 1 : 0, source: 'manual', createdAt: new Date().toISOString() },
    })
    return { name, enabled }
  })

  // ── Uninstall ──
  app.delete('/api/v1/skills/:name', async (request) => {
    const { name } = request.params as any
    try { await (prisma as any).userSkillPref.delete({ where: { userId_skillName: { userId: request.user!.userId, skillName: name } } }) } catch { /* ok */ }
    return { uninstalled: true }
  })
}
