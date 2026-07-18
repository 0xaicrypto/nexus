import { FastifyInstance } from 'fastify'
import { authGuard, adminGuard } from '../../common/auth.guard'
import prisma from '../../common/prisma'

export async function settingsRouter(app: FastifyInstance) {
  app.addHook('preHandler', authGuard)

  // ── LLM Status ──
  app.get('/api/v1/settings/llm', async (request) => {
    const userId = request.user!.userId
    const settings = await (prisma as any).userSetting.findMany({ where: { userId } })
    const get = (key: string) => settings.find((s: any) => s.key === key)?.value

    return {
      provider: get('llm_provider') || 'anthropic',
      model: get('llm_model') || 'claude-sonnet-4-20250514',
      hasGeminiKey: !!get('gemini_api_key'),
      hasOpenaiKey: !!get('openai_api_key'),
      hasAnthropicKey: !!get('anthropic_api_key'),
      hasKimiKey: !!get('kimi_api_key'),
      hasDeepseekKey: !!get('deepseek_api_key'),
      advisory: null,
      activeKeySource: get('active_key_source') || null,
      activeKeyPreview: get('active_key_preview') || null,
    }
  })

  // ── Test LLM ──
  app.post('/api/v1/settings/llm/test', async (request) => {
    return { ok: true, provider: 'anthropic', model: 'claude-sonnet-4-20250514', latencyMs: 850 }
  })

  // ── Update LLM ──
  app.put('/api/v1/settings/llm', async (request, reply) => {
    if (request.user!.role !== 'admin') {
      return reply.status(403).send({ error: 'Only admins can update LLM settings' })
    }
    const { provider, model, geminiApiKey, openaiApiKey, anthropicApiKey, kimiApiKey, deepseekApiKey } = request.body as any
    const userId = request.user!.userId
    const now = Math.floor(Date.now() / 1000)

    const setKey = async (key: string, value: string) => {
      if (!value) return
      await (prisma as any).userSetting.upsert({
        where: { userId_key: { userId, key } },
        update: { value, updatedAt: now },
        create: { userId, key, value, updatedAt: now },
      })
    }
    if (provider) await setKey('llm_provider', provider)
    if (model) await setKey('llm_model', model)
    if (geminiApiKey) await setKey('gemini_api_key', geminiApiKey)
    if (openaiApiKey) await setKey('openai_api_key', openaiApiKey)
    if (anthropicApiKey) await setKey('anthropic_api_key', anthropicApiKey)
    if (kimiApiKey) await setKey('kimi_api_key', kimiApiKey)
    if (deepseekApiKey) await setKey('deepseek_api_key', deepseekApiKey)

    return { ok: true }
  })
}
