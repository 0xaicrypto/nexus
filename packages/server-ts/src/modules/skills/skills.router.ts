import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard'
import prisma from '../../common/prisma'

// #3: Expanded skill catalog (30+ skills)
const CATALOG = [
  // Clinical
  { identifier: 'official/clinical-summary', name: 'Clinical Summary', description: 'Generate structured clinical summaries from patient encounters', source: 'official', version: '1.2', author: 'Heurion' },
  { identifier: 'official/safety-monitor', name: 'Safety Monitor', description: 'Track adverse events and DLTs across study arms with auto-alerting', source: 'official', version: '1.0', author: 'Heurion' },
  { identifier: 'official/eligibility-check', name: 'Eligibility Check', description: 'Auto-check patient eligibility against protocol inclusion/exclusion criteria', source: 'official', version: '1.0', author: 'Heurion' },
  { identifier: 'official/differential-diagnosis', name: 'Differential Diagnosis', description: 'Generate ranked differential diagnosis from patient findings and history', source: 'official', version: '1.0', author: 'Heurion' },
  { identifier: 'official/treatment-plan', name: 'Treatment Plan', description: 'Generate evidence-based treatment plans with NCCN guideline references', source: 'official', version: '1.1', author: 'Heurion' },
  { identifier: 'official/soap-note', name: 'SOAP Note', description: 'Generate structured SOAP notes from clinical encounters', source: 'official', version: '1.0', author: 'Heurion' },
  // Imaging
  { identifier: 'github/imaging-report', name: 'Imaging Report', description: 'Generate structured radiology reports from DICOM and clinical context', source: 'github', version: '0.9', author: 'rad-ai' },
  { identifier: 'github/chest-xray-reader', name: 'Chest X-Ray Reader', description: 'Preliminary chest X-ray interpretation with finding detection', source: 'github', version: '0.8', author: 'med-ai' },
  { identifier: 'github/ct-nodule-detection', name: 'CT Nodule Detection', description: 'Lung nodule detection and measurement from CT series', source: 'github', version: '0.7', author: 'oncology-ai' },
  { identifier: 'github/mri-brain-seg', name: 'MRI Brain Segmentation', description: 'Brain tumor segmentation and volumetric analysis from MRI', source: 'github', version: '0.6', author: 'neuro-ai' },
  // Medication
  { identifier: 'github/med-review', name: 'Medication Review', description: 'Review medication lists for interactions and contraindications', source: 'github', version: '0.8', author: 'pharm-ai' },
  { identifier: 'github/dosing-calculator', name: 'Dosing Calculator', description: 'Calculate body-surface-area and weight-based dosing for oncology drugs', source: 'github', version: '1.0', author: 'pharm-ai' },
  { identifier: 'github/polypharmacy-check', name: 'Polypharmacy Check', description: 'Identify potential issues in patients on 5+ concurrent medications', source: 'github', version: '0.5', author: 'geri-ai' },
  // Research
  { identifier: 'github/trial-matching', name: 'Trial Matching', description: 'Match patients to eligible clinical trials based on profile and biomarkers', source: 'github', version: '0.7', author: 'research-ai' },
  { identifier: 'github/protocol-parser', name: 'Protocol Parser', description: 'Parse clinical trial protocols from PDF/DOCX into structured data', source: 'github', version: '0.6', author: 'research-ai' },
  { identifier: 'github/consort-generator', name: 'CONSORT Generator', description: 'Generate CONSORT flow diagrams from study enrollment data', source: 'github', version: '0.8', author: 'research-ai' },
  { identifier: 'github/kaplan-meier', name: 'Kaplan-Meier Plot', description: 'Generate survival curves from time-to-event data', source: 'github', version: '0.9', author: 'stats-ai' },
  // Writing
  { identifier: 'anthropic/diagnostic-reasoning', name: 'Diagnostic Reasoning', description: 'Step-by-step differential diagnosis from clinical findings', source: 'anthropic', version: '1.0', author: 'Anthropic' },
  { identifier: 'anthropic/patient-education', name: 'Patient Education', description: 'Generate patient-friendly explanations of medical conditions and treatments', source: 'anthropic', version: '1.0', author: 'Anthropic' },
  { identifier: 'anthropic/literature-review', name: 'Literature Review', description: 'Summarize recent literature on a given clinical topic from PubMed', source: 'anthropic', version: '1.1', author: 'Anthropic' },
  { identifier: 'anthropic/guideline-synthesis', name: 'Guideline Synthesis', description: 'Synthesize recommendations across NCCN/ASCO/ESMO guidelines', source: 'anthropic', version: '1.0', author: 'Anthropic' },
  { identifier: 'anthropic/informed-consent', name: 'Informed Consent', description: 'Generate plain-language informed consent documents for clinical trials', source: 'anthropic', version: '0.9', author: 'Anthropic' },
  // Quality
  { identifier: 'github/note-quality', name: 'Note Quality Audit', description: 'Audit clinical notes for completeness, clarity, and medico-legal compliance', source: 'github', version: '0.7', author: 'quality-ai' },
  { identifier: 'github/coding-assist', name: 'ICD-10 Coding', description: 'Suggest ICD-10 codes from clinical documentation', source: 'github', version: '0.8', author: 'coding-ai' },
  { identifier: 'github/billing-review', name: 'Billing Review', description: 'Review clinical documentation for appropriate billing level support', source: 'github', version: '0.6', author: 'billing-ai' },
  // Communication
  { identifier: 'github/referral-letter', name: 'Referral Letter', description: 'Generate structured referral letters with key clinical details', source: 'github', version: '0.8', author: 'comm-ai' },
  { identifier: 'github/discharge-summary', name: 'Discharge Summary', description: 'Generate comprehensive discharge summaries from hospital course', source: 'github', version: '0.7', author: 'comm-ai' },
  { identifier: 'github/handoff-note', name: 'Handoff Note', description: 'Generate structured sign-out notes for care transitions', source: 'github', version: '0.6', author: 'comm-ai' },
]

export async function skillsRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  app.get('/api/v1/skills', async (request) => {
    const prefs = await (prisma as any).userSkillPref.findMany({ where: { userId: request.user!.userId } })
    const installed = new Map(prefs.map((p: any) => [p.skillName, p]))
    const skills = CATALOG
      .filter(s => installed.has(s.name))
      .map(s => ({ name: s.name, title: s.name, description: s.description, version: s.version, author: s.author, enabled: (installed.get(s.name) as any)?.enabled !== 0 }))
    return { skills }
  })

  // #3: Paginated search with page + page_size
  app.get('/api/v1/skills/search', async (request) => {
    const { query, source, page, page_size } = request.query as any
    const q = (query || '').toLowerCase()
    const src = source || 'all'
    const pageNum = parseInt(page || '1')
    const pageSize = parseInt(page_size || '10')

    const prefs = await (prisma as any).userSkillPref.findMany({ where: { userId: request.user!.userId } })
    const installed = new Set(prefs.map((p: any) => p.skillName))

    let results = CATALOG
      .filter(s => src === 'all' || s.source === src)
      .filter(s => !q || s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q))
      .map(s => ({ identifier: s.identifier, name: s.name, description: s.description, source: s.source, installed: installed.has(s.name), version: s.version, author: s.author }))

    const total = results.length
    const offset = (pageNum - 1) * pageSize
    results = results.slice(offset, offset + pageSize)

    return { results, total, page: pageNum, page_size: pageSize, total_pages: Math.ceil(total / pageSize) }
  })

  app.post('/api/v1/skills/install', async (request) => {
    const { identifier } = request.body as any
    const skill = CATALOG.find(s => s.identifier === identifier)
    const name = skill?.name || identifier.split('/').pop()
    const source = skill?.source || 'manual'
    await (prisma as any).userSkillPref.upsert({
      where: { userId_skillName: { userId: request.user!.userId, skillName: name } },
      update: { enabled: 1 },
      create: { userId: request.user!.userId, skillName: name, enabled: 1, autoApply: 0, source, createdAt: new Date().toISOString() },
    })
    return { name, source }
  })

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

  app.delete('/api/v1/skills/:name', async (request) => {
    const { name } = request.params as any
    try { await (prisma as any).userSkillPref.delete({ where: { userId_skillName: { userId: request.user!.userId, skillName: name } } }) } catch { /* ok */ }
    return { uninstalled: true }
  })
}
