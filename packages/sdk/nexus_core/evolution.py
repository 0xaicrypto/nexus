"""Falsifiable evolution — proposal / verdict / revert primitives.

Implements the BEP-Nexus §3.4 event schema and the normative
verdict-decision rules from
``docs/design/nexus-architecture.md``.

The high-level flow:

1. An evolver (Memory / Skill / Persona / Knowledge) wants to
   write to curated memory. **Before** the write, it builds an
   :class:`EvolutionProposal` declaring what it's about to change
   *and* what it predicts will happen — which user-task kinds it
   thinks will be fixed, which it thinks may regress, and a
   rollback pointer so the change can be undone.
2. The proposal is emitted as an ``evolution_proposal`` event
   into the EventLog (and thus eventually into the on-chain
   anchor manifest). The edit is then applied to storage.
3. After an observation window of ``expires_after_events`` events,
   the runner :func:`score_verdict` against the observed events,
   producing an :class:`EvolutionVerdict` with one of three
   decisions: ``kept`` / ``kept_with_warning`` / ``reverted``.
4. ``reverted`` decisions emit an :class:`EvolutionRevert` event
   and the storage layer flips its ``_current.json`` pointer back.

The key normative rule, baked into :func:`score_verdict`, is from
the AHE paper's empirical finding:

    Regression *prediction* is indistinguishable from random.
    Therefore: **only revert on observed regressions, never on
    predicted-but-unobserved ones.** Predicted regressions are
    advisory hints to the verdict scorer (look harder for these
    task_kinds), not revert triggers.

This module is pure data + pure logic. It does no I/O. The
EventLog wiring + storage pointer flips live in the framework
(``nexus`` package, Phase O.2 work).

Inspired by Lin, Liu, Pan et al., *Agentic Harness Engineering*
(arXiv:2604.25850v3, Apr 2026).
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Literal, Optional

# ── Type aliases ──────────────────────────────────────────────────────


#: Severity of a regression — drives the verdict decision (BEP §3.4).
Severity = Literal["low", "medium", "high"]

#: Verdict decision (BEP §3.4 normative).
VerdictDecision = Literal["kept", "kept_with_warning", "reverted"]

#: Revert trigger reason (one of these, recorded for audit).
RevertTrigger = Literal[
    "unpredicted_regression",
    "abc_drift",
    "user_revert",
    "hard_rule_violation",
]

#: ABC drift threshold pair used by :func:`score_verdict`.
@dataclasses.dataclass
class DriftThresholds:
    """ABC ``DriftScore`` thresholds. The runner reads these from the
    twin's :class:`ContractEngine`; passed in here so verdict scoring
    stays a pure function."""
    warning: float = 0.10        # drift delta that flips kept → kept_with_warning
    intervention: float = 0.30   # drift delta that flips kept_with_warning → reverted


# ── Data classes ──────────────────────────────────────────────────────


@dataclasses.dataclass
class TaskKindPrediction:
    """One predicted fix or predicted regression entry.

    The ``severity`` field is only meaningful for predicted
    regressions; predicted fixes carry only ``task_kind`` + reason.
    Verdict scoring ignores ``severity`` on predicted entries
    (regression prediction is unreliable per the paper) and uses
    severity only on *observed* regressions.
    """
    task_kind: str
    reason: str = ""
    severity: Severity = "low"   # only relevant for regressions


@dataclasses.dataclass
class EvolutionProposal:
    """An evolver's pre-write declaration of intent + predictions."""
    edit_id: str
    evolver: str                     # "MemoryEvolver" / "SkillEvolver" / etc.
    target_namespace: str            # "memory.facts" / "memory.persona" / "middleware.retry" / ...
    target_version_pre: str          # object-store key, e.g. "memory/facts/v0041.json"
    target_version_post: str

    evidence_event_ids: list[int] = dataclasses.field(default_factory=list)
    evidence_summary: str = ""
    inferred_root_cause: str = ""

    change_summary: str = ""
    change_diff: list[dict] = dataclasses.field(default_factory=list)

    predicted_fixes: list[TaskKindPrediction] = dataclasses.field(default_factory=list)
    predicted_regressions: list[TaskKindPrediction] = dataclasses.field(default_factory=list)

    rollback_pointer: str = ""
    expires_after_events: int = 100   # default verdict deadline

    # ── Causal lineage (Phase A+ / Phase C input) ─────────────────
    # Free-form dict the emitting evolver fills with whatever
    # accumulator state triggered this proposal. The Pressure
    # Dashboard's "Lineage" view reads this back to render
    # "caused by N facts in window [a, b]" / "caused by topic
    # threshold reached after 3 conversations" / etc.
    #
    # Recommended shape (not enforced — evolvers may add fields):
    #   {
    #     "trigger_reason": str,        # e.g. "fact_threshold_reached"
    #     "window": {"start_event_id": int, "end_event_id": int},
    #     "counts": {"facts": int, "skills": int, ...},
    #     "ranges": {"facts": [first_id, last_id], ...},
    #   }
    #
    # Default ``{}`` so existing evolvers / tests stay green; lineage
    # cards just degrade to "no lineage data" for older proposals.
    triggered_by: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_event_metadata(self) -> dict[str, Any]:
        """Serialise to the BEP §3.4 ``evolution_proposal.metadata``
        shape — what gets stored in the EventLog row."""
        return {
            "edit_id": self.edit_id,
            "evolver": self.evolver,
            "target_namespace": self.target_namespace,
            "target_version_pre": self.target_version_pre,
            "target_version_post": self.target_version_post,
            "evidence_event_ids": list(self.evidence_event_ids),
            "evidence_summary": self.evidence_summary,
            "inferred_root_cause": self.inferred_root_cause,
            "change_summary": self.change_summary,
            "change_diff": list(self.change_diff),
            "predicted_fixes": [
                {"task_kind": p.task_kind, "reason": p.reason}
                for p in self.predicted_fixes
            ],
            "predicted_regressions": [
                {"task_kind": p.task_kind, "reason": p.reason, "severity": p.severity}
                for p in self.predicted_regressions
            ],
            "rollback_pointer": self.rollback_pointer,
            "expires_after_events": self.expires_after_events,
            "triggered_by": dict(self.triggered_by),
        }


@dataclasses.dataclass
class FixMatch:
    """One predicted fix evaluated against observed events."""
    task_kind: str
    observed_count: int
    outcome: Literal["fixed", "no_signal"]


@dataclasses.dataclass
class ObservedRegression:
    """A regression observed during the verdict window — predicted
    or unpredicted."""
    task_kind: str
    observed_count: int
    severity: Severity
    evidence: str = ""


@dataclasses.dataclass
class EvolutionVerdict:
    """Outcome of evaluating an :class:`EvolutionProposal` over an
    observation window."""
    edit_id: str
    verdict_at_event: int           # sync_id at which the verdict was scored
    events_observed: int

    predicted_fix_match: list[FixMatch] = dataclasses.field(default_factory=list)
    predicted_fix_miss: list[FixMatch] = dataclasses.field(default_factory=list)
    predicted_regression_match: list[ObservedRegression] = dataclasses.field(default_factory=list)
    predicted_regression_miss: list[TaskKindPrediction] = dataclasses.field(default_factory=list)
    unpredicted_regressions: list[ObservedRegression] = dataclasses.field(default_factory=list)

    fix_score: float = 0.0          # 0..1 — hits / (hits + miss)
    regression_score: float = 0.0   # 0..1 — severity-weighted
    abc_drift_delta: float = 0.0    # ABC drift change over the window

    decision: VerdictDecision = "kept"

    def to_event_metadata(self) -> dict[str, Any]:
        return {
            "edit_id": self.edit_id,
            "verdict_at_event": self.verdict_at_event,
            "events_observed": self.events_observed,
            "predicted_fix_match": [
                {"task_kind": f.task_kind, "observed_count": f.observed_count, "outcome": f.outcome}
                for f in self.predicted_fix_match
            ],
            "predicted_fix_miss": [
                {"task_kind": f.task_kind, "observed_count": f.observed_count, "outcome": f.outcome}
                for f in self.predicted_fix_miss
            ],
            "predicted_regression_match": [
                {"task_kind": r.task_kind, "observed_count": r.observed_count,
                 "severity": r.severity, "evidence": r.evidence}
                for r in self.predicted_regression_match
            ],
            "predicted_regression_miss": [
                {"task_kind": p.task_kind, "reason": p.reason, "severity": p.severity}
                for p in self.predicted_regression_miss
            ],
            "unpredicted_regressions": [
                {"task_kind": r.task_kind, "observed_count": r.observed_count,
                 "severity": r.severity, "evidence": r.evidence}
                for r in self.unpredicted_regressions
            ],
            "fix_score": round(self.fix_score, 4),
            "regression_score": round(self.regression_score, 4),
            "abc_drift_delta": round(self.abc_drift_delta, 4),
            "decision": self.decision,
        }


@dataclasses.dataclass
class EvolutionRevert:
    """A rollback emitted when a verdict triggers ``decision = reverted``
    or when the user manually reverts."""
    edit_id: str
    rolled_back_to: str
    rolled_back_from: str
    trigger: RevertTrigger
    evidence: str = ""

    def to_event_metadata(self) -> dict[str, Any]:
        return {
            "edit_id": self.edit_id,
            "rolled_back_to": self.rolled_back_to,
            "rolled_back_from": self.rolled_back_from,
            "trigger": self.trigger,
            "evidence": self.evidence,
        }


# ── Verdict scoring (BEP §3.4 normative) ──────────────────────────────


# Severity numeric weights — higher means more bad.
_SEVERITY_WEIGHT: dict[Severity, float] = {
    "low": 0.2,
    "medium": 0.6,
    "high": 1.0,
}


def score_verdict(
    proposal: EvolutionProposal,
    *,
    verdict_at_event: int,
    events_observed: int,
    observed_fixes: Iterable[tuple[str, int]],
    observed_regressions: Iterable[tuple[str, int, Severity, str]],
    abc_drift_delta: float = 0.0,
    drift_thresholds: Optional[DriftThresholds] = None,
) -> EvolutionVerdict:
    """Compute the verdict for a proposal given the observed
    counters from the verdict window.

    Args:
        proposal: The :class:`EvolutionProposal` to evaluate.
        verdict_at_event: ``sync_id`` of the event at which the
            scorer is being invoked. Recorded in the verdict.
        events_observed: How many events were in the observation
            window. Used for diagnostics; doesn't affect the decision.
        observed_fixes: Iterable of ``(task_kind, observed_count)``
            for task kinds that the verdict scorer (or human user
            via approval) marked as "this turn went well — looks
            like a fix happened".
        observed_regressions: Iterable of
            ``(task_kind, observed_count, severity, evidence)``
            for task kinds where the scorer detected a problem
            during the window (irrespective of whether the
            proposal predicted it).
        abc_drift_delta: ABC ``DriftScore`` change observed over
            the window. Positive = drifted toward non-compliance.
        drift_thresholds: Override default warning / intervention
            thresholds.

    Returns:
        An :class:`EvolutionVerdict` with the decision baked in.

    Decision rules (BEP §3.4):
        * ``reverted`` — any unpredicted_regression with severity
          ∈ {medium, high} OR ``abc_drift_delta > intervention``.
        * ``kept_with_warning`` — any unpredicted_regression with
          severity = low OR ``abc_drift_delta > warning``.
        * ``kept`` — otherwise.

    The scorer **never** reverts on predicted-but-unobserved
    regressions. That's the AHE paper's lesson: regression
    prediction is essentially random; trusting it as a revert
    trigger creates false alarms.
    """
    thresh = drift_thresholds or DriftThresholds()

    observed_fix_set = {(tk, n) for tk, n in observed_fixes}
    observed_regs = list(observed_regressions)

    # ── Predicted fixes → match / miss ────────────────────────
    predicted_fix_kinds = {p.task_kind for p in proposal.predicted_fixes}
    fix_match: list[FixMatch] = []
    fix_miss: list[FixMatch] = []
    matched_fix_kinds: set[str] = set()

    for task_kind, observed_count in observed_fix_set:
        if task_kind in predicted_fix_kinds:
            fix_match.append(FixMatch(
                task_kind=task_kind,
                observed_count=observed_count,
                outcome="fixed",
            ))
            matched_fix_kinds.add(task_kind)

    for predicted in proposal.predicted_fixes:
        if predicted.task_kind not in matched_fix_kinds:
            fix_miss.append(FixMatch(
                task_kind=predicted.task_kind,
                observed_count=0,
                outcome="no_signal",
            ))

    fix_score = len(fix_match) / max(1, len(fix_match) + len(fix_miss))

    # ── Predicted vs observed regressions ──────────────────────
    predicted_reg_kinds = {p.task_kind for p in proposal.predicted_regressions}
    matched_predicted_regs: set[str] = set()

    predicted_regression_match: list[ObservedRegression] = []
    unpredicted_regressions: list[ObservedRegression] = []

    for tk, count, severity, evidence in observed_regs:
        obs = ObservedRegression(
            task_kind=tk,
            observed_count=count,
            severity=severity,
            evidence=evidence,
        )
        if tk in predicted_reg_kinds:
            predicted_regression_match.append(obs)
            matched_predicted_regs.add(tk)
        else:
            unpredicted_regressions.append(obs)

    predicted_regression_miss = [
        p for p in proposal.predicted_regressions
        if p.task_kind not in matched_predicted_regs
    ]

    # Regression score — severity-weighted, includes predicted+unpredicted
    # (everything that *actually happened* counts; predictions don't).
    if observed_regs:
        total_weight = sum(
            _SEVERITY_WEIGHT[sev] for _tk, _n, sev, _e in observed_regs
        )
        # Normalise: 1 medium = 0.6; 2 mediums = 1.0 (capped).
        regression_score = min(1.0, total_weight)
    else:
        regression_score = 0.0

    # ── Decision (BEP §3.4 normative) ───────────────────────────
    has_severe_observed = any(
        r.severity in ("medium", "high") for r in
        (predicted_regression_match + unpredicted_regressions)
    )
    has_low_observed = any(
        r.severity == "low" for r in
        (predicted_regression_match + unpredicted_regressions)
    )

    if has_severe_observed or abc_drift_delta > thresh.intervention:
        decision: VerdictDecision = "reverted"
    elif has_low_observed or abc_drift_delta > thresh.warning:
        decision = "kept_with_warning"
    else:
        decision = "kept"

    return EvolutionVerdict(
        edit_id=proposal.edit_id,
        verdict_at_event=verdict_at_event,
        events_observed=events_observed,
        predicted_fix_match=fix_match,
        predicted_fix_miss=fix_miss,
        predicted_regression_match=predicted_regression_match,
        predicted_regression_miss=predicted_regression_miss,
        unpredicted_regressions=unpredicted_regressions,
        fix_score=fix_score,
        regression_score=regression_score,
        abc_drift_delta=abc_drift_delta,
        decision=decision,
    )


# ── Event factories — produce ready-to-append EventLog rows ──────────


def make_proposal_event(
    proposal: EvolutionProposal,
    *,
    sync_id: int,
    session_id: str,
    client_created_at: str,
    server_received_at: str,
) -> dict[str, Any]:
    """Build an EventLog row for an ``evolution_proposal`` event.

    The ``content`` field is a short human-readable summary so a
    plaintext EventLog reader still has signal; the structured
    fields go into ``metadata``.
    """
    return {
        "client_created_at": client_created_at,
        "event_type": "evolution_proposal",
        "content": (
            f"{proposal.evolver} → {proposal.target_namespace}: "
            f"{proposal.change_summary}"
        ),
        "metadata": proposal.to_event_metadata(),
        "session_id": session_id,
        "sync_id": sync_id,
        "server_received_at": server_received_at,
    }


def make_verdict_event(
    verdict: EvolutionVerdict,
    *,
    sync_id: int,
    session_id: str,
    client_created_at: str,
    server_received_at: str,
) -> dict[str, Any]:
    """Build an EventLog row for an ``evolution_verdict`` event."""
    return {
        "client_created_at": client_created_at,
        "event_type": "evolution_verdict",
        "content": (
            f"verdict for {verdict.edit_id}: {verdict.decision} "
            f"(fix={verdict.fix_score:.2f}, "
            f"reg={verdict.regression_score:.2f})"
        ),
        "metadata": verdict.to_event_metadata(),
        "session_id": session_id,
        "sync_id": sync_id,
        "server_received_at": server_received_at,
    }


def make_revert_event(
    revert: EvolutionRevert,
    *,
    sync_id: int,
    session_id: str,
    client_created_at: str,
    server_received_at: str,
) -> dict[str, Any]:
    """Build an EventLog row for an ``evolution_revert`` event."""
    return {
        "client_created_at": client_created_at,
        "event_type": "evolution_revert",
        "content": (
            f"reverted {revert.edit_id}: "
            f"{revert.rolled_back_from} → {revert.rolled_back_to} "
            f"(trigger: {revert.trigger})"
        ),
        "metadata": revert.to_event_metadata(),
        "session_id": session_id,
        "sync_id": sync_id,
        "server_received_at": server_received_at,
    }


__all__ = [
    "Severity",
    "VerdictDecision",
    "RevertTrigger",
    "DriftThresholds",
    "TaskKindPrediction",
    "EvolutionProposal",
    "FixMatch",
    "ObservedRegression",
    "EvolutionVerdict",
    "EvolutionRevert",
    "score_verdict",
    "make_proposal_event",
    "make_verdict_event",
    "make_revert_event",
]
