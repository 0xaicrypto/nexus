import type { ChatStreamChunk } from '../types.js'

export async function* parseSSEStream(response: Response): AsyncGenerator<ChatStreamChunk> {
  if (!response.body) throw new Error('No response body')

  const reader = response.body.getReader()
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
        if (line.startsWith('data: ')) {
          try {
            yield JSON.parse(line.slice(6))
          } catch { /* skip malformed chunks */ }
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}
