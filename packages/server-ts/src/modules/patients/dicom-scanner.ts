import fs from 'fs'
import path from 'path'
import zlib from 'zlib'

let dicomParser: any = null
try {
  dicomParser = require('dicom-parser')
} catch { /* optional */ }

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

    // Create PNG (zero dependencies, browser-compatible)
    const scale = Math.min(1, 256 / Math.max(rows, cols))
    const outW = Math.floor(cols * scale)
    const outH = Math.floor(rows * scale)

    // Build raw image data with filter byte 0 per row
    const raw = Buffer.alloc(outH * (1 + outW))
    for (let y = 0; y < outH; y++) {
      raw[y * (1 + outW)] = 0 // filter: none
      const srcY = Math.floor(y / scale)
      for (let x = 0; x < outW; x++) {
        const srcX = Math.floor(x / scale)
        raw[y * (1 + outW) + 1 + x] = gray[srcY * cols + srcX]
      }
    }

    // Deflate the raw data
    const deflated = zlib.deflateSync(raw)

    // Helper: create PNG chunk
    const chunk = (type: string, data: Buffer): Buffer => {
      const len = Buffer.alloc(4); len.writeUInt32BE(data.length, 0)
      const crcData = Buffer.concat([Buffer.from(type), data])
      const crc = crc32(crcData)
      const crcBuf = Buffer.alloc(4); crcBuf.writeUInt32BE(crc, 0)
      return Buffer.concat([len, Buffer.from(type), data, crcBuf])
    }

    // IHDR
    const ihdr = Buffer.alloc(13)
    ihdr.writeUInt32BE(outW, 0); ihdr.writeUInt32BE(outH, 4)
    ihdr[8] = 8  // bit depth
    ihdr[9] = 0  // grayscale
    ihdr[10] = 0 // deflate
    ihdr[11] = 0 // adaptive filter
    ihdr[12] = 0 // non-interlaced

    const png = Buffer.concat([
      Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), // PNG signature
      chunk('IHDR', ihdr),
      chunk('IDAT', deflated),
      chunk('IEND', Buffer.alloc(0)),
    ])

    return png
  } catch {
    return null
  }
}

/**
 * Analyze DICOM image with Gemini Vision
 * Sends the rendered PNG to Gemini for AI-powered finding detection
 */
export async function analyzeWithGeminiVision(userId: string, fileId: string): Promise<string> {
  const filepath = getDicomPath(userId, fileId)
  if (!fs.existsSync(filepath) || !dicomParser) return ''

  try {
    const buffer = fs.readFileSync(filepath)
    const arr = buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength)
    const dataSet = dicomParser.parseDicom(new Uint8Array(arr))
    const rows = dataSet.uint16('x00280010')
    const cols = dataSet.uint16('x00280011')
    if (!rows || !cols) return ''

    const pixelData = new Uint16Array(dataSet.byteArray.buffer, dataSet.byteArray.byteOffset, rows * cols)
    const wc = parseFloat((dataSet.string('x00281050') || '40').split('\\')[0])
    const ww = parseFloat((dataSet.string('x00281051') || '400').split('\\')[0])
    const ri = parseFloat(dataSet.string('x00281052') || '-1000')
    const rs = parseFloat(dataSet.string('x00281053') || '1')

    const gray = Buffer.alloc(rows * cols)
    for (let i = 0; i < rows * cols; i++) {
      const hu = pixelData[i] * rs + ri
      gray[i] = Math.max(0, Math.min(255, Math.round((hu - (wc - ww/2)) / ww * 255)))
    }

    // Render 512x512 PNG with simple header for higher quality
    const scale = Math.min(1, 512 / Math.max(rows, cols))
    const outW = Math.floor(cols * scale)
    const outH = Math.floor(rows * scale)
    const raw = Buffer.alloc(outH * (1 + outW))
    for (let y = 0; y < outH; y++) {
      raw[y * (1 + outW)] = 0
      const srcY = Math.floor(y / scale)
      for (let x = 0; x < outW; x++) {
        raw[y * (1 + outW) + 1 + x] = gray[Math.floor(srcY) * cols + Math.floor(x / scale)]
      }
    }
    const deflated = zlib.deflateSync(raw)
    const chunk = (type: string, data: Buffer): Buffer => {
      const len = Buffer.alloc(4); len.writeUInt32BE(data.length, 0)
      const crc = crc32(Buffer.concat([Buffer.from(type), data]))
      const cb = Buffer.alloc(4); cb.writeUInt32BE(crc, 0)
      return Buffer.concat([len, Buffer.from(type), data, cb])
    }
    const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(outW, 0); ihdr.writeUInt32BE(outH, 4)
    ihdr[8]=8; ihdr[9]=0; ihdr[10]=0; ihdr[11]=0; ihdr[12]=0
    const png = Buffer.concat([
      Buffer.from([137,80,78,71,13,10,26,10]),
      chunk('IHDR', ihdr), chunk('IDAT', deflated), chunk('IEND', Buffer.alloc(0)),
    ])
    const base64 = png.toString('base64')

    // Get Gemini API key from DB
    const { PrismaClient } = require('@prisma/client')
    const prisma = new PrismaClient()
    const setting = await (prisma as any).userSetting.findUnique({
      where: { userId_key: { userId, key: 'gemini_api_key' } },
    })
    await prisma.$disconnect()
    const apiKey = setting?.value || process.env.GEMINI_API_KEY || ''

    if (!apiKey || apiKey.length < 10) return ''

    const resp = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=${apiKey}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          contents: [{
            parts: [
              { text: 'Analyze this chest CT image. List any abnormalities, nodules, masses, or findings in Chinese. Keep it concise (3-5 bullet points).' },
              { inlineData: { mimeType: 'image/png', data: base64 } },
            ],
          }],
        }),
      },
    )
    const data = await resp.json()
    return data?.candidates?.[0]?.content?.parts?.[0]?.text || ''
  } catch (e: any) {
    return ''
  }
}
function getDicomPath(userId: string, fileId: string): string {
  const dir = path.join(process.env.TWIN_BASE_DIR || '.nexus/twins', userId, 'uploads')
  let p = path.join(dir, fileId)
  if (fs.existsSync(p)) return p
  p = path.join(dir, fileId + '.dcm')
  if (fs.existsSync(p)) return p
  return p
}

function crc32(buf: Buffer): number {
  let crc = 0xFFFFFFFF
  for (let i = 0; i < buf.length; i++) {
    crc ^= buf[i]
    for (let j = 0; j < 8; j++) crc = (crc >>> 1) ^ (crc & 1 ? 0xEDB88320 : 0)
  }
  return (crc ^ 0xFFFFFFFF) >>> 0
}
