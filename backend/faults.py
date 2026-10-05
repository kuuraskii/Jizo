"""
JIZO - Part 1 evidence bus and temporal guard engine.

Two ideas carry this whole file:

1. Evidence bus: write down every step of every call, keyed by traceId.
   Nothing is guessed later - we replay what actually happened.

2. After-guards: a fault may only fire *after* proof that the risky moment
   has arrived (e.g. "only after the upstream already committed the work").
   This is what lets JIZO reproduce failures that a blunt "break the API"
   injector can never reproduce.

No network calls here. Everything runs on stub events, which keeps the
hardest logic testable in milliseconds.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

from .schemas import (
    BreakerState,
    DrillSpec,
    EvidenceEvent,
    FaultTarget,
    FaultType,
    GuardAfter,
    Phase,
    Pattern,
    ServedFrom,
)


class EvidenceBus:
    """In-memory record of what happened, grouped by traceId.

    Think of it as a lab notebook. Every party (proxy, breaker, drill
    engine) writes lines here; the scorer later reads the notebook and
    decides whether the system behaved correctly.

    The occurrence counter is the important part: for a given
    (traceId, api, phase) it counts 1st, 2nd, 3rd... so a k-of-n drill can
    say "only the 3rd call breaks" with real evidence behind it.
    """

    def __init__(self) -> None:
        # trace_id -> list of events, in the order they were recorded.
        self._events: dict[str, list[EvidenceEvent]] = defaultdict(list)
        # (trace_id, api_key, phase) -> how many times we have seen it.
        self._counters: dict[tuple[str, str, Phase], int] = defaultdict(int)
        # (trace_id, api_key) -> how many CALLS have been started.
        # A call is counted once, when it sends. Its fallback row does not
        # start a new call, which is what keeps "the 3rd call" honest.
        self._calls: dict[tuple[str, str], int] = defaultdict(int)

    # -- writing -----------------------------------------------------------

    def record(
        self,
        trace_id: str,
        api_key: str,
        phase: Phase,
        *,
        served_from: ServedFrom = ServedFrom.NONE,
        status_code: Optional[int] = None,
        effect_applied: bool = False,
        fault: Optional[FaultType] = None,
        latency_ms: Optional[float] = None,
        breaker_state: Optional[BreakerState] = None,
        leaked_raw_error: bool = False,
        call_index: Optional[int] = None,
        note: Optional[str] = None,
    ) -> EvidenceEvent:
        """Write one event and return it (occurrence + call number assigned).

        `occurrence` counts rows per (api, phase). `call_index` counts CALLS,
        and only advances on SEND - so a call that logs a failed attempt and
        then a fallback still counts as one call.
        """
        key = (trace_id, api_key, phase)
        self._counters[key] += 1

        call_key = (trace_id, api_key)
        if call_index is None:
            if phase is Phase.SEND:
                # Starting a new call.
                self._calls[call_key] += 1
                call_index = self._calls[call_key]
            else:
                call_index = self._calls[call_key] or 1

        event = EvidenceEvent(
            trace_id=trace_id,
            api_key=api_key,
            phase=phase,
            served_from=served_from,
            status_code=status_code,
            effect_applied=effect_applied,
            fault=fault,
            occurrence=self._counters[key],
            call_index=call_index,
            latency_ms=latency_ms,
            breaker_state=breaker_state,
            leaked_raw_error=leaked_raw_error,
            note=note,
        )
        self._events[trace_id].append(event)
        return event

    # -- reading -----------------------------------------------------------

    def events(self, trace_id: str) -> list[EvidenceEvent]:
        """All events for one request, oldest first."""
        return list(self._events.get(trace_id, []))

    def phase_events(
        self, trace_id: str, api_key: str, phase: Phase
    ) -> list[EvidenceEvent]:
        """Events for one phase of one API - useful for counting occurrences."""
        return [
            e
            for e in self._events.get(trace_id, [])
            if e.api_key == api_key and e.phase == phase
        ]

    def call_events(self, trace_id: str, api_key: str, call_index: int) -> list[EvidenceEvent]:
        """Every row belonging to one call (all phases, in order).

        One call can produce several rows - a failed attempt, then a
        fallback - so this is the reliable way to ask "what happened on
        call 3?" rather than filtering by phase.
        """
        return [
            e
            for e in self._events.get(trace_id, [])
            if e.api_key == api_key and e.call_index == call_index
        ]

    def occurrence_count(self, trace_id: str, api_key: str, phase: Phase) -> int:
        """How many times this (api, phase) has been seen so far.

        Uses .get so that merely asking about an unseen key does not insert
        it - otherwise long-running processes leak counter entries.
        """
        return self._counters.get((trace_id, api_key, phase), 0)

    def traces(self) -> list[str]:
        """Every traceId we know about."""
        return list(self._events.keys())

    def reset(self, trace_id: Optional[str] = None) -> None:
        """Clear one trace, or the whole notebook (used between tests)."""
        if trace_id is None:
            self._events.clear()
            self._counters.clear()
            self._calls.clear()
            return
        self._events.pop(trace_id, None)
        for key in [k for k in self._counters if k[0] == trace_id]:
            self._counters.pop(key, None)
        for key in [k for k in self._calls if k[0] == trace_id]:
            self._calls.pop(key, None)

    def call_count(self, trace_id: str, api_key: str) -> int:
        """How many distinct calls have been started for this api."""
        return self._calls.get((trace_id, api_key), 0)


class GuardDecision:
    """The guard's answer, with the reason attached for logging."""

    def __init__(self, should_fire: bool, reason: str, occurrence: int) -> None:
        self.should_fire = should_fire
        self.reason = reason
        self.occurrence = occurrence

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return (
            f"GuardDecision(should_fire={self.should_fire}, "
            f"occurrence={self.occurrence}, reason={self.reason!r})"
        )


class GuardEvaluator:
    """Decides whether a pending fault is allowed to fire *right now*.

    Two conditions must both hold:
      1. The required prior evidence already exists (the After-guard).
      2. We are on exactly the requested occurrence k.

    That is what stops a "premature" fault - one that fires before the
    dangerous window has actually opened - which is the failure mode that
    makes naive injectors produce meaningless results.
    """

    def __init__(self, bus: EvidenceBus) -> None:
        self.bus = bus

    def guard_satisfied(self, trace_id: str, guard: GuardAfter) -> bool:
        """Has the required 'after' evidence arrived enough times?"""
        seen = self.bus.occurrence_count(trace_id, guard.api_key, guard.phase)
        return seen >= guard.min_count

    def should_fire(
        self, trace_id: str, target: FaultTarget, guard: GuardAfter
    ) -> GuardDecision:
        """Full decision for one pending fault injection.

        **Call order matters:** log the call's SEND event *first*, then ask.
        The guard then judges the call that is currently in flight, which is
        what the proxy naturally does - it has sent the request and is now
        deciding whether the fault should strike this attempt.

        We judge on CALL number, not row count. One call can log several
        RECV rows (a failed attempt plus a fallback), so counting rows would
        make "the 3rd call" drift.
        """
        calls_seen = self.bus.call_count(trace_id, target.api_key)
        # The in-flight call is the one just started by SEND. Before any
        # SEND exists we treat call 1 as the candidate, which makes the
        # guard hold rather than fire on nothing.
        occurrence = max(calls_seen, 1)

        if not self.guard_satisfied(trace_id, guard):
            seen = self.bus.occurrence_count(trace_id, guard.api_key, guard.phase)
            return GuardDecision(
                False,
                f"waiting for guard evidence ({guard.describe()}); have {seen}",
                occurrence,
            )

        if occurrence < target.occurrence:
            return GuardDecision(
                False,
                f"occurrence {occurrence} < target k={target.occurrence}; holding",
                occurrence,
            )

        if occurrence > target.occurrence:
            # We are past the intended call. Firing now would be a stray
            # fault on a healthy call, so we refuse and say why.
            return GuardDecision(
                False,
                f"occurrence {occurrence} > target k={target.occurrence}; "
                "target window already passed",
                occurrence,
            )

        return GuardDecision(
            True, f"guard satisfied ({guard.describe()}); on occurrence {occurrence}", occurrence
        )


# ---------------------------------------------------------------------------
# Pattern helpers
#
# Each helper builds a ready-to-run DrillSpec. They exist so the target and
# guard are always paired correctly - the most common source of bogus
# drill results is a hand-written guard that does not match the intent.
# ---------------------------------------------------------------------------


def post_effect_drill(
    run_id: str,
    api_key: str,
    *,
    k: int = 1,
    n: int = 4,
    fault: FaultType = FaultType.DROP_RESPONSE,
    idempotent: bool = False,
) -> DrillSpec:
    """Pattern 1 - the answer is lost AFTER the upstream committed the work.

    This is the dangerous one: retrying naively performs the action twice
    (double charge, double dispatch). Guard waits for POST_EFFECT evidence.

    `idempotent` defaults to False here (unlike k-of-n, which is a read) to
    make the unsafe default explicit at the call site.
    """
    return DrillSpec(
        run_id=run_id,
        pattern=Pattern.POST_EFFECT,
        fault=fault,
        target=FaultTarget(api_key=api_key, phase=Phase.RECV, occurrence=k),
        guard=GuardAfter(api_key=api_key, phase=Phase.POST_EFFECT, min_count=1),
        total_occurrences=n,
        idempotent=idempotent,
    )


def order_sensitive_drill(
    run_id: str,
    api_key: str,
    *,
    k: int = 1,
    n: int = 4,
    fault: FaultType = FaultType.RIVAL_RESPONSE,
    idempotent: bool = False,
) -> DrillSpec:
    """Pattern 2 - a rival/stale response arrives before the real one.

    The system must not commit on the rival answer. Guard waits until the
    call has actually been sent.
    """
    return DrillSpec(
        run_id=run_id,
        pattern=Pattern.ORDER_SENSITIVE,
        fault=fault,
        target=FaultTarget(api_key=api_key, phase=Phase.RECV, occurrence=k),
        guard=GuardAfter(api_key=api_key, phase=Phase.SEND, min_count=1),
        total_occurrences=n,
        idempotent=idempotent,
    )


def k_of_n_drill(
    run_id: str,
    api_key: str,
    *,
    k: int = 3,
    n: int = 4,
    fault: FaultType = FaultType.HTTP_500,
    idempotent: bool = True,
) -> DrillSpec:
    """Pattern 3 - only the k-th call out of n breaks; the rest are healthy.

    Proves the protection is precise (right call degrades) instead of
    blunt (everything degrades). Guard just requires the call to exist.
    """
    return DrillSpec(
        run_id=run_id,
        pattern=Pattern.K_OF_N,
        fault=fault,
        target=FaultTarget(api_key=api_key, phase=Phase.RECV, occurrence=k),
        guard=GuardAfter(api_key=api_key, phase=Phase.SEND, min_count=1),
        total_occurrences=n,
        idempotent=idempotent,
    )


def build_spec(
    run_id: str,
    pattern: Pattern,
    api_key: str,
    *,
    k: int = 1,
    n: int = 4,
    fault: Optional[FaultType] = None,
    idempotent: Optional[bool] = None,
) -> DrillSpec:
    """Pattern-dispatching convenience wrapper.

    Callers (e.g. POST /fi/run) can take a pattern name from the request
    and get the correctly-paired spec without importing three helpers.
    """
    builders = {
        Pattern.POST_EFFECT: post_effect_drill,
        Pattern.ORDER_SENSITIVE: order_sensitive_drill,
        Pattern.K_OF_N: k_of_n_drill,
    }
    builder = builders[pattern]
    kwargs: dict = {"k": k, "n": n}
    if fault is not None:
        kwargs["fault"] = fault
    if idempotent is not None:
        kwargs["idempotent"] = idempotent
    return builder(run_id, api_key, **kwargs)


def find_fault_events(
    events: Iterable[EvidenceEvent], fault: Optional[FaultType] = None
) -> list[EvidenceEvent]:
    """Every event where a fault struck.

    Pass `fault` to filter to one fault type - which is what a grader
    should do, since a drill only cares about the fault it requested.
    """
    if fault is None:
        return [e for e in events if e.fault is not None]
    return [e for e in events if e.fault == fault]