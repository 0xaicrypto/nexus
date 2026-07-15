"""Agent Behavioral Contracts (ABC) — runtime enforcement for AI agents.

Based on: "Agent Behavioral Contracts: Formal Specification and Runtime
Enforcement for Reliable Autonomous AI Agents" (arXiv:2602.22302)

Contract C = (P, I_hard, I_soft, G_hard, G_soft, R):
  P: Preconditions (must hold before execution)
  I_hard: Hard invariants (must hold every step, violation = breach)
  I_soft: Soft invariants (may be transiently violated, recovery within k steps)
  G_hard: Hard governance (every action must comply)
  G_soft: Soft governance (transient violations allowed)
  R: Recovery mechanisms (soft violation → corrective action)

Usage:
    from nexus_core.contracts import ContractEngine, ContractSpec

    spec = ContractSpec.from_yaml("agent_contract.yaml")
    engine = ContractEngine(spec)

    # Before LLM call
    result = engine.pre_check(user_message, context)
    if result.blocked:
        return result.reason

    # After LLM response
    result = engine.post_check(response, context)
    if result.hard_violation:
        response = engine.recover(result)
"""

from .drift import DriftScore
from .engine import CheckResult, ContractEngine
from .spec import ContractSpec, Rule

__all__ = ["ContractSpec", "Rule", "ContractEngine", "CheckResult", "DriftScore"]
