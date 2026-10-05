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
   commit gets blamed on this drill.

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
    Pattern,
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
    answered: Optional[EvidenceEvent]  # a servable row at/after the faulted call
    dup_call: Optional[int]            # a call that applied its effect twice
    dup_count: int                     # how many times that call applied it
    leaked: bool                       # did a raw upstream error reach the caller?
    window_note: Optional[str]         # why the fault counts as premature, if it does

    @classmethod
    def of(cls, spec: DrillSpec, events: Iterable[EvidenceEvent]) -> "_Facts":
        """Read one `(spec, events)` pair once, the way every judge reads it."""
        scoped = _scope_to_api(events, spec)
        faults = _fault_events(scoped, spec)
        fired_on = sorted({e.call_index for e in faults})
        faulted_call = min(fired_on) if fired_on else 1
        dup_call, dup_count = _duplicated_within_call(scoped)

        return cls(
            scoped=scoped,
            faults=faults,
            fired_on=fired_on,
            answered=_answered(scoped, spec, faulted_call),
            dup_call=dup_call,
            dup_count=dup_count,
            leaked=any(e.leaked_raw_error for e in scoped),
            window_note=_fired_before_window_note(scoped, spec, faults),
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


def _answered(
    events: Iterable[EvidenceEvent], spec: DrillSpec, faulted_call: int
) -> Optional[EvidenceEvent]:
    """Was the customer served something, for the faulted call and after it?

    CW is deliberately narrow. Two mistakes it must never make:

    * accept a `served_from` that happens to be set on a SEND or POST_EFFECT
      bookkeeping row - so only rows at `spec.target.phase` count;
    * accept a value served BEFORE the fault - that says nothing about
      whether the system withstood anything, so only rows at or after the
      faulted call count.
    """
    for event in events:
        if (
            event.phase == spec.target.phase
            and event.call_index >= faulted_call
            and _is_servable(event)
        ):
            return event
    return None


def _fired_before_window_note(
    events: list[EvidenceEvent], spec: DrillSpec, faults: list[EvidenceEvent]
) -> Optional[str]:
    """Explain a premature fire, or return None if the timing was correct.

    Premature detection compares TIMELINE POSITIONS, not flags: is the
    earliest fault event before the earliest guard-evidence event? A fault can
    land in the same phase yet earlier in the sequence, and that still counts.

    If the guard's evidence never appeared at all, the window never opened, so
    any fault that fired was premature by definition - the injector ignored
    its own guard.
    """
    if not faults:
        return None

    window = [e for e in events if e.phase == spec.guard.phase]
    if len(window) < spec.guard.min_count:
        return (
            f"fault fired although the guard window never opened "
            f"({spec.guard.describe()}); the drill proved nothing"
        )

    positions = {id(e): i for i, e in enumerate(events)}
    first_fault = min(positions[id(e)] for e in faults)
    first_window = min(positions[id(e)] for e in window)
    if first_fault < first_window:
        return (
            f"fault fired at timeline position {first_fault}, before the guard "
            f"evidence at {first_window} ({spec.guard.describe()})"
        )
    return None


def _fired_on_wrong_occurrence(facts: _Facts, spec: DrillSpec) -> bool:
    """Did the fault leak onto any call other than the targeted one?

    This is what makes a precise injector distinguishable from a blunt one: the
    drill must hit call k and nothing else.
    """
    target = spec.target.occurrence
    return any(call_index != target for call_index in facts.fired_on)


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

    rival_served = any(e.served_from is ServedFrom.LIVE for e in facts.faults)
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
    """Did anything commit once the rival answer had already arrived?

    Only effects positioned AFTER the earliest rival fault row count. A commit
    that happened earlier in the trace belongs to an earlier call and says
    nothing about whether the rival was trusted.
    """
    if not facts.faults:
        return False
    positions = {id(e): i for i, e in enumerate(facts.scoped)}
    first_fault = min(positions[id(e)] for e in facts.faults)
    return any(
        e.effect_applied and positions[id(e)] > first_fault for e in facts.scoped
    )


def _score_k_of_n(spec: DrillSpec, events: list[EvidenceEvent]) -> DrillOutcome:
    """Pattern 3 - only the k-th call out of n breaks; the rest are healthy.

    This is the precision test. CW requires ALL expected healthy calls to serve
    live data - degrading every call is not protection, it is a blackout, and
    before the review this check was computed and then ignored (a false PASS).

    Premature here means: the fault leaked onto a call other than k.
    """
    facts = _Facts.of(spec, events)
    notes = _premature_notes(facts, spec)

    missed_note = _missed_note(facts, spec)
    if missed_note:
        notes.append(missed_note)

    unhealthy = [call_index for call_index in _healthy_calls(facts, spec)
                 if not _served_live(facts, spec, call_index)]
    if unhealthy:
        notes.append(
            f"healthy call(s) {unhealthy} did not serve live data: the failure "
            "spread past the targeted call"
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
            f"the faulted call was served from "
            f"'{facts.answered.served_from.value}'"
        )
    else:
        notes.append("the faulted call served the caller nothing")

    return DrillOutcome(
        correct_withstand=facts.answered is not None and not unhealthy,
        policy_success=not facts.leaked and facts.dup_call is None,
        premature=bool(_premature_notes(facts, spec)),
        missed=missed_note is not None,
        duplicate=facts.dup_call is not None,
        notes=notes,
    )


def _healthy_calls(facts: _Facts, spec: DrillSpec) -> list[int]:
    """The calls that were supposed to succeed: every observed call except k.

    Only calls that actually appear in the trace are expected - a call that was
    never made cannot have degraded. When `n=1`, this is empty and CW rests on
    the faulted call alone (see edgecases S-12).
    """
    observed = {e.call_index for e in facts.scoped}
    return sorted(c for c in observed if c != spec.target.occurrence)


def _served_live(facts: _Facts, spec: DrillSpec, call_index: int) -> bool:
    """Did this healthy call answer with fresh, unfaulted data?

    Requires a row at the drill's response phase, carrying no fault, that
    served LIVE. A fallback on a call that should have been healthy is a
    failure, not a save.
    """
    return any(
        e.call_index == call_index
        and e.phase == spec.target.phase
        and e.fault is None
        and e.served_from is ServedFrom.LIVE
        for e in facts.scoped
    )


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
