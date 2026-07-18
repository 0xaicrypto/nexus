/**
 * Memory Projection — 上下文压缩与加权注意力
 *
 * 核心问题: 医生与 Agent 聊了 30 天、500 轮对话，LLM 上下文窗口有限。
 * 如何选择最相关、最重要的上下文注入当前轮次的 system prompt？
 *
 * 策略: 三层衰减 + 重要性加权
 *
 * Layer 1 — 完整保留 (高注意力)
 *   最近 N 轮对话全文，不压缩
 *   N = 3 (可配置)
 *
 * Layer 2 — 摘要压缩 (中注意力)
 *   最近 7 天的会话摘要（Episode），每个 ~100 tokens
 *
 * Layer 3 — 事实提取 (低注意力但持久)
 *   所有 Facts，按 importance×recency 排序
 *   高重要性(5) 的旧事实 > 低重要性(1) 的新事实
 *
 * 注意力公式:
 *   attention_score(entry) = recency_weight × importance_multiplier
 *
 *   recency_weight = e^(-λ × days_ago)
 *      λ = 0.3 → 7天前约 12%, 30天前约 0.01%
 *
 *   importance_multiplier (仅 Facts):
 *      5 → 2.0x    (极高, 总是保留)
 *      4 → 1.5x
 *      3 → 1.0x    (基准)
 *      2 → 0.5x
 *      1 → 0.25x   (低重要性快速衰减)
 *
 * 预算分配 (假设 8000 token 上下文窗口):
 *   ┌────────────┬──────────┬─────────────────────┐
 *   │ 类别        │ Token  % │ 内容                 │
 *   ├────────────┼──────────┼─────────────────────┤
 *   │ System     │ 500   6% │ 人格 + 指令           │
 *   │ Patient    │ 1000  12% │ 当前患者临床图谱       │
 *   │ Layer 1    │ 2500  31% │ 最近 3 轮完整对话      │
 *   │ Layer 2    │ 1500  19% │ 最近 7 天 Episodes    │
 *   │ Layer 3    │ 1500  19% │ 高权重 Facts          │
 *   │ Skills     │ 500    6% │ 活跃技能列表           │
 *   │ Reserve    │ 500    6% │ 预留弹性               │
 *   └────────────┴──────────┴─────────────────────┘
 */

import { Fact, Episode, LearnedSkill } from '../evolution/stores'
import { EventLog, Event } from '../core/event-log'
import prisma from '../common/prisma'

// ── 配置 ────────────────────────────────────────────────

export interface ProjectionConfig {
  maxTokens: number           // 上下文窗口大小 (token 估计)
  layer1Turns: number         // 完整保留的最近轮次
  layer2EpisodeDays: number   // Episode 保留天数
  recencyLambda: number       // 衰减系数
  patientContextTokens: number
  reserveTokens: number
}

const DEFAULT_CONFIG: ProjectionConfig = {
  maxTokens: 8000,
  layer1Turns: 3,
  layer2EpisodeDays: 7,
  recencyLambda: 0.3,
  patientContextTokens: 1000,
  reserveTokens: 500,
}

// ── 注意力评分 ──────────────────────────────────────────

interface ScoredFact { fact: Fact; score: number }
interface ScoredEpisode { episode: Episode; score: number }

function daysAgo(timestamp: number): number {
  return (Date.now() - timestamp) / (1000 * 60 * 60 * 24)
}

function recencyWeight(days: number, lambda: number): number {
  return Math.exp(-lambda * days)
}

function estimateTokens(text: string): number {
  // 粗略估计: 英文 ~4 chars/token, 中文 ~1.5 chars/token
  const latinChars = (text.match(/[a-zA-Z0-9\s]/g) || []).length
  const nonLatinChars = text.length - latinChars
  return Math.ceil(latinChars / 4 + nonLatinChars / 1.5)
}

// ── 上下文投影器 ───────────────────────────────────────

export class MemoryProjection {
  constructor(
    private eventLog: EventLog,
    private config: ProjectionConfig = DEFAULT_CONFIG,
  ) {}

  /**
   * 为当前对话轮次投影上下文
   *
   * @returns 组装好的 system prompt 各部分，调用方决定如何拼接
   */
  async project(params: {
    userId: string
    patientHash: string | null
    sessionId: string
    persona: string
    facts: Fact[]
    episodes: Episode[]
    skills: LearnedSkill[]
  }): Promise<{
    systemPrompt: string
    budget: { layer: string; tokens: number; items: number }[]
  }> {
    const budget: { layer: string; tokens: number; items: number }[] = []
    const { maxTokens, layer1Turns, layer2EpisodeDays, patientContextTokens, reserveTokens } = this.config

    // ── Layer 0: System Persona (固定) ──
    const personaTokens = estimateTokens(params.persona)
    let remaining = maxTokens - personaTokens - reserveTokens

    // ── Layer 0b: Patient Context (高优先级) ──
    let patientContext = ''
    if (params.patientHash) {
      patientContext = await this.buildPatientContext(params.userId, params.patientHash)
    }
    const patientTokens = Math.min(estimateTokens(patientContext), patientContextTokens)
    remaining -= patientTokens

    // ── Layer 1: 最近 N 轮完整对话 (最高注意力) ──
    const recentEvents = this.eventLog.query({
      sessionId: params.sessionId,
      limit: layer1Turns * 2, // user + assistant = 2 events per turn
    }).reverse()
    let layer1Text = ''
    for (const evt of recentEvents) {
      const line = evt.eventType === 'user_message'
        ? `User: ${evt.content}`
        : `Assistant: ${evt.content}`
      layer1Text += line + '\n'
    }
    const layer1Tokens = estimateTokens(layer1Text)
    remaining -= layer1Tokens

    // ── Layer 2: 最近 N 天 Episodes (中注意力) ──
    const scoredEpisodes = params.episodes
      .map(ep => ({ episode: ep, score: recencyWeight(daysAgo(ep.createdAt), this.config.recencyLambda) }))
      .filter(s => s.score > 0.05) // 注意力低于 5% 的丢弃
      .sort((a, b) => b.score - a.score)

    let layer2Text = ''
    let layer2Count = 0
    const episodeBudget = Math.min(remaining * 0.4, 1500)
    for (const se of scoredEpisodes) {
      const line = `[Day ${Math.round(daysAgo(se.episode.createdAt))}d ago] ${se.episode.summary}`
      const t = estimateTokens(line)
      if (estimateTokens(layer2Text) + t > episodeBudget) break
      layer2Text += line + '\n'
      layer2Count++
    }
    remaining -= estimateTokens(layer2Text)

    // ── Layer 3: 加权 Facts (importance × recency) ──
    const scoredFacts = params.facts
      .map(f => {
        const impMultiplier = [0.25, 0.5, 1.0, 1.5, 2.0][f.importance - 1] || 1.0
        const score = recencyWeight(daysAgo(f.createdAt), this.config.recencyLambda) * impMultiplier
        return { fact: f, score }
      })
      .filter(s => s.score > 0.02)
      .sort((a, b) => b.score - a.score)

    let layer3Text = ''
    let layer3Count = 0
    const factsBudget = Math.min(remaining, 1500)
    for (const sf of scoredFacts) {
      const line = this.formatFact(sf.fact, sf.score)
      const t = estimateTokens(line)
      if (estimateTokens(layer3Text) + t > factsBudget) break
      layer3Text += line + '\n'
      layer3Count++
    }
    remaining -= estimateTokens(layer3Text)

    // ── Layer 4: Skills (固定, 低开销) ──
    let skillsText = ''
    if (params.skills.length > 0) {
      skillsText = params.skills
        .filter(s => s.successCount > 0)
        .slice(0, 5) // 最多 5 个技能
        .map(s => `- ${s.name}: ${s.bestStrategy} (${s.successCount}/${s.taskCount})`)
        .join('\n')
    }

    // ── 组装 ──
    const sections = [
      params.persona,
      patientContext ? `\n## Patient Context\n${patientContext}` : '',
      layer1Text ? `\n## Recent Conversation\n${layer1Text}` : '',
      layer2Text ? `\n## Recent Sessions\n${layer2Text}` : '',
      layer3Text ? `\n## Accumulated Knowledge\n${layer3Text}` : '',
      skillsText ? `\n## Active Skills\n${skillsText}` : '',
    ].filter(Boolean)

    return {
      systemPrompt: sections.join('\n'),
      budget: [
        { layer: 'persona', tokens: personaTokens, items: 1 },
        { layer: 'patient', tokens: patientTokens, items: params.patientHash ? 1 : 0 },
        { layer: 'layer1_recent', tokens: layer1Tokens, items: recentEvents.length },
        { layer: 'layer2_episodes', tokens: estimateTokens(layer2Text), items: layer2Count },
        { layer: 'layer3_facts', tokens: estimateTokens(layer3Text), items: layer3Count },
        { layer: 'layer4_skills', tokens: estimateTokens(skillsText), items: params.skills.length },
        { layer: 'reserve', tokens: remaining, items: 0 },
      ],
    }
  }

  // ── 患者上下文 (从临床图谱获取) ──

  private async buildPatientContext(userId: string, patientHash: string): Promise<string> {
    try {
      const nodes = await (prisma as any).$queryRawUnsafe(
        `SELECT node_type, content_json, weight, updated_at
         FROM clinical_graph_nodes
         WHERE user_id = ? AND patient_hash = ?
         ORDER BY weight DESC
         LIMIT 25`,
        userId, patientHash
      ) as Array<{ node_type: string; content_json: string; weight: number; updated_at: number }>

      if (!nodes.length) return ''

      const lines = nodes.map(n => {
        try {
          const c = JSON.parse(n.content_json)
          const text = c.text || c.content || c.summary || ''
          const tag = n.node_type.replace(/_/g, ' ')
          const recency = Math.round(daysAgo(n.updated_at))
          return `[${tag}] ${text} (${recency}d ago, weight:${n.weight})`
        } catch { return '' }
      }).filter(Boolean)

      // 按类型分组
      const byType: Record<string, string[]> = {}
      for (const line of lines) {
        const type = line.split(']')[0].slice(1)
        if (!byType[type]) byType[type] = []
        byType[type].push(line)
      }

      return Object.entries(byType)
        .map(([type, items]) => `### ${type}\n${items.join('\n')}`)
        .join('\n\n')
    } catch {
      return ''
    }
  }

  // ── 事实格式化 (注意力越高 → 越详细) ──

  private formatFact(fact: Fact, score: number): string {
    const stars = '★'.repeat(fact.importance)
    const days = Math.round(daysAgo(fact.createdAt))
    // 高注意力 → 完整内容, 低注意力 → 截断
    const content = score > 0.5 ? fact.content : fact.content.slice(0, 80) + '...'
    return `[${fact.category} ${stars}] ${content} (${days}d ago)`
  }
}
