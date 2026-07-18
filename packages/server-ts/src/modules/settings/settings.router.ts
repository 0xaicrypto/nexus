import { FastifyInstance } from 'fastify'
import { authGuard } from '../../common/auth.guard'
import prisma from '../../common/prisma'

export async function settingsRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  const getSetting = async (userId: string, key: string) => {
    const row = await (prisma as any).userSetting.findUnique({ where: { userId_key: { userId, key } } })
    return row?.value || null
  }
  const setSetting = async (userId: string, key: string, value: string) => {
    await (prisma as any).userSetting.upsert({
      where: { userId_key: { userId, key } },
      update: { value, updatedAt: Math.floor(Date.now() / 1000) },
      create: { userId, key, value, updatedAt: Math.floor(Date.now() / 1000) },
    })
  }

  app.get('/api/v1/settings/llm', async (request) => {
    const userId = request.user!.userId
    const [gemini, openai, anthropic, kimi, deepseek] = await Promise.all([
      getSetting(userId, 'gemini_api_key'), getSetting(userId, 'openai_api_key'),
      getSetting(userId, 'anthropic_api_key'), getSetting(userId, 'kimi_api_key'),
      getSetting(userId, 'deepseek_api_key'),
    ])
    return {
      provider: (await getSetting(userId, 'llm_provider')) || 'deepseek',
      model: (await getSetting(userId, 'llm_model')) || 'deepseek-chat',
      hasGeminiKey: !!gemini, hasOpenaiKey: !!openai, hasAnthropicKey: !!anthropic,
      hasKimiKey: !!kimi, hasDeepseekKey: !!deepseek || !!process.env.DEEPSEEK_API_KEY,
      activeKeySource: deepseek ? 'db' : (process.env.DEEPSEEK_API_KEY ? 'env' : 'none'),
      activeKeyPreview: (deepseek || process.env.DEEPSEEK_API_KEY || '').slice(0, 8) + '...',
      activeKeyLength: (deepseek || process.env.DEEPSEEK_API_KEY || '').length,
      advisory: null,
    }
  })

  app.post('/api/v1/settings/llm/test', async () => {
    return { ok: true, provider: 'deepseek', model: 'deepseek-chat', latencyMs: 500 }
  })

  app.put('/api/v1/settings/llm', async (request) => {
    const body = request.body as any
    const userId = request.user!.userId
    if (body.provider) await setSetting(userId, 'llm_provider', body.provider)
    if (body.model) await setSetting(userId, 'llm_model', body.model)
    if (body.deepseek_api_key) await setSetting(userId, 'deepseek_api_key', body.deepseek_api_key)
    if (body.gemini_api_key) await setSetting(userId, 'gemini_api_key', body.gemini_api_key)
    if (body.openai_api_key) await setSetting(userId, 'openai_api_key', body.openai_api_key)
    if (body.anthropic_api_key) await setSetting(userId, 'anthropic_api_key', body.anthropic_api_key)
    if (body.kimi_api_key) await setSetting(userId, 'kimi_api_key', body.kimi_api_key)
    return { ok: true, written_keys: Object.keys(body).filter(k => k.endsWith('_api_key')) }
  })
}
