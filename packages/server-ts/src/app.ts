import { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify'
import fastifyCors from '@fastify/cors'
import fastifyMultipart from '@fastify/multipart'
import { config } from './config.js'
import { authRouter } from './modules/auth/auth.router.js'
import { chatRouter } from './modules/chat/chat.router.js'
import { sessionRouter, agentRouter } from './modules/chat/session-agent.router.js'
import { patientsRouter } from './modules/patients/patients.router.js'
import { researchRouter } from './modules/research/research.router.js'
import { documentsRouter } from './modules/documents/documents.router.js'
import { skillsRouter } from './modules/skills/skills.router.js'
import { settingsRouter } from './modules/settings/settings.router.js'
import { filesRouter } from './modules/files/files.router.js'
import { adminRouter } from './modules/admin/admin.router.js'
import { stubRouter } from './modules/stubs/stubs.router.js'
import { ZodError } from 'zod'

export async function createApp(): Promise<FastifyInstance> {
  const app = require('fastify')({ logger: true })

  // ── Global error handler ──
  app.setErrorHandler((err: Error, _req: FastifyRequest, reply: FastifyReply) => {
    if (err instanceof ZodError) {
      return reply.status(400).send({ error: 'Validation failed', details: err.errors })
    }
    reply.status(500).send({ error: err.message || 'Internal error' })
  })

  // ── Plugins ──
  await app.register(fastifyCors, { origin: config.corsAllowOrigins, credentials: true })
  await app.register(fastifyMultipart, { limits: { fileSize: 100 * 1024 * 1024 } })

  // ── Health + Config ──
  app.get('/healthz', async () => 'ok')
  app.get('/api/v1/config', async () => ({
    appName: 'Heurion', apiVersion: 1, minClientApiVersion: 1, billingEnabled: false,
  }))

  // ── Routes ──
  await app.register(authRouter)
  await app.register(sessionRouter)
  await app.register(agentRouter)
  await app.register(chatRouter)
  await app.register(researchRouter)
  await app.register(documentsRouter)
  await app.register(skillsRouter)
  await app.register(settingsRouter)
  await app.register(filesRouter)
  await app.register(adminRouter)
  await app.register(patientsRouter)
  await app.register(stubRouter)

  return app
}
