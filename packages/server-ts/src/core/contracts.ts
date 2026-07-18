// Behavioural Contract Engine — constrains agent behaviour via rules
// Each rule is checked before and after every chat turn
// Drift is tracked over time and surfaced to the evolution verdict runner

export interface Rule {
  name: string
  description: string
  check: (context: string) => RuleResult
}

export interface RuleResult {
  passed: boolean
  violations: string[]
  score: number  // 0-1, 1 = perfect compliance
}

export interface DriftScore {
  namespace: string           // 'facts' | 'skills' | 'persona' | 'knowledge' | 'episodes'
  drift: number               // accumulated drift since last anchor
  violations: string[]        // recent contract violations
  timestamp: number
}

export class ContractEngine {
  private rules: Rule[] = []

  addRule(rule: Rule) {
    this.rules.push(rule)
  }

  preCheck(context: string): RuleResult {
    let totalScore = 0
    const allViolations: string[] = []
    for (const rule of this.rules) {
      const result = rule.check(context)
      totalScore += result.score
      allViolations.push(...result.violations.map(v => `[${rule.name}] ${v}`))
    }
    return {
      passed: allViolations.length === 0,
      violations: allViolations,
      score: this.rules.length > 0 ? totalScore / this.rules.length : 1,
    }
  }

  postCheck(request: string, response: string): RuleResult {
    return this.preCheck(`REQUEST:\n${request}\n\nRESPONSE:\n${response}`)
  }
}
