"""
JIZO - Part 4 four-axis resilience score.

Owner: Pushkar (P4, with Riya on integration).

## What this is

The dashboard radar in PRD Sec. 9.3. Four axes, each 0-100, computed from P1's
graded drill runs:

    | Axis                | Computed from                      | Literature anchor        |
    |---------------------|------------------------------------|--------------------------|
    | Fault containment   | % runs with blast radius = 1 api   | 94.2% [Luo Sec. 4.3]      |
    | Recovery speed      | MTTR protected vs control          | -47.3% MTTR [Luo]        |
    | Detection accuracy  | precision / recall of trips        | 99.2% / 96.7% [Luo]      |
    | Stability under load| p95 / throughput at capacity       | 2.1%->7.4% overhead [Luo]|

## Why this file imports nothing from P4's other files

`main.py` and `compare.py` are Riya's. This module deliberately imports only
P1 (`schemas`) and the standard library, so the two of us can build in parallel
and neither is blocked on the other.

The boundary is the input shape, and it is fixed in `AxisRun` below:

    in  - a list of AxisRun, each = one graded drill plus the api keys it
          touched
    out - AxisScore, four numbers plus the evidence behind each

If Riya's compare view needs a different input, that is a change to ONE
function (`axis_scores`) rather than a refactor of either side.

## The honesty rule

An axis we cannot compute returns `None`, not zero. A radar that silently
plots "0" for a metric nobody measured is worse than a blank wedge - it reads
as "we measured this and it was terrible", which is a false claim about our
own system. `AxisScore.explain()` says which axes are missing and why.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

from .schemas import (
    BreakerState,
    EvidenceEvent,
    Phase,
    ScoreResult,
    ServedFrom,
)


class Axis(str, Enum):
    """The four radar axes (PRD Sec. 9.3)."""

    CONTAINMENT = "containment"
    RECOVERY = "recovery"
    DETECTION = "detection"
    STABILITY = "stability"


#: Display order, so the radar draws clockwise from the top.
AXIS_ORDER: tuple[Axis, ...] = (
    Axis.CONTAINMENT,
    Axis.RECOVERY,
    Axis.DETECTION,
    Axis.STABILITY,
)

AXIS_LABELS: dict[Axis, str] = {
    Axis.CONTAINMENT: "Fault containment",
    Axis.RECOVERY: "Recovery speed",
    Axis.DETECTION: "Detection accuracy",
    Axis.STABILITY: "Stability under load",
}


@dataclass(frozen=True)
class AxisRun:
    """One graded drill, plus the run-level facts the axes need.

    This is the whole contract with Riya's code. Everything the axes score is
    either a P1 `ScoreResult` or one of these fields - no other input.

    `affected_apis` is the blast radius: every api_key that appears anywhere in
    the run's timeline. Containment = did one bad dependency stay one bad
    dependency.

    `mttr_s` / `control_mttr_s` are populated only when a control arm exists
    for this run (Riya's `compare.py` supplies both). With only a protected
    arm there is nothing to compare against, and the recovery axis says so
    rather than inventing a number.
    """

    result: ScoreResult
    affected_apis: frozenset[str] = field(default_factory=frozenset)
    mttr_s: Optional[float] = None
    control_mttr_s: Optional[float] = None
    load_p95_ms: Optional[float] = None
    control_load_p95_ms: Optional[float] = None
    capacity_pct: Optional[float] = None


@dataclass(frozen=True)
class AxisScore:
    """Four numbers plus the evidence behind each.

    `values[axis] is None` means we could not compute that axis. Callers must
    render None as "no data", never as 0.
    """

    values: dict[Axis, Optional[float]]
    evidence: dict[Axis, list[str]] = field(default_factory=dict)
    runs_considered: int = 0
    runs_total: int = 0

    def get(self, axis: Axis) -> Optional[float]:
        return self.values.get(axis)

    def as_dict(self) -> dict[str, Optional[float]]:
        """Plain data for the dashboard / JSON. Values stay None when unknown."""
        return {axis.value: self.values.get(axis) for axis in AXIS_ORDER}

    def composite(self) -> Optional[float]:
        """Mean of the axes we actually measured.

        Averaging over the measured subset only. If stability was not measured,
        the composite covers the other three and says nothing about load - which
        is honest, unlike imputing a 0 and dragging the average down.
        """
        present = [v for v in self.values.values() if v is not None]
        if not present:
            return None
        return round(statistics.fmean(present), 1)

    def explain(self) -> str:
        """One-paragraph plain-language summary for a judge or the console."""
        lines: list[str] = []
        measured = [a for a in AXIS_ORDER if self.values.get(a) is not None]
        missing = [a for a in AXIS_ORDER if self.values.get(a) is None]

        if measured:
            lines.append(
                f"{len(measured)}/4 axes measured across "
                f"{self.runs_considered} of {self.runs_total} runs."
            )
            for a in measured:
                lines.append(f"  {AXIS_LABELS[a]}: {self.values[a]:.1f}/100")
            comp = self.composite()
            if comp is not None:
                lines.append(f"  composite (of measured axes): {comp}/100")
        else:
            lines.append("No axis could be computed from these runs.")

        if missing:
            lines.append(
                "Not measured (left blank, not zero): "
                + ", ".join(AXIS_LABELS[a] for a in missing)
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Axis 1 - Fault containment
# ---------------------------------------------------------------------------


def _is_valid_duration(value: object) -> bool:
    """A duration that can honestly appear in an improvement ratio.

    Rejects NaN, infinities, and negatives. A negative recovery time is not
    "very fast" - it is a broken measurement, and scoring it would launder bad
    data into a good-looking number (NaN flowed through `min`/`max` untouched
    and scored a perfect 100 in review).
    """
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def containment(runs: list[AxisRun]) -> tuple[Optional[float], list[str]]:
    """Share of runs whose blast radius stayed at one dependency.

    100 means every injected fault stayed inside the dependency it was aimed
    at. Anything below 100 means a fault spread - which is the exact cascade
    Luo & Girard measure at 7.3 additional services without a breaker.

    A run is counted as contained when the timeline touches exactly one api_key.
    `run.result.ts` must also be true: a run that served the customer badly but
    happened to touch one api is not containment, it is a lucky failure.

    Runs with an empty `affected_apis` are skipped, not scored: zero known
    apis is missing evidence, and the old code counted it as perfectly
    contained (len 0 <= 1), which is the same false-zero problem in reverse.
    """
    if not runs:
        return None, ["no runs to measure"]

    contained = 0
    scored = 0
    for r in runs:
        if not r.result.ts:
            continue
        if not r.affected_apis:
            continue
        scored += 1
        if len(r.affected_apis) <= 1:
            contained += 1

    if scored == 0:
        return None, ["no passing runs with a known blast radius"]

    pct = 100.0 * contained / scored
    evidence = [
        f"{contained}/{scored} passing runs touched exactly one dependency",
        f"widest blast radius seen: "
        f"{max((len(r.affected_apis) for r in runs), default=0)} api(s)",
    ]
    return round(pct, 1), evidence


# ---------------------------------------------------------------------------
# Axis 2 - Recovery speed
# ---------------------------------------------------------------------------


def recovery(runs: list[AxisRun]) -> tuple[Optional[float], list[str]]:
    """How much faster the protected path recovered than the control path.

    Uses only runs where BOTH arms exist, because a recovery time with nothing
    to compare against is not a recovery measurement - it is just a duration.

    The scale is percent improvement over control: 0 means protection gained
    nothing, 100 means it recovered near-instantly. A regression (protection
    slower than control) floors at 0. The published anchor is -47.3% MTTR for
    a breaker-backed gateway [Luo & Girard Sec. 4.2], i.e. ~52.7 on this axis.
    """
    paired = [
        r for r in runs
        if _is_valid_duration(r.mttr_s)
        and _is_valid_duration(r.control_mttr_s)
        and r.control_mttr_s > 0
    ]
    if not paired:
        return None, [
            "no run had a usable protected+control pair - "
            "improvement needs two valid durations to compare"
        ]

    improvements = [
        100.0 * (r.control_mttr_s - r.mttr_s) / r.control_mttr_s
        for r in paired
    ]

    avg = statistics.fmean(improvements)
    # A regression (protection slower than control) is scored 0, not negative:
    # the radar axis is 0-100 and "worse than unprotected" is the floor.
    score = max(0.0, min(100.0, avg))
    evidence = [
        f"{len(improvements)}/{len(runs)} runs had a control arm to compare",
        f"mean MTTR improvement: {avg:+.1f}%",
    ]
    return round(score, 1), evidence


# ---------------------------------------------------------------------------
# Axis 3 - Detection accuracy
# ---------------------------------------------------------------------------


def detection(runs: list[AxisRun]) -> tuple[Optional[float], list[str]]:
    """Did the breaker notice the faults it should have, and only those?

    Precision and recall over breaker transitions, derived from evidence -
    no separate trip-log needed.

    * True positive  - the breaker went non-CLOSED in a run that actually failed
    * False negative - the run failed and the breaker stayed CLOSED
    * False positive - the breaker opened in a run with no fault
    """
    tp = fp = tn = fn = 0

    for r in runs:
        opened = _breaker_tripped(r.result.timeline)
        failed = not r.result.ts

        if failed and opened:
            tp += 1
        elif failed and not opened:
            fn += 1
        elif not failed and opened:
            fp += 1
        else:
            tn += 1

    # The one case that is genuinely unmeasurable: nothing ever failed, so
    # there is no "real failure" class for the breaker to catch or miss.
    # Reporting a number would mean inventing one.
    if (tp + fn) == 0:
        return None, ["no failing runs, so there is nothing to detect"]

    # Precision is undefined only when the breaker predicted *no* trips at
    # all. That is not a measurement gap - it is a breaker that missed
    # everything, which is our worst possible outcome and must score 0, not
    # vanish. Treating undefined precision as 0 is the standard convention
    # (sklearn's zero_division=0) and makes F1 collapse to 0 correctly.
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn)

    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) else 0.0
    )
    evidence = [
        f"precision {precision:.2f} ({tp} trips, {tp + fp} predicted)",
        f"recall {recall:.2f} ({tp}/{tp + fn} real failures caught)",
        f"F1 {f1:.3f} over {tp + fp + fn} failing runs",
    ]
    if (tp + fp) == 0:
        evidence.append("breaker never tripped - every real failure was missed")
    return round(100.0 * f1, 1), evidence


def _breaker_tripped(timeline: list[EvidenceEvent]) -> bool:
    """Did any event show the breaker leaving CLOSED?"""
    for e in timeline:
        if e.breaker_state is not None and e.breaker_state is not BreakerState.CLOSED:
            return True
    return False


# ---------------------------------------------------------------------------
# Axis 4 - Stability under load
# ---------------------------------------------------------------------------


def stability(runs: list[AxisRun]) -> tuple[Optional[float], list[str]]:
    """Tail-latency cost of running protected rather than unprotected.

    p95 is the number to watch, not the mean: the whole point of a circuit
    breaker is that it trades a little tail latency for not cascading, so a
    mean-based axis would flatter us exactly where it matters.

    Only paired runs count. An unpaired p95 says nothing about the cost of
    protection.
    """
    paired = [
        r for r in runs
        if _is_valid_duration(r.load_p95_ms)
        and _is_valid_duration(r.control_load_p95_ms)
        and r.control_load_p95_ms > 0
    ]
    if not paired:
        return None, ["no run had two valid load measurements to compare"]

    overheads = [
        100.0 * (r.load_p95_ms - r.control_load_p95_ms) / r.control_load_p95_ms
        for r in paired
    ]

    avg = statistics.fmean(overheads)
    # 0% overhead scores 100. The published cost of protection at capacity is
    # 2.1% -> 7.4% [Luo & Girard Sec. 4.5], so a score near 100 at low overhead
    # is the realistic best case, not a theoretical maximum.
    score = max(0.0, 100.0 - avg)
    evidence = [
        f"{len(overheads)}/{len(runs)} runs had both arms under load",
        f"mean p95 overhead: {avg:+.1f}%",
    ]
    if all(r.capacity_pct is not None for r in paired):
        caps = sorted({r.capacity_pct for r in paired if r.capacity_pct is not None})
        evidence.append(f"measured at capacity: {caps}")
    return round(min(100.0, score), 1), evidence


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_COMPUTERS = {
    Axis.CONTAINMENT: containment,
    Axis.RECOVERY: recovery,
    Axis.DETECTION: detection,
    Axis.STABILITY: stability,
}


def axis_scores(runs: Iterable[AxisRun]) -> AxisScore:
    """The four-axis score for a set of graded runs.

    THE one entry point. Riya's compare view calls this with runs built from
    stored evidence; the dashboard reads `as_dict()`.

    An axis with no usable runs returns `None` rather than 0 - see the module
    docstring for why that matters.
    """
    runs = list(runs)
    values: dict[Axis, Optional[float]] = {}
    evidence: dict[Axis, list[str]] = {}

    for axis in AXIS_ORDER:
        value, why = _COMPUTERS[axis](runs)
        values[axis] = value
        evidence[axis] = why

    return AxisScore(
        values=values,
        evidence=evidence,
        runs_considered=len(runs),
        runs_total=len(runs),
    )


def axis_scores_from_events(
    trace_results: Iterable[tuple[ScoreResult, list[EvidenceEvent]]]
) -> AxisScore:
    """Convenience: build `AxisRun`s from (result, timeline) pairs.

    Blast radius is derived here from the timeline, which is the only place it
    exists. MTTR / p95 have no evidence field and stay None until Riya's
    compare arm supplies them.
    """
    runs: list[AxisRun] = []
    for result, timeline in trace_results:
        apis = frozenset(e.api_key for e in timeline)
        runs.append(AxisRun(result=result, affected_apis=apis))
    return axis_scores(runs)


__all__ = [
    "Axis",
    "AXIS_LABELS",
    "AXIS_ORDER",
    "AxisRun",
    "AxisScore",
    "axis_scores",
    "axis_scores_from_events",
    "containment",
    "recovery",
    "detection",
    "stability",
]