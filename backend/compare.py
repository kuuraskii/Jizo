"""
JIZO - Part 4 control-vs-experiment comparison.

A single run tells you the system worked. Only a comparison tells you *your
change* worked: run the same workload twice, once through the protected path
and once straight at the upstream, and read the delta. That is the Netflix
ChAP method [Basiri et al. 2016/17], and it is what this module implements.

    control     the unprotected arm - no breaker, no retry, no fallback
    experiment  the protected arm - P2's ``resilient_get`` end to end

The report answers the four questions the PRD asks a judge to be able to
read off one table:

    success     did the customer get an answer?
    fallback    was that answer a degraded one?
    duplicates  did the same call do its work twice?
    ts          did the run satisfy Temporal Success?

    delta       experiment minus control, same units

## Three deliberate constraints

1. **No FastAPI, no SQLAlchemy, no database.** This module is pure: it takes
   events and returns a report. The route in ``main.py`` owns the HTTP shape
   and the mode-split query; this file owns the arithmetic. That is what makes
   the comparison testable in milliseconds against stub evidence, on the same
   discipline P1's scorer follows (``edgecases/README.md`` invariant 1).

2. **"Servable" is imported, not redefined.** ``SERVABLE_SOURCES`` comes from
   ``backend.scoring``, so the comparator and the grader cannot drift on what
   counts as an answer. The scorer computes a boolean answer to "was this run
   correct"; this module counts how often each arm produced one.

3. **Rates are fractions, not percentages, and say so in their name.**
   ``success_rate`` is 0.0-1.0. P3-G13 records a real bug where a caller passed
   ``0.25`` meaning 25% into a column documented as 0-100 and stored 0.25%
   silently. A ``_rate`` suffix plus an explicit docstring is the cheapest way
   to not repeat that.

## Rules borrowed from the scorer, so the two cannot disagree

* **A call is ``(api_key, call_index)``, never ``call_index`` alone.** The bus
  keys counters on ``(trace_id, api_key)``, so in a ``/route/plan`` fan-out
  weather's call 1 and geocode's call 1 are different calls. The scorer never
  has to think about this because it scopes to one API first; this module is
  handed both arms and may span several APIs, so it must.
* **The LAST response row of a call decides.** Same rule as the scorer's
  ``_served_live``: a call that served live and then died on that same call did
  not finish healthy, and an ``any()`` match would call it healthy anyway.
* **TS is read, never recomputed.** ``ScoreResult.ts`` is P1's frozen verdict.
  This module averages it; it never re-derives the conjunction.

## Known limitation carried over from P1 (B1)

``duplicates`` counts applied effects per ``(api_key, call_index)``, exactly as
P1's ``Mult`` does. That means it is structurally blind to a *retried*
non-idempotent double charge, because ``EvidenceBus`` advances ``call_index``
on every SEND so a retry becomes a new call. ``backend/scoring.py`` documents
this as open contract question **B1** and it is unresolved - see the
"OPEN CONTRACT QUESTIONS" section of that file. This module reproduces
today's rule rather than inventing a different one, so the comparator and the
grader always agree. Fixing B1 changes both together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .schemas import EvidenceEvent, Phase, ScoreResult, ServedFrom
from .scoring import SERVABLE_SOURCES


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


#: The two arms of the comparison. Mirrors ``MODE_VALUES`` in
#: ``backend/models.py`` (the CHECK constraint on ``request_logs.mode`` and the
#: ``ix_request_logs_mode`` index both use it). Deliberately not imported:
#: ``models`` pulls in SQLAlchemy, and this module must stay importable - and
#: testable - with no database stack present.
MODES = ("control", "experiment")

#: Which arm a request goes to when the caller did not say. The protected arm
#: is the safe default: defaulting to ``control`` would silently serve real
#: user traffic with no breaker, no retry and no fallback.
DEFAULT_MODE = "experiment"


def resolve_mode(raw: Optional[str]) -> str:
    """Normalise the traffic-splitter's input into one of :data:`MODES`.

    Pure. ``main.py`` passes the query parameter straight in; this decides.

    ``None`` or empty becomes :data:`DEFAULT_MODE`. Anything else must be one
    of the two arms exactly - not a case-folded near-miss - so a typo fails
    loudly at the door instead of quietly running traffic down an unintended
    path.

    Raises:
        ValueError: if ``raw`` is neither arm. P1's ``build_spec`` raises a
            bare ``KeyError`` for the same class of mistake, which edgecases
            L-05 flags as an unhelpful 500; this raises a readable message
            from the start.
    """
    if raw is None:
        return DEFAULT_MODE
    cleaned = raw.strip().lower()
    if not cleaned:
        return DEFAULT_MODE
    if cleaned not in MODES:
        raise ValueError(
            f"unknown mode {raw!r}; expected one of {', '.join(MODES)}"
        )
    return cleaned


# ---------------------------------------------------------------------------
# Per-arm metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SideMetrics:
    """What one arm of the comparison actually did.

    ``calls`` counts every call that appears in the evidence, including one
    that never produced a response row. That is deliberate and it fails safe:
    a protection layer that gave up before answering has not succeeded, and
    crediting it would inflate the rate.

    The three rates are fractions in ``0.0-1.0``, named with a ``_rate``
    suffix so they cannot be confused with the 0-100 percentages used
    elsewhere in the project (see the module docstring, and P3-G13).

    ``ts_rate`` is ``None`` when no :class:`ScoreResult` was supplied, which is
    the honest "we do not know" answer - the same reasoning behind
    ``store.event_to_row`` defaulting ``mode`` to ``None`` rather than
    ``"control"``.
    """

    calls: int
    success: int
    fallback: int
    duplicates: int
    runs: int = 0
    success_rate: float = 0.0
    fallback_rate: float = 0.0
    duplicate_rate: float = 0.0
    ts_rate: Optional[float] = None

    def as_dict(self) -> dict:
        """Plain data, for a JSON response or a log line.

        ``main.py`` may wrap this, but it never has to: the dataclass is
        already serialisable field by field.
        """
        return {
            "calls": self.calls,
            "success": self.success,
            "fallback": self.fallback,
            "duplicates": self.duplicates,
            "runs": self.runs,
            "success_rate": self.success_rate,
            "fallback_rate": self.fallback_rate,
            "duplicate_rate": self.duplicate_rate,
            "ts_rate": self.ts_rate,
        }


@dataclass(frozen=True)
class SideDelta:
    """``experiment - control`` for each rate.

    Sign convention, stated because a comparator without one is unreadable:

    * a POSITIVE ``ts`` delta means protection bought correctness;
    * a POSITIVE ``success`` delta means protection served more callers;
    * a POSITIVE ``fallback`` or ``duplicates`` delta means protection made
      things *worse* on that measure.

    ``ts`` is ``0.0`` when either arm has no verdict, because "we do not know"
    is not evidence of a change.
    """

    success: float = 0.0
    fallback: float = 0.0
    duplicates: float = 0.0
    ts: float = 0.0

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "fallback": self.fallback,
            "duplicates": self.duplicates,
            "ts": self.ts,
        }


@dataclass(frozen=True)
class CompareResult:
    """The whole report: both arms plus the delta between them.

    P5's comparator panel renders this; ``main.py`` serves it. The 4-axis
    numbers are deliberately absent - they belong to ``backend/axes.py``, and
    they are computed from these raw per-side metrics rather than replacing
    them.
    """

    control: SideMetrics
    experiment: SideMetrics
    delta: SideDelta = field(default_factory=SideDelta)
    mode: str = DEFAULT_MODE

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "control": self.control.as_dict(),
            "experiment": self.experiment.as_dict(),
            "delta": self.delta.as_dict(),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float:
    """A fraction, never a percentage and never a division by zero.

    An empty arm scores ``0.0`` rather than raising or returning ``None``.
    That is the project's "fail safe, never fail open" rule applied to a rate:
    no evidence is not a success.
    """
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _final_responses(
    events: Iterable[EvidenceEvent],
) -> dict[tuple[str, int], EvidenceEvent]:
    """The LAST response-phase row of each call, keyed by ``(api, call)``.

    "Last" is what makes the rule match the scorer's: a call that served the
    customer and then failed again on the same call did not withstand anything,
    so its last row is the verdict for that call.

    Keyed on ``(api_key, call_index)`` rather than ``call_index`` because the
    bus counts calls per ``(trace_id, api_key)``. In a fan-out, weather call 1
    and geocode call 1 are different calls and must not overwrite each other.
    """
    final: dict[tuple[str, int], EvidenceEvent] = {}
    for event in events:
        if event.phase is not Phase.RECV:
            continue
        final[(event.api_key, event.call_index)] = event
    return final


def _call_keys(events: Iterable[EvidenceEvent]) -> set[tuple[str, int]]:
    """Every call the evidence mentions, response row or not.

    Derived from all rows rather than only response rows so that a call which
    was sent and never answered still appears in the denominator.
    """
    return {(e.api_key, e.call_index) for e in events}


def _duplicated_calls(events: Iterable[EvidenceEvent]) -> set[tuple[str, int]]:
    """Calls that applied their side effect more than once.

    Counted per ``(api_key, call_index)``, which is exactly P1's rule, so the
    comparator and the grader always agree. See the module docstring for the
    B1 caveat: a retried double charge lands on two call numbers and is
    invisible to this - and to P1.
    """
    counts: dict[tuple[str, int], int] = {}
    for event in events:
        if event.effect_applied:
            key = (event.api_key, event.call_index)
            counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def side_metrics(
    events: Iterable[EvidenceEvent],
    *,
    results: Optional[Sequence[ScoreResult]] = None,
) -> SideMetrics:
    """Summarise one arm from its evidence.

    Definitions, so the dashboard and the rubric cannot disagree:

    * **calls** - distinct ``(api_key, call_index)`` in the evidence.
    * **success** - calls whose LAST response row served something usable,
      i.e. ``served_from in SERVABLE_SOURCES`` (``LIVE``, ``CACHE``,
      ``DEFAULT``, ``MESSAGE``). Imported from the scorer, not restated.
    * **fallback** - calls whose LAST response row served a degraded answer:
      ``CACHE``, ``DEFAULT`` or ``MESSAGE``. ``LIVE`` is not a fallback, and a
      call that served nothing is neither.
    * **duplicates** - calls that applied an effect more than once (see the B1
      caveat in the module docstring).
    * **ts_rate** - the mean of ``result.ts`` across ``results``, or ``None``
      when no verdicts were supplied.

    ``events`` may span several APIs and several traces; the caller (main.py)
    has already filtered to this arm.
    """
    rows = list(events)
    finals = _final_responses(rows)
    calls = _call_keys(rows)

    served = {
        key
        for key, event in finals.items()
        if event.served_from in SERVABLE_SOURCES
    }
    degraded = {
        key
        for key, event in finals.items()
        if event.served_from in (ServedFrom.CACHE, ServedFrom.DEFAULT,
                                 ServedFrom.MESSAGE)
    }
    duplicated = _duplicated_calls(rows)

    verdicts = list(results) if results else []
    ts_rate = (
        _rate(sum(1 for r in verdicts if r.ts), len(verdicts))
        if verdicts
        else None
    )

    total = len(calls)
    return SideMetrics(
        calls=total,
        success=len(served),
        fallback=len(degraded),
        duplicates=len(duplicated),
        runs=len(verdicts),
        success_rate=_rate(len(served), total),
        fallback_rate=_rate(len(degraded), total),
        duplicate_rate=_rate(len(duplicated), total),
        ts_rate=ts_rate,
    )


def side_delta(control: SideMetrics, experiment: SideMetrics) -> SideDelta:
    """``experiment - control`` for each of the four rates.

    ``ts`` is ``0.0`` when either arm supplied no verdicts: an absent number
    is not a change of zero, and presenting it as one would let a missing
    comparison look like a neutral result.
    """
    if control.ts_rate is None or experiment.ts_rate is None:
        ts = 0.0
    else:
        ts = experiment.ts_rate - control.ts_rate

    return SideDelta(
        success=experiment.success_rate - control.success_rate,
        fallback=experiment.fallback_rate - control.fallback_rate,
        duplicates=experiment.duplicate_rate - control.duplicate_rate,
        ts=ts,
    )


def compare_sides(
    control_events: Iterable[EvidenceEvent],
    experiment_events: Iterable[EvidenceEvent],
    *,
    control_results: Optional[Sequence[ScoreResult]] = None,
    experiment_results: Optional[Sequence[ScoreResult]] = None,
    mode: str = DEFAULT_MODE,
) -> CompareResult:
    """Compare the protected arm against the unprotected one.

    The single call P4 needs. Pure, so it can be tested against stub evidence
    with no server, no database and no network.

    Args:
        control_events: evidence from the unprotected arm.
        experiment_events: evidence from the protected arm.
        control_results: verdicts for the control arm, if they were graded.
        experiment_results: verdicts for the experiment arm.
        mode: the arm the request was routed to, for echoing back to the
            dashboard. Use :func:`resolve_mode` to normalise caller input
            first.

    Returns:
        A :class:`CompareResult` holding both arms and the delta. No 4-axis
        numbers - those are ``backend/axes.py``'s job, computed *from* these
        raw metrics.
    """
    control = side_metrics(control_events, results=control_results)
    experiment = side_metrics(experiment_events, results=experiment_results)
    return CompareResult(
        control=control,
        experiment=experiment,
        delta=side_delta(control, experiment),
        mode=mode,
    )


__all__ = [
    "MODES",
    "DEFAULT_MODE",
    "CompareResult",
    "SideDelta",
    "SideMetrics",
    "compare_sides",
    "resolve_mode",
    "side_delta",
    "side_metrics",
]
