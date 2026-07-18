// DeepSeek LLM client — OpenAI-compatible Chat Completions API
const DEEPSEEK_BASE = 'https://api.deepseek.com/v1'
const DEEPSEEK_MODEL = 'deepseek-chat'

interface ChatMessage {
  role: 'system' | 'user' | 'assistant'
  content: string
}

interface DeepSeekChunk {
  choices?: Array<{ delta?: { content?: string; role?: string }; finish_reason?: string | null }>
}

/**
 * Non-streaming call — used for simple completions
 */
export async function deepseekChat(messages: ChatMessage[], apiKey: string): Promise<string> {
  const res = await fetch(`${DEEPSEEK_BASE}/chat/completions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({ model: DEEPSEEK_MODEL, messages, max_tokens: 4096, temperature: 0.7 }),
  })
  if (!res.ok) {
    const err = await res.text().catch(() => '')
    throw new Error(`DeepSeek API ${res.status}: ${err.slice(0, 200)}`)
  }
  const json = await res.json()
  return json.choices?.[0]?.message?.content || ''
}

/**
 * Streaming call — yields chunks via AsyncGenerator
 */
export async function* deepseekStream(messages: ChatMessage[], apiKey: string): AsyncGenerator<string> {
  const res = await fetch(`${DEEPSEEK_BASE}/chat/completions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({ model: DEEPSEEK_MODEL, messages, max_tokens: 4096, temperature: 0.7, stream: true }),
  })
  if (!res.ok) {
    const err = await res.text().catch(() => '')
    throw new Error(`DeepSeek API ${res.status}: ${err.slice(0, 200)}`)
  }

  const reader = res.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const data = line.slice(6).trim()
        if (data === '[DONE]') return
        try {
          const chunk: DeepSeekChunk = JSON.parse(data)
          const content = chunk.choices?.[0]?.delta?.content
          if (content) yield content
        } catch { /* skip parse errors */ }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

export function getApiKey(): string {
  return process.env.DEEPSEEK_API_KEY || 'sk-edc3839a3dd44babaf33dc16d0761dc3'
}
