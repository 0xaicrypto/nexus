import { EventLog, Event } from '../../core/event-log'
import { FactsStore, EpisodesStore, SkillsStore } from '../../evolution/stores'
import { ContractEngine } from '../../core/contracts'
import { MemoryProjection } from '../../retrieval/memory-projection'

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

  /**
   * 一次完整对话轮次 — 走完整进化回路
   *
   *   1. INGEST   → eventLog.append(user_message)
   *   6. RETRIEVE → projection.project(...)    ← 加权注意力上下文
   *   3. CONTRACT → contracts.preCheck()
   *   4. LLM      → llmCall(systemPrompt, message)
   *   5. CONTRACT → contracts.postCheck()
   *   1. INGEST   → eventLog.append(assistant_response)
   *
   *   异步后处理:
   *   2. EXTRACT  → extractTakeaway()
   *   5. EVOLVE   → maybeEvolve()
   */
  async turn(params: {
    userId: string
    message: string
    sessionId: string
    patientHash: string | null
    persona: string
    llmCall: (systemPrompt: string, userMessage: string) => Promise<string>
  }): Promise<{ userEvent: Event; response: string; budget: any[] }> {
    const { userId, message, sessionId, patientHash, persona, llmCall } = params

    // ── 1. 记录用户消息 ──
    const userEvent = this.eventLog.append({
      timestamp: Date.now() / 1000,
      eventType: 'user_message',
      content: message,
      metadata: { patientHash },
      agentId: userId,
      sessionId,
    })

    // ── 6. 加权注意力上下文投影 ──
    const projected = await this.projection.project({
      userId,
      patientHash,
      sessionId,
      persona,
      facts: this.factsStore.all(),
      episodes: this.episodesStore.all(),
      skills: this.skillsStore.all(),
    })

    // ── 3. 契约前置检查 ──
    const preCheck = this.contracts.preCheck(message)
    if (preCheck.violations.length > 0) {
      console.warn('pre-check violations:', preCheck.violations)
    }

    // ── 4. LLM 回复 ──
    const response = await llmCall(projected.systemPrompt, message)

    // ── 5. 契约后置检查 ──
    const postCheck = this.contracts.postCheck(message, response)

    // ── 1. 记录助手回复 ──
    this.eventLog.append({
      timestamp: Date.now() / 1000,
      eventType: 'assistant_response',
      content: response,
      metadata: { contractPassed: postCheck.passed },
      agentId: userId,
      sessionId,
    })

    return { userEvent, response, budget: projected.budget }
  }

  // ── 异步后处理 (不阻塞回复) ──

  async postTurn(userId: string, sessionId: string, message: string) {
    // 2. EXTRACT: 提取会话摘要
    const turnCount = this.eventLog.query({ sessionId }).length
    this.episodesStore.upsert(sessionId,
      `Conversation about: ${message.slice(0, 150)}`,
      turnCount,
    )

    // 5. EVOLVE: 每 20 轮触发进化
    const totalTurns = this.eventLog.count()
    if (totalTurns % 20 === 0 && totalTurns > 0) {
      const prev = this.factsStore.currentVersion()
      this.factsStore.commit()
      console.log(`[EVOLVE] facts ${prev} → ${this.factsStore.currentVersion()} (turn ${totalTurns})`)
    }
  }
}
