"""ContractSpec — YAML-based agent behavioral contract definition.

Supports two sources:
  - system.yaml: developer-defined, immutable (hard constraints)
  - user_rules.yaml: user-defined via conversation, appendable (soft only)

At runtime, both are merged: user rules can add soft invariants but cannot
override hard constraints.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Rule:
    """A single contract rule."""
    check: str                          # Check function name
    description: str = ""
    severity: str = "hard"              # "hard" or "soft"
    category: str = "invariant"         # "precondition", "invariant", "governance"
    params: dict = field(default_factory=dict)  # Check-specific params
    recovery: str = ""                  # Recovery action name (soft only)
    recovery_window: int = 3            # k steps for soft recovery
    source: str = "system"             # "system" or "user"
    created_at: float = 0.0


@dataclass
class ContractSpec:
    """Parsed contract specification."""
    name: str = "default"
    version: str = "1.0"

    preconditions: list[Rule] = field(default_factory=list)
    hard_invariants: list[Rule] = field(default_factory=list)
    soft_invariants: list[Rule] = field(default_factory=list)
    hard_governance: list[Rule] = field(default_factory=list)
    soft_governance: list[Rule] = field(default_factory=list)

    # Drift parameters
    compliance_weight: float = 0.7
    distributional_weight: float = 0.3
    warning_threshold: float = 0.15
    intervention_threshold: float = 0.35
    observation_window: int = 10

    @classmethod
    def from_yaml(cls, path: str | Path) -> ContractSpec:
        """Load contract from YAML file."""
        path = Path(path)
        if not path.exists():
            logger.warning("Contract file not found: %s — using empty contract", path)
            return cls()

        # Simple YAML parser (no PyYAML dependency)
        text = path.read_text(encoding="utf-8")
        data = _simple_yaml_parse(text)
        return cls._from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> ContractSpec:
        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: dict) -> ContractSpec:
        contract = data.get("contract", data)
        spec = cls(
            name=contract.get("name", "default"),
            version=contract.get("version", "1.0"),
        )

        # Preconditions
        for item in contract.get("preconditions", []):
            spec.preconditions.append(Rule(
                check=item.get("check", ""),
                description=item.get("description", ""),
                severity="hard",
                category="precondition",
                params=item.get("params", {}),
                source="system",
            ))

        # Invariants
        inv = contract.get("invariants", {})
        for item in inv.get("hard", []):
            spec.hard_invariants.append(Rule(
                check=item.get("check", ""),
                description=item.get("description", ""),
                severity="hard",
                category="invariant",
                params=item.get("params", {}),
                source="system",
            ))
        for item in inv.get("soft", []):
            spec.soft_invariants.append(Rule(
                check=item.get("check", ""),
                description=item.get("description", ""),
                severity="soft",
                category="invariant",
                params=item.get("params", {}),
                recovery=item.get("recovery", ""),
                recovery_window=item.get("recovery_window", 3),
                source="system",
            ))

        # Governance
        gov = contract.get("governance", {})
        for item in gov.get("hard", []):
            spec.hard_governance.append(Rule(
                check=item.get("check", ""),
                description=item.get("description", ""),
                severity="hard",
                category="governance",
                params=item,
                source="system",
            ))
        for item in gov.get("soft", []):
            spec.soft_governance.append(Rule(
                check=item.get("check", ""),
                description=item.get("description", ""),
                severity="soft",
                category="governance",
                params=item,
                recovery=item.get("recovery", ""),
                recovery_window=item.get("recovery_window", 5),
                source="system",
            ))

        # Drift params
        drift = contract.get("drift", {})
        if drift:
            spec.compliance_weight = float(drift.get("compliance_weight", 0.7))
            spec.distributional_weight = float(drift.get("distributional_weight", 0.3))
            spec.warning_threshold = float(drift.get("warning_threshold", 0.15))
            spec.intervention_threshold = float(drift.get("intervention_threshold", 0.35))
            spec.observation_window = int(drift.get("observation_window", 10))

        return spec

    def add_user_rule(self, rule: Rule) -> None:
        """Add a user-defined rule (soft only, cannot override hard constraints)."""
        rule.source = "user"
        rule.created_at = time.time()

        if rule.severity == "hard":
            logger.warning("User rules cannot define hard constraints — downgrading to soft")
            rule.severity = "soft"

        if rule.category == "invariant":
            self.soft_invariants.append(rule)
        elif rule.category == "governance":
            self.soft_governance.append(rule)
        else:
            self.soft_invariants.append(rule)

        logger.info("User rule added: %s (%s)", rule.check, rule.description)

    @property
    def all_hard(self) -> list[Rule]:
        return self.hard_invariants + self.hard_governance

    @property
    def all_soft(self) -> list[Rule]:
        return self.soft_invariants + self.soft_governance

    @property
    def all_rules(self) -> list[Rule]:
        return self.preconditions + self.all_hard + self.all_soft

    def save_user_rules(self, path: str | Path) -> None:
        """Persist user-defined rules to disk."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        user_rules = [r for r in self.all_rules if r.source == "user"]

        import json
        data = []
        for r in user_rules:
            data.append({
                "check": r.check,
                "description": r.description,
                "severity": r.severity,
                "category": r.category,
                "params": r.params,
                "recovery": r.recovery,
                "recovery_window": r.recovery_window,
                "created_at": r.created_at,
            })
        path.write_text(json.dumps(data, indent=2, default=str))

    def load_user_rules(self, path: str | Path) -> int:
        """Load user-defined rules from disk. Returns count loaded."""
        path = Path(path)
        if not path.exists():
            return 0
        import json
        try:
            data = json.loads(path.read_text())
            count = 0
            for item in data:
                rule = Rule(
                    check=item.get("check", ""),
                    description=item.get("description", ""),
                    severity="soft",  # force soft
                    category=item.get("category", "invariant"),
                    params=item.get("params", {}),
                    recovery=item.get("recovery", ""),
                    recovery_window=item.get("recovery_window", 3),
                    source="user",
                    created_at=item.get("created_at", 0),
                )
                if rule.category == "invariant":
                    self.soft_invariants.append(rule)
                else:
                    self.soft_governance.append(rule)
                count += 1
            logger.info("Loaded %d user rules from %s", count, path)
            return count
        except Exception as e:
            logger.warning("Failed to load user rules: %s", e)
            return 0


def _simple_yaml_parse(text: str) -> dict:
    """Minimal YAML-like parser for contract files. No external dependency."""
    import json
    # Try JSON first (contract files may be JSON)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.debug("JSON parse of contract failed: %s", e)

    # Simple key-value YAML parsing
    result = {}
    current = result
    stack = [(result, -1)]

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())

        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()

            # Navigate to correct nesting level
            while stack and stack[-1][1] >= indent:
                stack.pop()
            current = stack[-1][0] if stack else result

            if value:
                # Strip quotes
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value == "true":
                    value = True
                elif value == "false":
                    value = False
                else:
                    try:
                        value = float(value) if "." in value else int(value)
                    except ValueError as e:
                        logger.debug("parsing scalar as number failed: %s", e)
                current[key] = value
            else:
                current[key] = {}
                stack.append((current[key], indent))
                current = current[key]

    return result
