import fs from 'fs'
import path from 'path'

let dicomParser: any = null
let sharp: any = null
try {
  dicomParser = require('dicom-parser')
  sharp = require('sharp')
} catch { /* optional, tsx handles require() */ }

export interface DicomFinding {
  type: string
  content: string
}

export function quickScanDicom(userId: string, fileId: string): DicomFinding[] {
  const filepath = getDicomPath(userId, fileId)
  if (!fs.existsSync(filepath)) return [{ type: 'error', content: 'File not found' }]

  if (dicomParser) {
    try {
      const buffer = fs.readFileSync(filepath)
      const arr = buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength)
      const dataSet = dicomParser.parseDicom(new Uint8Array(arr))
      const findings: DicomFinding[] = []

      const s = (tag: string) => { try { return dataSet.string(tag) } catch { return null } }
      const n = (tag: string) => { try { return dataSet.uint16(tag) } catch { return 0 } }

      const name = s('x00100010'); const id = s('x00100020'); const sex = s('x00100040'); const age = s('x00101010')
      if (name || id) findings.push({ type: 'patient', content: `${name || '?'} | ID:${id || '?'} | ${sex || '?'} | ${age || '?'}` })

      const studyDesc = s('x00081030'); const studyDate = s('x00080020'); const modality = s('x00080060')
      if (studyDesc) findings.push({ type: 'study', content: `${studyDesc} | ${studyDate || '?'} | ${modality || '?'}` })

      const inst = s('x00080080'); const manu = s('x00080070'); const mdl = s('x00081090')
      if (inst || manu) findings.push({ type: 'institution', content: `${inst || '?'} | ${manu || '?'} ${mdl || ''}` })

      const rows = n('x00280010'); const cols = n('x00280011')
      if (rows > 0) findings.push({ type: 'image', content: `${rows}x${cols} | ${s('x00180050') || '?'}mm` })

      const comments = s('x00204000')
      if (comments) findings.push({ type: 'findings', content: comments })

      findings.push({ type: 'meta', content: `${Object.keys(dataSet.elements || {}).length} DICOM tags` })
      return findings
    } catch (e: any) {
      return [{ type: 'error', content: e.message }]
    }
  }
  try {
    const text = fs.readFileSync(filepath, 'utf-8').slice(0, 5000)
    if (text.trim()) return [{ type: 'text_content', content: text }]
  } catch {}
  return [{ type: 'error', content: 'Cannot read' }]
}

/**
 * Render DICOM pixel data as PNG thumbnail (max 256px)
 */
export function renderDicomSlice(userId: string, fileId: string): Buffer | null {
  const filepath = getDicomPath(userId, fileId)
  if (!fs.existsSync(filepath) || !dicomParser) return null

  try {
    const buffer = fs.readFileSync(filepath)
    const arr = buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength)
    const dataSet = dicomParser.parseDicom(new Uint8Array(arr))

    const rows = dataSet.uint16('x00280010')
    const cols = dataSet.uint16('x00280011')
    if (!rows || !cols) return null

    const pixelData = new Uint16Array(dataSet.byteArray.buffer, dataSet.byteArray.byteOffset, rows * cols)
    const wc = parseFloat((dataSet.string('x00281050') || '40').split('\\')[0])
    const ww = parseFloat((dataSet.string('x00281051') || '400').split('\\')[0])
    const ri = parseFloat(dataSet.string('x00281052') || '-1000')
    const rs = parseFloat(dataSet.string('x00281053') || '1')

    // Convert to 8-bit grayscale with window/level
    const gray = Buffer.alloc(rows * cols)
    for (let i = 0; i < rows * cols; i++) {
      const hu = pixelData[i] * rs + ri
      const low = wc - ww / 2
      gray[i] = Math.max(0, Math.min(255, Math.round((hu - low) / ww * 255)))
    }

    // Create BMP if sharp not available (zero-dependency fallback)
    const scale = Math.min(1, 256 / Math.max(rows, cols))
    const outW = Math.floor(cols * scale)
    const outH = Math.floor(rows * scale)
    const rowSize = Math.floor((outW * 3 + 3) / 4) * 4
    const imageSize = rowSize * outH
    const fileSize = 54 + imageSize

    const bmp = Buffer.alloc(fileSize)
    bmp.write('BM', 0)
    bmp.writeUInt32LE(fileSize, 2)
    bmp.writeUInt32LE(54, 10) // offset to pixels
    bmp.writeUInt32LE(40, 14) // DIB header size
    bmp.writeInt32LE(outW, 18)
    bmp.writeInt32LE(outH, 22)
    bmp.writeUInt16LE(1, 26) // planes
    bmp.writeUInt16LE(24, 28) // bpp
    bmp.writeUInt32LE(imageSize, 34)

    for (let y = 0; y < outH; y++) {
      const srcY = Math.floor(y / scale)
      for (let x = 0; x < outW; x++) {
        const srcX = Math.floor(x / scale)
        const val = gray[srcY * cols + srcX]
        const offset = 54 + (outH - 1 - y) * rowSize + x * 3
        bmp[offset] = val     // B
        bmp[offset + 1] = val // G
        bmp[offset + 2] = val // R
      }
    }
    return bmp
  } catch {
    return null
  }
}

function getDicomPath(userId: string, fileId: string): string {
  return path.join(process.env.TWIN_BASE_DIR || '.nexus/twins', userId, 'uploads', fileId)
}
