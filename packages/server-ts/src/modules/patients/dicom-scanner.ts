import fs from 'fs'
import path from 'path'

// Pure TypeScript DICOM parser — no Python needed
let dicomParser: any = null
try { dicomParser = require('dicom-parser') } catch { /* optional */ }

export interface DicomFinding {
  type: string
  content: string
}

export function quickScanDicom(userId: string, fileId: string): DicomFinding[] {
  const dir = path.join(process.env.TWIN_BASE_DIR || '.nexus/twins', userId, 'uploads')
  const filepath = path.join(dir, fileId)

  if (!fs.existsSync(filepath)) {
    return [{ type: 'error', content: 'File not found' }]
  }

  // Try dicom-parser first
  if (dicomParser) {
    try {
      const buffer = fs.readFileSync(filepath)
      const arrayBuffer = buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength)
      const dataSet = dicomParser.parseDicom(new Uint8Array(arrayBuffer))

      const findings: DicomFinding[] = []

      const getTag = (tag: string) => {
        try { return dataSet.string(tag) } catch { return null }
      }
      const getTagNum = (tag: string) => {
        try { return dataSet.uint16(tag) } catch { return 0 }
      }

      const name = getTag('x00100010')
      const id = getTag('x00100020')
      const sex = getTag('x00100040')
      const age = getTag('x00101010')
      if (name || id) {
        findings.push({ type: 'patient', content: `Patient: ${name || '?'}, ID: ${id || '?'}, Sex: ${sex || '?'}, Age: ${age || '?'}` })
      }

      const studyDesc = getTag('x00081030')
      const studyDate = getTag('x00080020')
      const modality = getTag('x00080060')
      if (studyDesc) {
        findings.push({ type: 'study', content: `Study: ${studyDesc}, Date: ${studyDate || '?'}, Modality: ${modality || '?'}` })
      }

      const institution = getTag('x00080080')
      const manufacturer = getTag('x00080070')
      const model = getTag('x00081090')
      if (institution || manufacturer) {
        findings.push({ type: 'institution', content: `Institution: ${institution || '?'}, Device: ${manufacturer || '?'} ${model || ''}` })
      }

      const rows = getTagNum('x00280010')
      const cols = getTagNum('x00280011')
      const sliceThickness = getTag('x00180050')
      const windowCenter = getTag('x00281050')
      const windowWidth = getTag('x00281051')
      if (rows > 0) {
        findings.push({ type: 'image', content: `Image: ${rows}x${cols}${sliceThickness ? `, Slice: ${sliceThickness}mm` : ''}${windowCenter ? `, Window: ${windowCenter}/${windowWidth}` : ''}` })
      }

      const comments = getTag('x00204000')
      if (comments) {
        findings.push({ type: 'findings', content: comments })
      }

      // Count number of tags found
      const tagCount = Object.keys(dataSet.elements || {}).length
      findings.push({ type: 'meta', content: `DICOM parsed: ${tagCount} tags extracted` })

      return findings
    } catch (e: any) {
      return [{ type: 'error', content: `DICOM parse error: ${e.message}` }]
    }
  }

  // Fallback: try reading as text (works for text reports)
  try {
    const text = fs.readFileSync(filepath, 'utf-8').slice(0, 5000)
    if (text.trim()) {
      return [{ type: 'text_content', content: text }]
    }
  } catch {
    return [{ type: 'error', content: 'Cannot read file — try uploading as .txt report' }]
  }

  return [{ type: 'error', content: 'dicom-parser not installed' }]
}
