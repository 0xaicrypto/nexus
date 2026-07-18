import { deepseekChat, getApiKey } from '../../common/llm.js'
import prisma from '../../common/prisma.js'
import fs from 'fs'
import path from 'path'

/**
 * Clinical Analysis Service — extracted from uploads and chats
 * Auto-updates patient records with findings
 */

export interface ClinicalFinding {
  finding_type: string  // 'diagnosis', 'lab_result', 'imaging', 'medication', 'symptom'
  content: string
  confidence: number
}

export async function analyzeUploadForPatient(
  userId: string, fileId: string
): Promise<ClinicalFinding[]> {
  const dir = path.join(process.env.TWIN_BASE_DIR || '.nexus/twins', userId, 'uploads')
  const filepath = path.join(dir, fileId)
  if (!fs.existsSync(filepath)) return []

  const text = fs.readFileSync(filepath, 'utf-8').slice(0, 3000)
  const prompt = `Extract clinical findings from this medical document. Return ONLY a JSON array:
[{"finding_type": "diagnosis|lab_result|imaging|medication|symptom", "content": "short finding", "confidence": 0.0-1.0}]

Document:
${text}`

  try {
    const result = await deepseekChat([{ role: 'user', content: prompt }], getApiKey())
    const match = result.match(/\[[\s\S]*\]/)
    return match ? JSON.parse(match[0]) : []
  } catch {
    return []
  }
}

export async function updatePatientFromFindings(
  userId: string, patientHash: string, findings: ClinicalFinding[]
): Promise<void> {
  if (!findings.length) return

  const patient = await (prisma as any).patientRecord.findFirst({
    where: { hash: patientHash, userId },
  })
  if (!patient) return

  const existingComplaint = patient.chiefComplaint || ''
  const newFindings = findings
    .filter(f => f.confidence > 0.5)
    .map(f => `[${f.finding_type}] ${f.content}`)
    .join('; ')

  await (prisma as any).patientRecord.update({
    where: { hash: patientHash },
    data: {
      chiefComplaint: existingComplaint
        ? `${existingComplaint} | ${newFindings}`
        : newFindings,
      updatedAt: new Date().toISOString(),
    },
  })
}

export async function analyzeChatForPatient(
  userId: string, patientHash: string, messages: string
): Promise<ClinicalFinding[]> {
  const prompt = `Extract clinical findings from this doctor-patient conversation. Return ONLY a JSON array:
[{"finding_type": "diagnosis|lab_result|imaging|medication|symptom", "content": "short finding", "confidence": 0.0-1.0}]

Conversation:
${messages.slice(0, 3000)}`

  try {
    const result = await deepseekChat([{ role: 'user', content: prompt }], getApiKey())
    const match = result.match(/\[[\s\S]*\]/)
    return match ? JSON.parse(match[0]) : []
  } catch {
    return []
  }
}
