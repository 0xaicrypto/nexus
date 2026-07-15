"""ContractEngine — runtime enforcement of Agent Behavioral Contracts.

Checks preconditions before execution, invariants/governance after each action,
tracks soft violations for recovery, and logs everything to EventLog.

Overhead target: <10ms per check (pure Python, no LLM calls).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .spec import ContractSpec, Rule

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    """Result of a contract check."""
    passed: bool = True
    hard_violation: bool = False
    soft_violations: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    blocked: bool = False               # True if action should be blocked
    reason: str = ""                    # Human-readable reason for blocking
    recovery_actions: list[str] = field(default_factory=list)


# ── Built-in check functions ──

def check_no_pii_leak(text: str, **kwargs) -> bool:
    """Check if text contains common PII patterns."""
    patterns = [
        r'\b\d{3}-\d{2}-\d{4}\b',          # SSN
        r'\b\d{16}\b',                       # Credit card (16 digits)
        r'\b[A-Z]\d{8}\b',                  # Passport
        r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b',  # CC with spaces
    ]
    for pattern in patterns:
        if re.search(pattern, text):
            return False
    return True


def check_language_match(text: str, target: str = "", **kwargs) -> bool:
    """Simple check if text is primarily in the target language."""
    if not target:
        return True
    # Simple heuristic: count CJK characters for Chinese detection
    if target in ("zh", "zh-CN", "chinese"):
        cjk_count = sum(1 for c in text if '一' <= c <= '鿿')
        return cjk_count > len(text) * 0.1
    elif target in ("en", "english"):
        ascii_count = sum(1 for c in text if c.isascii() and c.isalpha())
        return ascii_count > len(text) * 0.3
    return True


def check_response_length(text: str, max_tokens: int = 4000, **kwargs) -> bool:
    """Check if response is within length limit."""
    # Rough token estimate: 1 token ≈ 4 chars for English, 2 chars for Chinese
    char_count = len(text)
    return char_count < max_tokens * 3


def check_tool_whitelist(tool_name: str, allowed: list = None, **kwargs) -> bool:
    """Check if a tool call is in the whitelist."""
    if not allowed:
        return True
    return tool_name in allowed


def check_max_transaction(amount_usd: float = 0, limit_usd: float = 1000, **kwargs) -> bool:
    """Check if a transaction amount is within limits."""
    return amount_usd <= limit_usd


def check_no_forbidden_patterns(text: str, patterns: list = None, **kwargs) -> bool:
    """Check if text contains forbidden patterns."""
    if not patterns:
        return True
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return False
    return True


def check_session_turns(turn_count: int = 0, max_turns: int = 100, **kwargs) -> bool:
    """Check if session hasn't exceeded max turns."""
    return turn_count <= max_turns


def check_professional_tone(text: str, **kwargs) -> bool:
    """Simple check for unprofessional language."""
    # Basic: check for excessive emoji, all-caps shouting, profanity markers
    emoji_count = sum(1 for c in text if ord(c) > 0x1F600)
    if emoji_count > 5:
        return False
    upper_ratio = sum(1 for c in text if c.isupper()) / max(len(text), 1)
    if upper_ratio > 0.5 and len(text) > 20:
        return False
    return True


# Registry of built-in checks
BUILTIN_CHECKS = {
    "no_pii_leak": check_no_pii_leak,
    "language_match": check_language_match,
    "response_length": check_response_length,
    "tool_whitelist": check_tool_whitelist,
    "max_transaction": check_max_transaction,
    "no_forbidden_patterns": check_no_forbidden_patterns,
    "session_turns": check_session_turns,
    "professional_tone": check_professional_tone,
    # User-defined checks are added dynamically
}


class ContractEngine:
    """Runtime contract enforcement engine.

    Evaluates rules against agent state at each step.
    Tracks soft violations and triggers recovery when needed.
    All results are logged to EventLog for audit.
    """

    def __init__(self, spec: ContractSpec, event_log=None):
        self.spec = spec
        self._event_log = event_log
        self._soft_violation_tracker: dict[str, list[int]] = {}  # rule_check -> [step_numbers]
        self._step_count = 0
        self._custom_checks: dict[str, callable] = {}
        self._hard_compliance_history: list[float] = []
        self._soft_compliance_history: list[float] = []

    def register_check(self, name: str, fn: callable) -> None:
        """Register a custom check function."""
        self._custom_checks[name] = fn

    def _get_check_fn(self, name: str):
        """Look up a check function by name."""
        return self._custom_checks.get(name) or BUILTIN_CHECKS.get(name)

    def _eval_rule(self, rule: Rule, **context) -> bool:
        """Evaluate a single rule against the current context."""
        fn = self._get_check_fn(rule.check)
        if fn is None:
            logger.debug("No check function for '%s' — skipping", rule.check)
            return True  # Unknown checks pass by default
        try:
            params = {**rule.params, **context}
            return fn(**params)
        except Exception as e:
            logger.warning("Check '%s' raised error: %s", rule.check, e)
            return True  # Errors don't block (fail-open for availability)

    def check_preconditions(self, **context) -> CheckResult:
        """Check all preconditions before agent starts."""
        result = CheckResult()
        for rule in self.spec.preconditions:
            if not self._eval_rule(rule, **context):
                result.passed = False
                result.hard_violation = True
                result.blocked = True
                result.reason = f"Precondition failed: {rule.description or rule.check}"
                break
        self._log_check("precondition", result)
        return result

    def pre_check(self, user_message: str, **context) -> CheckResult:
        """Check hard governance before processing a user message."""
        self._step_count += 1
        result = CheckResult()
        context["text"] = user_message
        context["turn_count"] = self._step_count

        for rule in self.spec.hard_governance:
            if not self._eval_rule(rule, **context):
                result.passed = False
                result.hard_violation = True
                result.blocked = True
                result.reason = f"Governance violation: {rule.description or rule.check}"
                result.details[rule.check] = "hard_governance_violation"
                break

        # Also check soft governance (don't block, just track)
        for rule in self.spec.soft_governance:
            if not self._eval_rule(rule, **context):
                result.soft_violations.append(rule.check)
                self._track_soft_violation(rule)

        self._log_check("pre_check", result)
        return result

    def post_check(self, response: str, **context) -> CheckResult:
        """Check invariants and governance after LLM generates a response."""
        result = CheckResult()
        context["text"] = response

        # Hard invariants — violation = breach
        hard_passed = 0
        for rule in self.spec.hard_invariants:
            if self._eval_rule(rule, **context):
                hard_passed += 1
            else:
                result.hard_violation = True
                result.passed = False
                result.reason = f"Hard invariant violated: {rule.description or rule.check}"
                result.details[rule.check] = "hard_invariant_violation"
                result.recovery_actions.append("regenerate")

        # Soft invariants — track and recover
        soft_passed = 0
        for rule in self.spec.soft_invariants:
            if self._eval_rule(rule, **context):
                soft_passed += 1
            else:
                result.soft_violations.append(rule.check)
                self._track_soft_violation(rule)
                if rule.recovery:
                    result.recovery_actions.append(rule.recovery)

        # Calculate compliance scores
        total_hard = len(self.spec.hard_invariants) + len(self.spec.hard_governance)
        total_soft = len(self.spec.soft_invariants) + len(self.spec.soft_governance)
        hard_score = hard_passed / max(total_hard, 1)
        soft_score = soft_passed / max(total_soft, 1)

        self._hard_compliance_history.append(hard_score)
        self._soft_compliance_history.append(soft_score)

        result.details["hard_compliance"] = hard_score
        result.details["soft_compliance"] = soft_score
        result.details["step"] = self._step_count

        self._log_check("post_check", result)
        return result

    def _track_soft_violation(self, rule: Rule) -> None:
        """Track a soft violation for recovery window checking."""
        if rule.check not in self._soft_violation_tracker:
            self._soft_violation_tracker[rule.check] = []
        self._soft_violation_tracker[rule.check].append(self._step_count)

        # Check if recovery window exceeded
        violations = self._soft_violation_tracker[rule.check]
        if len(violations) >= rule.recovery_window:
            recent = [v for v in violations if v >= self._step_count - rule.recovery_window]
            if len(recent) >= rule.recovery_window:
                logger.warning("Soft constraint '%s' violated %d consecutive times — recovery needed",
                               rule.check, rule.recovery_window)

    def needs_recovery(self, check_name: str) -> bool:
        """Check if a soft constraint has exceeded its recovery window."""
        rule = None
        for r in self.spec.all_soft:
            if r.check == check_name:
                rule = r
                break
        if not rule:
            return False

        violations = self._soft_violation_tracker.get(check_name, [])
        recent = [v for v in violations if v >= self._step_count - rule.recovery_window]
        return len(recent) >= rule.recovery_window

    def clear_violation(self, check_name: str) -> None:
        """Clear violation history for a soft constraint (after recovery)."""
        self._soft_violation_tracker.pop(check_name, None)

    def _log_check(self, check_type: str, result: CheckResult) -> None:
        """Log check result to EventLog."""
        if self._event_log:
            self._event_log.append(
                "contract_check",
                f"{check_type}: passed={result.passed}, hard_violation={result.hard_violation}, "
                f"soft_violations={result.soft_violations}",
                metadata={
                    "check_type": check_type,
                    "passed": result.passed,
                    "hard_violation": result.hard_violation,
                    "soft_violations": result.soft_violations,
                    "details": result.details,
                    "step": self._step_count,
                },
            )

    @property
    def compliance_history(self) -> dict:
        return {
            "hard": list(self._hard_compliance_history),
            "soft": list(self._soft_compliance_history),
            "steps": self._step_count,
        }
