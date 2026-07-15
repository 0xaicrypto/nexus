"""VerdictRunner — closes the falsifiable-evolution loop (Phase O.4).

Phase O.2 made each evolver emit an ``evolution_proposal`` event into
the EventLog before its writes. VerdictRunner is the consumer: it
periodically scans the log, finds proposals whose observation window
has elapsed, and writes back ``evolution_verdict`` events with one of
three decisions per BEP-Nexus §3.4:

* ``kept`` — no observed regressions, no severe drift
* ``kept_with_warning`` — low-severity regressions OR drift > warning
* ``reverted`` — medium/high regressions OR drift > intervention

When a proposal is reverted, the runner additionally:

  1. calls ``store.rollback(prev_version)`` on the appropriate
     namespace store (PersonaStore / FactsStore / etc.)
  2. emits an ``evolution_revert`` event referencing the verdict

Predictions (``predicted_fixes`` / ``predicted_regressions``) on the
proposal are passed straight through to ``score_verdict`` — when the
emitting evolver leaves them empty (today's MVP), the scorer's
"unpredicted_regressions" path drives the decision via observed
contract violations and ABC drift. A task_kind classifier landing in a
follow-up will populate predictions for richer scoring.

Failure isolation: per-proposal scoring errors are caught and logged
so one corrupt proposal can't block verdicts on the others.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol

from nexus_core.evolution import (
    DriftThresholds,
    EvolutionProposal,
    EvolutionRevert,
    EvolutionVerdict,
    TaskKindPrediction,
    score_verdict,
)
from nexus_core.memory import Event, EventLog

logger = logging.getLogger("nexus.evolution.verdict_runner")


# ── Store protocol ────────────────────────────────────────────────


class _RollbackableStore(Protocol):
    """Subset of VersionedStore that VerdictRunner needs.

    PersonaStore / FactsStore / EpisodesStore / SkillsStore /
    KnowledgeStore all satisfy this — they delegate to the same
    ``VersionedStore`` primitive.
    """

    def rollback(self, version: str) -> str: ...

    def current_version(self) -> Optional[str]: ...


# ── Helpers ───────────────────────────────────────────────────────


def _proposal_from_event(ev: Event) -> Optional[EvolutionProposal]:
    """Reconstruct a Proposal from an EventLog row.

    Forward-compat: unknown fields are ignored (the dataclass has a
    fixed set), so older / newer schemas don't crash the runner.
    """
    md = ev.metadata or {}
    try:
        predicted_fixes = [
            TaskKindPrediction(
                task_kind=p.get("task_kind", ""),
                reason=p.get("reason", ""),
            )
            for p in md.get("predicted_fixes", [])
        ]
        predicted_regressions = [
            TaskKindPrediction(
                task_kind=p.get("task_kind", ""),
                reason=p.get("reason", ""),
                severity=p.get("severity", "low"),
            )
            for p in md.get("predicted_regressions", [])
        ]
        return EvolutionProposal(
            edit_id=md.get("edit_id", ""),
            evolver=md.get("evolver", ""),
            target_namespace=md.get("target_namespace", ""),
            target_version_pre=md.get("target_version_pre", ""),
            target_version_post=md.get("target_version_post", ""),
            evidence_event_ids=list(md.get("evidence_event_ids", []) or []),
            evidence_summary=md.get("evidence_summary", ""),
            inferred_root_cause=md.get("inferred_root_cause", ""),
            change_summary=md.get("change_summary", ""),
            change_diff=list(md.get("change_diff", []) or []),
            predicted_fixes=predicted_fixes,
            predicted_regressions=predicted_regressions,
            rollback_pointer=md.get("rollback_pointer", ""),
            expires_after_events=md.get("expires_after_events", 100),
            triggered_by=dict(md.get("triggered_by", {}) or {}),
        )
    except Exception as e:  # noqa: BLE001 — corrupt row, log and skip
        logger.warning("could not parse proposal event idx=%s: %s", ev.index, e)
        return None


def _is_regression(ev: Event) -> bool:
    """Treat a contract_check event as a regression when it failed
    or recorded a hard violation. Soft violations alone are not
    counted (the contract engine has its own recovery_window logic
    for those)."""
    if ev.event_type != "contract_check":
        return False
    md = ev.metadata or {}
    return bool(md.get("hard_violation")) or md.get("passed") is False


def _severity_for(ev: Event) -> str:
    """Map contract_check severity → BEP-Nexus severity vocabulary."""
    md = ev.metadata or {}
    if md.get("hard_violation"):
        return "high"
    return "low"


# ── VerdictRunner ─────────────────────────────────────────────────


class VerdictRunner:
    """Scans the EventLog for unsettled proposals, scores them, and
    applies the verdict (rollback when needed).

    Typical usage from a long-running twin::

        runner = VerdictRunner(
            event_log=twin.event_log,
            stores={
                "memory.persona": twin.persona_store,
                "memory.facts":   twin.facts,
            },
            drift=twin.drift,
        )
        # Run after each compaction round (or on a periodic timer)
        verdicts = runner.score_pending()
    """

    def __init__(
        self,
        event_log: EventLog,
        *,
        stores: Optional[dict[str, _RollbackableStore]] = None,
        drift: Any = None,
        thresholds: Optional[DriftThresholds] = None,
        default_window: int = 100,
        scan_limit: int = 1000,
        # Phase D: ``rollback_handlers`` is gone. Phase O.6 used
        # per-namespace ``apply_rollback`` callbacks to re-sync
        # legacy artifacts after a typed-store rollback; with the
        # legacy artifacts deleted, the typed-store rollback IS
        # the rollback — chat-time projections rebuild their
        # caches from the typed store on the next read.
    ):
        self.event_log = event_log
        self.stores = stores or {}
        self.drift = drift
        self.thresholds = thresholds or DriftThresholds()
        self.default_window = default_window
        self.scan_limit = scan_limit

    # ── Public API ────────────────────────────────────────────────

    def score_pending(self, *, force: bool = False) -> list[EvolutionVerdict]:
        """Score every unsettled proposal whose window has elapsed.

        Args:
            force: when True, score even proposals whose observation
                window has not yet closed. Useful for tests and for
                user-triggered "evaluate now" actions.

        Returns:
            The list of verdicts written to the event log this run.
        """
        all_events = self.event_log.recent(limit=self.scan_limit)
        # ``recent`` returns oldest-first when iterated forward — but
        # the implementation actually returns newest-first reversed
        # to oldest-first. We rely on idx ordering instead.
        all_events.sort(key=lambda e: e.index)

        # Index settled edit_ids so we don't double-score
        settled: set[str] = set()
        for ev in all_events:
            if ev.event_type == "evolution_verdict":
                edit_id = (ev.metadata or {}).get("edit_id")
                if edit_id:
                    settled.add(edit_id)

        latest_idx = all_events[-1].index if all_events else 0
        verdicts: list[EvolutionVerdict] = []

        for ev in all_events:
            if ev.event_type != "evolution_proposal":
                continue
            proposal = _proposal_from_event(ev)
            if proposal is None or not proposal.edit_id:
                continue
            if proposal.edit_id in settled:
                continue

            window = max(1, proposal.expires_after_events or self.default_window)
            events_observed = max(0, latest_idx - ev.index)
            if not force and events_observed < window:
                continue  # still pending

            try:
                verdict = self._score_one(
                    proposal=proposal,
                    proposal_event=ev,
                    all_events=all_events,
                    latest_idx=latest_idx,
                    events_observed=events_observed,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "verdict scoring failed for edit_id=%s: %s",
                    proposal.edit_id, e,
                )
                continue

            self._emit_verdict(verdict)
            verdicts.append(verdict)

            if verdict.decision == "reverted":
                self._apply_revert(proposal=proposal, verdict=verdict)

        return verdicts

    # ── Internals ─────────────────────────────────────────────────

    def _score_one(
        self,
        *,
        proposal: EvolutionProposal,
        proposal_event: Event,
        all_events: list[Event],
        latest_idx: int,
        events_observed: int,
    ) -> EvolutionVerdict:
        """Build an EvolutionVerdict for one proposal."""
        # Window of events that arrived AFTER the proposal.
        window_events = [
            e for e in all_events
            if e.index > proposal_event.index and e.index <= latest_idx
        ]

        # Aggregate contract violations as observed regressions.
        # The current MVP buckets all violations under a synthetic
        # task_kind "contract" — the upcoming task_kind classifier
        # will split this into real per-task_kind buckets.
        violations = [e for e in window_events if _is_regression(e)]
        observed_regressions: list[tuple[str, int, str, str]] = []
        if violations:
            high_count = sum(
                1 for e in violations
                if (e.metadata or {}).get("hard_violation")
            )
            low_count = len(violations) - high_count
            if high_count:
                observed_regressions.append((
                    "contract", high_count, "high",
                    f"{high_count} hard contract violation(s) in window",
                ))
            if low_count:
                observed_regressions.append((
                    "contract", low_count, "low",
                    f"{low_count} soft contract failure(s) in window",
                ))

        # Drift delta — best-effort. We don't snapshot drift at
        # proposal time yet, so we use current drift directly. The
        # decision rules use ``> intervention`` so a drift currently
        # below intervention won't trigger revert.
        drift_now = 0.0
        if self.drift is not None:
            try:
                drift_now = float(self.drift.current())
            except Exception:  # noqa: BLE001
                drift_now = 0.0

        return score_verdict(
            proposal=proposal,
            verdict_at_event=latest_idx,
            events_observed=events_observed,
            observed_fixes=[],
            observed_regressions=observed_regressions,
            abc_drift_delta=drift_now,
            drift_thresholds=self.thresholds,
        )

    def _emit_verdict(self, verdict: EvolutionVerdict) -> None:
        try:
            self.event_log.append(
                event_type="evolution_verdict",
                content=(
                    f"verdict for {verdict.edit_id}: {verdict.decision} "
                    f"(reg={verdict.regression_score:.2f}, "
                    f"drift={verdict.abc_drift_delta:.2f})"
                ),
                metadata=verdict.to_event_metadata(),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("emit evolution_verdict failed: %s", e)

    def _apply_revert(
        self,
        *,
        proposal: EvolutionProposal,
        verdict: EvolutionVerdict,
    ) -> None:
        """Roll the namespace store back to the proposal's
        ``rollback_pointer`` and emit an ``evolution_revert`` event."""
        store = self.stores.get(proposal.target_namespace)
        rolled_from = ""
        rolled_to = proposal.rollback_pointer or ""
        if store is not None and rolled_to and rolled_to != "(uncommitted)":
            try:
                rolled_from = store.current_version() or ""
                store.rollback(rolled_to)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "rollback failed for %s → %s: %s",
                    proposal.target_namespace, rolled_to, e,
                )
                rolled_from = ""  # signal failure to the revert event
        else:
            logger.info(
                "no rollback target for %s (rollback_pointer=%r) — "
                "emitting revert event without store-side rollback",
                proposal.target_namespace, rolled_to,
            )

        # Phase D removed the per-evolver apply_rollback dispatch.
        # The typed-store rollback above (line 332-345) IS now the
        # complete rollback — there are no legacy artifacts to
        # re-sync. The evolvers' in-memory projections rebuild from
        # the rolled-back typed store on their next ``load_*`` call.

        revert = EvolutionRevert(
            edit_id=proposal.edit_id,
            rolled_back_to=rolled_to,
            rolled_back_from=rolled_from,
            trigger="verdict",
            evidence=(
                f"reg={verdict.regression_score:.2f}, "
                f"drift={verdict.abc_drift_delta:.2f}"
            ),
        )
        try:
            self.event_log.append(
                event_type="evolution_revert",
                content=(
                    f"reverted {proposal.edit_id}: "
                    f"{rolled_from} → {rolled_to} (trigger=verdict)"
                ),
                metadata=revert.to_event_metadata(),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("emit evolution_revert failed: %s", e)


__all__ = ["VerdictRunner"]
