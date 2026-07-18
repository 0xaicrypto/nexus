import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard'
import prisma from '../../common/prisma'
import fs from 'fs'
import path from 'path'

export async function filesRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  // ── Upload ──
  app.post('/api/v1/files/upload', async (request, reply) => {
    const data = await request.file()
    if (!data) return reply.status(400).send({ error: 'No file uploaded' })

    const uploadDir = path.join(process.env.TWIN_BASE_DIR || '.nexus/twins', request.user!.userId, 'uploads')
    fs.mkdirSync(uploadDir, { recursive: true })
    const filename = `${Date.now()}_${data.filename}`
    const filepath = path.join(uploadDir, filename)

    const buffer = await data.toBuffer()
    fs.writeFileSync(filepath, buffer)

    return {
      fileId: filename,
      filename: data.filename,
      contentType: data.mimetype,
      sizeBytes: buffer.length,
      uploadedAt: new Date().toISOString(),
    }
  })

  // ── List ──
  app.get('/api/v1/files', async (request) => {
    const userId = request.user!.userId
    const dir = path.join(process.env.TWIN_BASE_DIR || '.nexus/twins', userId, 'uploads')
    if (!fs.existsSync(dir)) return []

    const { patientHash, limit } = request.query as any
    const files = fs.readdirSync(dir)
      .map(f => {
        const stat = fs.statSync(path.join(dir, f))
        const parts = f.split('_')
        return {
          id: f,
          filename: parts.slice(1).join('_') || f,
          contentType: 'application/octet-stream',
          sizeBytes: stat.size,
          patientHash: patientHash || null,
          createdAt: stat.birthtime.toISOString(),
        }
      })
      .sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime())

    return (limit ? files.slice(0, parseInt(limit)) : files)
  })

  // ── Delete ──
  app.delete('/api/v1/files/:fileId', async (request, reply) => {
    const { fileId } = request.params as any
    const filepath = path.join(process.env.TWIN_BASE_DIR || '.nexus/twins', request.user!.userId, 'uploads', fileId)
    if (fs.existsSync(filepath)) {
      fs.unlinkSync(filepath)
      return { deleted: true }
    }
    return reply.status(404).send({ error: 'File not found' })
  })
}
