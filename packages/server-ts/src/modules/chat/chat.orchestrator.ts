import { EventLog, Event } from '../../core/event-log'
import { FactsStore, EpisodesStore, SkillsStore } from '../../evolution/stores'
import { ContractEngine } from '../../core/contracts'
import { MemoryProjection } from '../../retrieval/memory-projection'
import { deepseekChat, getApiKey } from '../../common/llm.js'

export class ChatOrchestrator {
  private projection: MemoryProjection

  constructor(
    private eventLog: EventLog,
    private factsStore: FactsStore,
    private episodesStore: EpisodesStore,
    private skillsStore: SkillsStore,
    private contracts: ContractEngine,
  ) {
    this.projection = new MemoryProjection(eventLog)
  }

  async turn(params: {
    userId: string; message: string; sessionId: string
    patientHash: string | null; persona: string
    llmCall: (systemPrompt: string, userMessage: string) => Promise<string>
  }): Promise<{ userEvent: Event; response: string; budget: any[] }> {
    const { userId, message, sessionId, patientHash, persona, llmCall } = params

    const userEvent = this.eventLog.append({
      timestamp: Date.now() / 1000, eventType: 'user_message', content: message,
      metadata: { patientHash }, agentId: userId, sessionId,
    })

    const projected = await this.projection.project({
      userId, patientHash, sessionId,
      persona, facts: this.factsStore.all(), episodes: this.episodesStore.all(), skills: this.skillsStore.all(),
    })

    const preCheck = this.contracts.preCheck(message)
    if (preCheck.violations.length > 0) console.warn('pre-check violations:', preCheck.violations)

    const response = await llmCall(projected.systemPrompt, message)

    const postCheck = this.contracts.postCheck(message, response)
    this.eventLog.append({
      timestamp: Date.now() / 1000, eventType: 'assistant_response', content: response,
      metadata: { contractPassed: postCheck.passed }, agentId: userId, sessionId,
    })

    return { userEvent, response, budget: projected.budget }
  }

  // #2: Extract facts automatically using DeepSeek
  async postTurn(userId: string, sessionId: string, userMessage: string) {
    const recentEvents = this.eventLog.query({ sessionId, limit: 6 }).reverse()
    const conversation = recentEvents
      .map(e => `${e.eventType === 'user_message' ? 'USER' : 'AI'}: ${e.content.slice(0, 300)}`)
      .join('\n')

    // Extract takeaway as episode summary
    const turnCount = this.eventLog.query({ sessionId }).length
    this.episodesStore.upsert(sessionId, userMessage.slice(0, 150), turnCount)

    // Every 5 turns, extract facts from conversation with DeepSeek
    const totalTurns = this.eventLog.count()
    if (totalTurns % 5 === 0 && totalTurns > 0) {
      try {
        const apiKey = getApiKey()
        const extractionPrompt = `Extract key facts, preferences, and knowledge from this conversation. Return ONLY a JSON array of objects with: category (preference/fact/constraint/goal/context), importance (1-5), content (short sentence).\n\n${conversation}\n\n[JSON array]:`

        const result = await deepseekChat([{ role: 'user', content: extractionPrompt }], apiKey)
        const jsonMatch = result.match(/\[[\s\S]*\]/)
        if (jsonMatch) {
          const facts = JSON.parse(jsonMatch[0])
          for (const f of facts) {
            if (f.category && f.content) {
              this.factsStore.add({
                category: f.category,
                importance: Math.min(5, Math.max(1, f.importance || 3)),
                content: f.content,
              })
            }
          }
          this.factsStore.commit()
          this.eventLog.append({
            timestamp: Date.now() / 1000,
            eventType: 'evolution',
            content: `🧠 Extracted ${facts.length} new facts`,
            metadata: { factCount: facts.length, categories: [...new Set(facts.map((f: any) => f.category))] },
            agentId: userId, sessionId,
          })
          console.log(`[EVOLVE] Extracted ${facts.length} facts (turn ${totalTurns})`)
        }
      } catch (err) {
        console.log('[EVOLVE] Fact extraction skipped:', (err as Error).message.slice(0, 100))
      }
    }
  }
}
