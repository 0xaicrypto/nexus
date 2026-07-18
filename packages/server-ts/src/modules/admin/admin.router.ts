import { FastifyInstance } from 'fastify'
import bcrypt from 'bcryptjs'
import prisma from '../../common/prisma'
import { adminGuard } from '../../common/auth.guard'

export async function adminRouter(app: FastifyInstance) {
  app.addHook('preHandler', adminGuard)

  // ── List users (frontend expects { users: [...] }) ──
  app.get('/api/v1/admin/users', async () => {
    const users = await (prisma as any).user.findMany({
      orderBy: { createdAt: 'desc' },
      select: { id: true, displayName: true, role: true, createdAt: true, disabledAt: true, lastLoginAt: true },
    })
    return {
      users: users.map((u: any) => ({
        user_id: u.id,
        username: u.displayName,
        role: u.role,
        created_at: u.createdAt,
        disabled_at: u.disabledAt,
        last_login_at: u.lastLoginAt,
        has_password: true,
      })),
    }
  })

  // ── Disable user ──
  app.post('/api/v1/admin/users/:userId/disable', async (request, reply) => {
    const { userId } = request.params as any
    const now = new Date().toISOString()
    try {
      await (prisma as any).user.update({ where: { id: userId }, data: { disabledAt: now } })
      return { user_id: userId, disabled_at: now, ok: true }
    } catch {
      return reply.status(404).send({ error: 'User not found' })
    }
  })

  // ── Enable user ──
  app.post('/api/v1/admin/users/:userId/enable', async (request, reply) => {
    const { userId } = request.params as any
    try {
      await (prisma as any).user.update({ where: { id: userId }, data: { disabledAt: null } })
      return { user_id: userId, disabled_at: null, ok: true }
    } catch {
      return reply.status(404).send({ error: 'User not found' })
    }
  })

  // ── Reset password ──
  app.post('/api/v1/admin/users/:userId/reset-password', async (request, reply) => {
    const { userId } = request.params as any
    const { new_password } = request.body as any
    if (!new_password) return reply.status(400).send({ error: 'new_password required' })
    const hash = await bcrypt.hash(new_password, 10)
    try {
      await (prisma as any).user.update({ where: { id: userId }, data: { passwordHash: hash } })
      return { user_id: userId, ok: true }
    } catch {
      return reply.status(404).send({ error: 'User not found' })
    }
  })
}
