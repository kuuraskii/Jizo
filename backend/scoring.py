"""
JIZO - Part 1 Temporal Success scorer.

This module turns evidence into a verdict. It answers one question:

    given a DrillSpec and the EvidenceEvents a drill actually produced,
    did the system behave correctly?

The verdict is Temporal Success (TS):

    TS = CW AND PS AND NOT Prem AND NOT Miss AND NOT Mult

    CW   correct_withstand - was the customer still served for the faulted call?
    PS   policy_success    - were the safety rules respected (no raw error leak,
                             no duplicated action, no commit on rival data)?
    Prem premature         - did the fault fire before its window opened, on the
                             wrong call, or against rival data? (pattern-specific)
    Miss missed            - did the requested fault fail to fire on call k?
    Mult duplicate         - did one single call apply its side effect twice?

Five rules this file must never break:

1. **It is pure.** `score_run(spec, events)` takes data and returns data.
   It never imports or touches `EvidenceBus`, and never reaches the network.
   That is what makes every claim reproducible on stage.

2. **Every judge scopes to `spec.target.api_key` first.** A real request fans
   out to several upstream APIs. Without scoping, another API's legitimate
   commit gets blamed on this drill. There is exactly one deliberate
   exception, the guard window: a guard names its OWN api and `FiRun` lets
   that differ from the target, so that one check reads the whole trace -
   the same rows the injector's `guard_satisfied` looks at. If the grader and
   the injector disagree about whether the window ever opened, the headline
   number cannot be defended on stage.

3. **Judges read the spec, never a literal.** The fault type comes from
   `spec.fault`, the phase from `spec.target.phase`, the targeted call from
   `spec.target.occurrence`. A drill using `DELAY` instead of `HTTP_500` must
   still be graded correctly.

4. **Calls are counted with `call_index`, not `occurrence`.** One call can log
   a failed attempt *and* a fallback, so counting rows would make "the 3rd
   call" drift.

5. **Failing safe beats failing open.** No evidence, no fault, no commit
   evidence - any of those means the run proved nothing, so it scores
   `miss`/`prem` and TS fails. Never relax that to make a demo look greener.

Adding a pattern costs two edits: one judge in `_JUDGES` below, one builder
in `faults.py`.
"""

from __future__ import annotations

from typing import Iterable, NamedTuple, Optional

from .schemas import (
    DrillOutcome,
    DrillSpec,
    EvidenceEvent,
    FaultType,
    Pattern,
    Phase,
    ScoreResult,
    ServedFrom,
)


# ---------------------------------------------------------------------------
# What counts as "the customer got an answer"
# ---------------------------------------------------------------------------


#: Anything except NONE means the caller received *something* usable.
#: NONE is the "nothing was served at all" marker.
SERVABLE_SOURCES = (
    ServedFrom.LIVE,
    ServedFrom.CACHE,
    ServedFrom.DEFAULT,
    ServedFrom.MESSAGE,
)


def _is_servable(event: EvidenceEvent) -> bool:
    """Did this row give the caller something to show?"""
    return event.served_from in SERVABLE_SOURCES


# ---------------------------------------------------------------------------
# Evidence facts shared by all three judges
# ---------------------------------------------------------------------------


class _Facts(NamedTuple):
    """Everything the judges need to know about one drilled trace.

    Computing this once keeps the judges short and, more importantly, makes
    sure all three patterns read the evidence the same way.
    """

    scoped: list[EvidenceEvent]        # rows for the drilled api only
    faults: list[EvidenceEvent]        # rows where the REQUESTED fault struck
    fired_on: list[int]                # which CALLS the fault landed on
    answered: Optional[EvidenceEvent]  # a servable row answering the faulted call
    dup_call: Optional[int]            # a call that applied its effect twice
    dup_count: int                     # how many times that call applied it
    leaked: bool                       # did a raw upstream error reach the caller?
    window_note: Optional[str]         # why the fault counts as premature, if it does

    @classmethod
    def of(cls, spec: DrillSpec, events: Iterable[EvidenceEvent]) -> "_Facts":
        """Read one `(spec, events)` pair once, the way every judge reads it."""
        # The trace is read twice over: once scoped to the drilled api, and
        # once whole, because the guard window may live on a different
        # dependency. Materialise it first - a generator would already be
        # drained by the time the unscoped guard check ran.
        timeline = list(events)
        scoped = _scope_to_api(timeline, spec)
        faults = _fault_events(scoped, spec)
        fired_on = sorted({e.call_index for e in faults})
        # CW asks about the call the drill PROMISED to break, so that is
        # `spec.target.occurrence` and not the earliest call a fault happened
        # to leak onto: leaking onto call 1 is a Prem finding, not a reason
        # to grade call 1. The "which call fired" fallback this line used to
        # carry was dead - `fired_on` is empty exactly when `faults` is, and
        # `_answered` refuses outright when no fault fired at all.
        faulted_call = spec.target.occurrence
        dup_call, dup_count = _duplicated_within_call(scoped)

        return cls(
            scoped=scoped,
            faults=faults,
            fired_on=fired_on,
            answered=_answered(scoped, spec, faulted_call, faults),
            dup_call=dup_call,
            dup_count=dup_count,
            leaked=any(e.leaked_raw_error for e in scoped),
            window_note=_fired_before_window_note(timeline, spec, faults),
        )


def _scope_to_api(events: Iterable[EvidenceEvent], spec: DrillSpec) -> list[EvidenceEvent]:
    """Keep only the rows belonging to the API this drill targets.

    Rule 2 above: a real request fans out to several APIs, and an unrelated
    API's legitimate commit must not influence this drill's verdict.
    """
    return [e for e in events if e.api_key == spec.target.api_key]


def _fault_events(
    events: Iterable[EvidenceEvent], spec: DrillSpec
) -> list[EvidenceEvent]:
    """Rows where the fault this drill asked for actually struck.

    Matched on `spec.fault`, not on "any fault": a drill only cares about its
    own fault, and a different fault type elsewhere in the trace is someone
    else's problem.

    Phase is deliberately NOT filtered here. A fault that landed on the wrong
    phase (or before the window opened) is exactly the failure we must detect.
    """
    return [e for e in events if e.fault == spec.fault]


def _duplicated_within_call(
    events: Iterable[EvidenceEvent],
) -> tuple[Optional[int], int]:
    """Find a call that applied its side effect more than once.

    Effects are counted PER CALL, not per trace: two different calls each
    committing once is completely normal traffic, not a duplicate.

    Returns `(call_index, times)` for the first offending call, or
    `(None, 0)` when every call committed at most once.
    """
    counts: dict[int, int] = {}
    for event in events:
        if event.effect_applied:
            counts[event.call_index] = counts.get(event.call_index, 0) + 1

    for call_index in sorted(counts):
        if counts[call_index] > 1:
            return call_index, counts[call_index]
    return None, 0


def _all_faults_off_phase(spec: DrillSpec, faults: list[EvidenceEvent]) -> bool:
    """Did every fault land away from `spec.target.phase`?

    When true there is no on-phase fault to anchor a timeline position, so
    `_answered` has nothing meaningful to compare against and must report no
    answer rather than pick an arbitrary anchor.
    """
    return not any(e.phase == spec.target.phase for e in faults)


def _answered(
    events: list[EvidenceEvent],
    spec: DrillSpec,
    faulted_call: int,
    faults: list[EvidenceEvent],
) -> Optional[EvidenceEvent]:
    """Was the customer served something *for the faulted call, after the fault*?

    CW is deliberately narrow, because three separate mistakes used to slip
    through here and each of them awarded a false PASS:

    * a `served_from` set on a SEND or POST_EFFECT bookkeeping row must not
      count, so only rows at `spec.target.phase` qualify;
    * an answer served BEFORE the fault says nothing about withstanding it,
      so the row must be at or after the first fault in timeline order (the
      fault row itself may be the answer, e.g. a DELAY that still delivered);
    * an answer on a *later, unrelated* call must not rescue the faulted one.
      The fallback that saves a customer is written against the call that
      failed, so the row must belong to `faulted_call` itself.

    A later call in the trace is a different customer request.
    """
    positions = {id(e): i for i, e in enumerate(events)}
    # With no fault fired there is nothing to withstand, so CW is false
    # regardless of how healthy the trace looks. Without this guard the
    # first servable row would make a faultless run look correct.
    if not faults:
        return None
    # Anchor on the first ON-phase fault. With only off-phase faults there is
    # no window to anchor to, so nothing here can prove the fault was
    # withstood.
    on_phase_faults = [e for e in faults if e.phase == spec.target.phase]
    if not on_phase_faults:
        return None
    first_fault = min(positions[id(e)] for e in on_phase_faults)

    # The call's LAST qualifying row decides. A call that served the customer
    # and then failed again did not withstand the fault, and judging the
    # healthy calls the same way (`_served_live`) keeps one rule for both.
    best: Optional[EvidenceEvent] = None
    for event in events:
        if (
            event.phase == spec.target.phase
            and event.call_index == faulted_call
            and positions[id(event)] >= first_fault
        ):
            if _is_servable(event):
                best = event
            else:
                best = None  # a later failure on the same call cancels it
    return best


def _fired_before_window_note(
    all_events: list[EvidenceEvent],
    spec: DrillSpec,
    faults: list[EvidenceEvent],
) -> Optional[str]:
    """Explain a premature fire, or return None if the timing was correct.

    Premature detection compares TIMELINE POSITIONS, not flags: is the
    earliest fault event before the earliest guard-evidence event? A fault can
    land in the same phase yet earlier in the sequence, and that still counts.

    If the guard's evidence never appeared at all, the window never opened, so
    any fault that fired was premature by definition - the injector ignored
    its own guard.

    `all_events` is the UNSCOPED trace: the guard names its own `api_key`, and
    `FiRun` lets that differ from `target.api_key`. Searching only the scoped
    rows would intersect the two and could never find the window, so a
    legitimate cross-API guard would score premature while the injector - which
    looks at the right api - had already fired.
    """
    if not faults:
        return None

    window = [
        e
        for e in all_events
        if e.api_key == spec.guard.api_key and e.phase == spec.guard.phase
    ]

    # When the guard watches POST_EFFECT, the window means "the commit
    # happened", so a row that recorded no effect must not open it - a drill
    # that never created the outcome-uncertain window it claims to test would
    # otherwise pass. Keyed on the guard's own phase rather than the pattern:
    # a cross-API guard may legitimately watch SEND, where `effect_applied` is
    # meaningless and filtering on it would make the window unreachable.
    if spec.guard.phase is Phase.POST_EFFECT:
        window = [e for e in window if e.effect_applied]

    if len(window) < spec.guard.min_count:
        return (
            f"fault fired although the guard window never opened "
            f"({spec.guard.describe()}); the drill proved nothing"
        )

    positions = {id(e): i for i, e in enumerate(all_events)}
    first_fault = min(positions[id(e)] for e in faults)
    first_window = min(positions[id(e)] for e in window)
    if first_fault < first_window:
        return (
            f"fault fired at timeline position {first_fault}, before the guard "
            f"evidence at {first_window} ({spec.guard.describe()})"
        )
    return None


def _missed_note(facts: _Facts, spec: DrillSpec) -> Optional[str]:
    """Explain a missed fault, or None when the fault fired on call k."""
    target = spec.target.occurrence
    if not facts.faults:
        return (
            f"no '{spec.fault.value}' event at all: the drill never ran, so "
            "nothing was proven"
        )
    if target not in facts.fired_on:
        return (
            f"'{spec.fault.value}' never fired on the targeted call {target} "
            f"(it fired on {facts.fired_on or 'no call'})"
        )
    return None


def _premature_notes(facts: _Facts, spec: DrillSpec) -> list[str]:
    """All reasons this fault counts as premature, as readable notes."""
    notes: list[str] = []
    wrong = [
        call_index
        for call_index in facts.fired_on
        if call_index != spec.target.occurrence
    ]
    if wrong:
        notes.append(
            f"fault leaked onto call(s) {wrong}; the drill targeted call "
            f"{spec.target.occurrence}"
        )

    # A fault that only ever landed off-phase never struck the targeted
    # window. If an on-phase fault also landed on that call, the window WAS
    # struck and an extra tagged row is not itself a failure.
    off_phase = [e for e in facts.faults if e.phase != spec.target.phase]
    if off_phase:
        on_phase_calls = {
            e.call_index for e in facts.faults if e.phase == spec.target.phase
        }
        stranded = sorted(
            {e.call_index for e in off_phase if e.call_index not in on_phase_calls}
        )
        if stranded:
            phases = sorted(
                {e.phase.value for e in off_phase if e.call_index in stranded}
            )
            notes.append(
                f"fault landed on call(s) {stranded} at phase(s) {phases} "
                f"instead of '{spec.target.phase.value}'; the targeted window "
                "was never struck there"
            )

    if facts.window_note:
        notes.append(facts.window_note)
    return notes


# ---------------------------------------------------------------------------
# Pattern judges
#
# The flag name `premature` is shared, but its MEANING is pattern-specific:
#   post_effect     - fired before the guard's evidence existed
#   order_sensitive - served or committed on the rival's data
#   k_of_n          - leaked onto a call other than k
# The dashboard label must be pattern-aware (see edgecases L-09).
# ---------------------------------------------------------------------------


def _score_post_effect(spec: DrillSpec, events: list[EvidenceEvent]) -> DrillOutcome:
    """Pattern 1 - the answer is lost AFTER the upstream committed the work.

    The dangerous one: a naive retry performs the action twice (double charge,
    double dispatch). Correct handling serves the customer from cache or a
    clear message and performs the work exactly once.

    Premature here means: the fault fired before the commit evidence existed,
    or on a call other than k. A fault with no commit evidence anywhere is
    premature too - without the commit there was no outcome-uncertain window,
    so the run proves nothing (this was a false PASS before the review).
    """
    facts = _Facts.of(spec, events)
    early_notes = _premature_notes(facts, spec)
    notes = list(early_notes)

    missed_note = _missed_note(facts, spec)
    if missed_note:
        notes.append(missed_note)

    if facts.dup_call is not None:
        notes.append(
            f"call {facts.dup_call} applied its side effect {facts.dup_count} "
            "times: the action was duplicated"
        )
    if facts.leaked:
        notes.append("a raw upstream error body was passed to the caller")
    if facts.answered is not None:
        notes.append(
            f"caller was served from '{facts.answered.served_from.value}' on "
            f"call {facts.answered.call_index}"
        )
    else:
        notes.append("the faulted call served the caller nothing")

    return DrillOutcome(
        correct_withstand=facts.answered is not None,
        policy_success=not facts.leaked and facts.dup_call is None,
        premature=bool(early_notes),
        missed=missed_note is not None,
        duplicate=facts.dup_call is not None,
        notes=notes,
    )


def _score_order_sensitive(spec: DrillSpec, events: list[EvidenceEvent]) -> DrillOutcome:
    """Pattern 2 - a rival/stale answer wins the race and arrives first.

    Correct handling: recognise the answer as stale, do NOT commit on it, and
    serve the customer something safe.

    Premature here means: committed on the rival, or handed the rival payload
    to the caller. Both are judged against the RIVAL's call boundary, so a
    legitimate commit from an earlier, unrelated call is not punished. That
    boundary check is why a correct run (which may commit later on real data)
    is not failed.
    """
    facts = _Facts.of(spec, events)
    early_notes = _premature_notes(facts, spec)
    notes = list(early_notes)

    # "The rival payload reached the caller" only means something when the
    # fault actually IS a competing answer. With any other fault on this
    # pattern (legal, and required so judges read the spec), a live row is
    # just the normal response.
    rival_served = spec.fault is FaultType.RIVAL_RESPONSE and any(
        e.served_from is ServedFrom.LIVE for e in facts.faults
    )
    committed_on_rival = _committed_after_rival(facts)
    if rival_served:
        notes.append("the rival payload was served to the caller as live data")
    if committed_on_rival:
        notes.append(
            "a side effect was committed after the rival answer arrived, "
            "i.e. on unverified data"
        )

    missed_note = _missed_note(facts, spec)
    if missed_note:
        notes.append(missed_note)

    if facts.dup_call is not None:
        notes.append(
            f"call {facts.dup_call} applied its side effect {facts.dup_count} "
            "times: the action was duplicated"
        )
    if facts.leaked:
        notes.append("a raw upstream error body was passed to the caller")
    if facts.answered is not None:
        notes.append(
            f"caller was served from '{facts.answered.served_from.value}' on "
            f"call {facts.answered.call_index}"
        )
    else:
        notes.append("the faulted call served the caller nothing")

    premature = bool(early_notes) or rival_served or committed_on_rival

    return DrillOutcome(
        # Serving or committing on rival data is not "withstanding" the fault.
        correct_withstand=facts.answered is not None
        and not rival_served
        and not committed_on_rival,
        policy_success=not facts.leaked
        and facts.dup_call is None
        and not rival_served
        and not committed_on_rival,
        premature=premature,
        missed=missed_note is not None,
        duplicate=facts.dup_call is not None,
        notes=notes,
    )


def _committed_after_rival(facts: _Facts) -> bool:
    """Did the SAME call commit after the rival answer arrived?

    Bounded by the rival's call, not by the whole timeline. A commit on a
    later call is the system legitimately acting on fresh real data, and
    punishing that fails a correct run. Only a commit on the rival's own call
    can mean the stale answer was trusted.
    """
    if not facts.faults:
        return False
    rival_call = min(e.call_index for e in facts.faults)
    positions = {id(e): i for i, e in enumerate(facts.scoped)}
    first_fault = min(positions[id(e)] for e in facts.faults)
    return any(
        e.effect_applied
        and e.call_index == rival_call
        and positions[id(e)] > first_fault
        for e in facts.scoped
    )


def _score_k_of_n(spec: DrillSpec, events: list[EvidenceEvent]) -> DrillOutcome:
    """Pattern 3 - only the k-th call out of n breaks; the rest are healthy.

    This is the precision test. CW requires ALL expected healthy calls to serve
    live data - degrading every call is not protection, it is a blackout, and
    before the review this check was computed and then ignored (a false PASS).

    Premature here means: the fault leaked onto a call other than k.
    """
    facts = _Facts.of(spec, events)
    early_notes = _premature_notes(facts, spec)
    notes = list(early_notes)

    missed_note = _missed_note(facts, spec)
    if missed_note:
        notes.append(missed_note)

    unhealthy = [call_index for call_index in _healthy_calls(facts, spec)
                 if not _served_live(facts, spec, call_index)]
    if unhealthy:
        notes.append(
            f"expected healthy call(s) {unhealthy} did not end with live data: "
            "the failure spread past the targeted call, or the call never "
            f"completed (the drill promised {spec.total_occurrences} calls)"
        )

    if facts.dup_call is not None:
        notes.append(
            f"call {facts.dup_call} applied its side effect {facts.dup_count} "
            "times: the action was duplicated"
        )
    if facts.leaked:
        notes.append("a raw upstream error body was passed to the caller")
    if facts.answered is not None:
        notes.append(
            f"the faulted call {facts.answered.call_index} was served from "
            f"'{facts.answered.served_from.value}'"
        )
    else:
        notes.append("the faulted call served the caller nothing")

    return DrillOutcome(
        correct_withstand=facts.answered is not None and not unhealthy,
        policy_success=not facts.leaked and facts.dup_call is None,
        premature=bool(early_notes),
        missed=missed_note is not None,
        duplicate=facts.dup_call is not None,
        notes=notes,
    )


def _healthy_calls(facts: _Facts, spec: DrillSpec) -> list[int]:
    """The calls that were supposed to succeed: calls 1..n except k.

    `spec.total_occurrences` is the promise the drill makes, so it is read
    here. Using only observed calls let a truncated trace pass: if the
    protection layer bailed early and the last calls never happened, nothing
    was degraded so nothing was penalised. A promised call that never arrived
    is itself the failure.

    When `n == 1` this is empty and CW rests on the faulted call alone
    (see edgecases S-12).
    """
    return [c for c in range(1, spec.total_occurrences + 1)
            if c != spec.target.occurrence]


def _served_live(facts: _Facts, spec: DrillSpec, call_index: int) -> bool:
    """Did this healthy call end with fresh, unfaulted data?

    Requires the call's LAST response-phase row to carry no fault and to have
    served LIVE. Checking the last row matters: a call that served live and
    then died on the same call is not healthy, and an `any()` match would
    call it healthy anyway.
    """
    rows = [
        e
        for e in facts.scoped
        if e.call_index == call_index and e.phase == spec.target.phase
    ]
    if not rows:
        return False
    final = rows[-1]
    return final.fault is None and final.served_from is ServedFrom.LIVE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


#: One judge per pattern. Adding a fourth pattern means adding one entry here
#: plus one builder in faults.py - nothing else in the codebase moves.
_JUDGES = {
    Pattern.POST_EFFECT: _score_post_effect,
    Pattern.ORDER_SENSITIVE: _score_order_sensitive,
    Pattern.K_OF_N: _score_k_of_n,
}


def evaluate(spec: DrillSpec, events: list[EvidenceEvent]) -> DrillOutcome:
    """Grade one drill and return the raw findings.

    This is the single source of truth for the verdict: `DrillOutcome.ts` is
    the only place the conjunction is written down, so no caller can
    re-implement it and drift.

    `events` is one trace's evidence rows, oldest first. An empty list is
    legal and fails safe (`miss=True`, `ts=False`) rather than defaulting to a
    free pass.
    """
    timeline = list(events) if events else []
    judge = _JUDGES.get(spec.pattern)
    if judge is None:  # pragma: no cover - Pattern is a closed enum today
        raise ValueError(
            f"no judge for pattern {spec.pattern!r}; known patterns: "
            f"{sorted(p.value for p in _JUDGES)}"
        )
    return judge(spec, timeline)


def score_run(spec: DrillSpec, events: list[EvidenceEvent]) -> ScoreResult:
    """Grade one drill and return the full verdict, timeline attached.

    Thin wrapper around `evaluate()` on purpose: the five flags are copied
    straight out of the `DrillOutcome` and `ts` comes from its property, so
    `score_run(...).ts` and `evaluate(...).ts` can never disagree.

    The timeline and the spec travel with the result, so a consumer (the
    dashboard, P4's comparator) can re-score or explain the run without
    keeping its own copy of either.
    """
    timeline = list(events) if events else []
    outcome = evaluate(spec, timeline)

    return ScoreResult(
        run_id=spec.run_id,
        pattern=spec.pattern,
        ts=outcome.ts,
        cw=outcome.correct_withstand,
        ps=outcome.policy_success,
        prem=outcome.premature,
        miss=outcome.missed,
        mult=outcome.duplicate,
        timeline=timeline,
        spec=spec,
        notes=list(outcome.notes),
    )


# ===========================================================================
# OPEN CONTRACT QUESTIONS - DOCUMENTATION ONLY
#
# Nothing below this line is code. The three questions below describe
# behaviour this module gets WRONG, or cannot express at all, and none of
# them has been decided. They are written down here so nobody "fixes" them
# by accident, and so the next person to hit one of these traces starts
# from a written question instead of a bug report.
#
# Each question records: what we are asking, why it matters, the concrete
# trace that exposes it, and who has to decide. The matching tests in
# tests/test_scoring.py (section `test_open_contract_questions`) pin today's
# behaviour and are marked provisional. Do NOT read them as the intended
# verdict.
# ===========================================================================


# --- B1: is a retry a new call, or a second attempt on the same call? -----
#
# QUESTION
#   When P2's proxy logs a retry, should that retry be a new `call_index`,
#   or the same call carrying a second attempt row?
#
# WHY IT MATTERS
#   `_duplicated_within_call` counts applied effects per `call_index`, and
#   `EvidenceBus.record` advances `call_index` on every SEND. A retry is
#   therefore a new call by construction, so the side effect of a retried
#   non-idempotent operation lands on two different call_index values and
#   Mult can never see it. The one flag that exists to catch the double
#   charge is structurally blind to the case it was written for.
#
# THE FAILING TRACE (post_effect_drill(k=1, n=1, idempotent=False))
#     SEND                                       -> call 1
#     POST_EFFECT effect_applied=True            -> call 1
#     RECV fault=DROP_RESPONSE served_from=NONE  -> call 1
#     RECV served_from=CACHE                      -> call 1   (customer saved)
#     SEND                                       -> call 2   <- the retry
#     POST_EFFECT effect_applied=True            -> call 2   <- work done twice
#     RECV served_from=LIVE                      -> call 2
#   Scores TS=True, mult=False today. The customer was charged twice and the
#   run is a PASS, because one effect per call is indistinguishable from
#   correct behaviour.
#
# WHO MUST DECIDE
#   P2's proxy owner (they write the evidence rows) together with P1's
#   scorer owner. Either P2 logs a retry as the same call carrying a second
#   attempt row, or the shared schema grows an explicit field - an attempt
#   number, or a stable operation id - that the scorer can group on. The
#   grader cannot decide this on its own: it has nothing in the trace that
#   links the two attempts, and guessing is worse than the current silence.


# --- B2: is `total_occurrences` a promise, or a label? --------------------
#
# QUESTION
#   Is `spec.total_occurrences` a promise the grader must enforce in all
#   three patterns, or just a label describing the drill's nominal size?
#
# WHY IT MATTERS
#   `total_occurrences` is read in exactly one place, `_healthy_calls`, and
#   only the k-of-n judge calls that. `_score_post_effect` and
#   `_score_order_sensitive` never look at it. A drill that promised four
#   calls, ran one, and stopped is graded exactly like a drill that ran all
#   four, so a protection layer that bailed out early reads as a clean pass.
#   Rule 5 says a run that proved nothing must fail; here the two judges are
#   simply not looking at the evidence of that.
#
# THE FAILING TRACES
#   post_effect_drill(k=1, n=4, idempotent=False), one call only:
#     SEND                                       -> call 1
#     POST_EFFECT effect_applied=True            -> call 1
#     RECV fault=DROP_RESPONSE served_from=NONE  -> call 1
#     RECV served_from=CACHE                      -> call 1
#   Scores TS=True with one of the four promised calls.
#
#   order_sensitive_drill(k=1, n=2), one call only:
#     SEND                                       -> call 1
#     RECV fault=RIVAL_RESPONSE served_from=NONE -> call 1
#     RECV served_from=MESSAGE                    -> call 1
#   Scores TS=True with one of the two promised calls.
#
# WHO MUST DECIDE
#   P1's scorer owner with P4's comparator/dashboard owner, because the
#   answer changes what the headline number claims to have measured. If the
#   answer is "promise", the other two judges owe the same treatment
#   `_healthy_calls` already gives k-of-n. If the answer is "label", the
#   dashboards must stop presenting n as something the run covered.


# --- B3: does `spec.idempotent` change the duplicate verdict? -------------
#
# QUESTION
#   For a non-idempotent operation, should a retry that re-applies the side
#   effect score `mult=True` even when the two attempts sit on two different
#   `call_index` values?
#
# WHY IT MATTERS
#   `spec.idempotent` is read nowhere in this module. A drill declared
#   `idempotent=False` - the flag whose entire meaning is "doing this twice
#   would cause real harm" - is graded by exactly the same duplicate rule as
#   a safe read, so PS and Mult say nothing about the case the flag exists
#   to describe. This is the decision that would catch B1's double charge.
#
# THE FAILING TRACE
#   The B1 trace, unchanged: a non-idempotent post_effect drill whose effect
#   is applied on call 1 and again on call 2. Today it scores PS=True,
#   mult=False, TS=True - the safety flag agreed with a double charge.
#
# WHO MUST DECIDE
#   P1's scorer owner with P2's proxy owner, and only after B1: deciding
#   "yes" is unimplementable while a retry is still its own `call_index`, so
#   B3 without B1 leaves a rule nobody can write.
