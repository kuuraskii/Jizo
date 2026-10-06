"""
Tests for the JIZO Temporal Success scorer.

THIS FILE IS RIYA'S SLICE - it is the spec she implements against.

Nothing here is implemented yet. `backend/scoring.py` does not exist, so
every test below fails on import. That is deliberate: the tests define the
required behaviour, and the reviewer can check her implementation against
them without reading prose.

What she must build:

    # backend/scoring.py
    def score_run(spec: DrillSpec, events: list[EvidenceEvent]) -> ScoreResult
    def evaluate(spec: DrillSpec, events: list[EvidenceEvent]) -> DrillOutcome

The verdict rule, already defined for her on `DrillOutcome.ts`:

    TS = CW AND PS AND NOT Prem AND NOT Miss AND NOT Mult

Every expectation below was verified against a working implementation during
Part 1's design phase, so these are not guesses. Full reasoning per case is
in testcases/REVIEW_FIXES.md and testcases/cases_*.md.

Suggested first step:
    pip install pydantic pytest
    # then make the tests below pass, in the order listed
"""

from __future__ import annotations

import pytest

from backend import (
    BreakerState,
    DrillOutcome,
    DrillSpec,
    EvidenceBus,
    EvidenceEvent,
    FaultTarget,
    FaultType,
    GuardAfter,
    GuardEvaluator,
    Pattern,
    Phase,
    ScoreResult,
    ServedFrom,
    evaluate,
    k_of_n_drill,
    order_sensitive_drill,
    post_effect_drill,
    score_run,
)

API = "weather"


def healthy_call(bus: EvidenceBus, trace: str, *, effect: bool = False) -> None:
    """One call that completes normally and serves live data."""
    bus.record(trace, API, Phase.SEND)
    if effect:
        bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)


# ---------------------------------------------------------------------------
# Expected behaviour, by pattern
# ---------------------------------------------------------------------------


def test_post_effect_clean_run_scores_ts1() -> None:
    """Fault hits after commit; app serves cache; work happened once."""
    bus = EvidenceBus()
    trace = "run-pe-clean"
    spec = post_effect_drill(trace, API, k=1, n=1, idempotent=False)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)

    result = score_run(spec, bus.events(trace))
    assert isinstance(result, ScoreResult)
    assert result.ts is True, result.explain()
    assert result.mult is False


def test_post_effect_detects_duplicate_action() -> None:
    """One call applying its side effect twice must fail."""
    bus = EvidenceBus()
    trace = "run-pe-dup"
    spec = post_effect_drill(trace, API, k=1, n=1, idempotent=False)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)  # the duplicate
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert result.mult is True
    assert result.ts is False


def test_post_effect_detects_premature_fault() -> None:
    """Fault before the commit evidence is a premature run."""
    bus = EvidenceBus()
    trace = "run-pe-prem"
    spec = post_effect_drill(trace, API, k=1, n=1)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.PRE_EFFECT, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)

    result = score_run(spec, bus.events(trace))
    assert result.prem is True
    assert result.ts is False


def test_post_effect_detects_missed_fault() -> None:
    """A healthy trace proves nothing and must not pass."""
    bus = EvidenceBus()
    trace = "run-pe-miss"
    spec = post_effect_drill(trace, API, k=1, n=1)

    healthy_call(bus, trace, effect=True)

    result = score_run(spec, bus.events(trace))
    assert result.miss is True
    assert result.ts is False


def test_post_effect_fails_when_no_commit_was_evidenced() -> None:
    """No commit means the outcome-uncertain window never opened.

    Subtle: every other flag looks fine, yet nothing was proven.
    """
    bus = EvidenceBus()
    trace = "run-pe-nocommit"
    spec = post_effect_drill(trace, API, k=1, n=1)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)

    result = score_run(spec, bus.events(trace))
    assert result.ts is False


def test_post_effect_detects_fault_on_wrong_occurrence() -> None:
    """Spec targets call 2; a fault on call 1 must not score as the drill."""
    bus = EvidenceBus()
    trace = "run-pe-wrongk"
    spec = post_effect_drill(trace, API, k=2, n=2, idempotent=False)

    bus.record(trace, API, Phase.SEND)                                    # call 1
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)                              # fault on 1
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)

    bus.record(trace, API, Phase.SEND)                                    # call 2
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert result.prem is True
    assert result.ts is False


def test_correct_handling_of_500_passes() -> None:
    """A 500 handled with a fallback must PASS.

    Guards against punishing correct behaviour, which is its own scoring bug.
    """
    bus = EvidenceBus()
    trace = "run-pe-500-ok"
    spec = post_effect_drill(trace, API, k=1, n=1, fault=FaultType.HTTP_500,
                             idempotent=False)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500,
               served_from=ServedFrom.NONE, status_code=500)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE,
               leaked_raw_error=False)

    result = score_run(spec, bus.events(trace))
    assert result.ts is True, result.explain()


def test_leaked_raw_error_fails_the_drill() -> None:
    """Handing the upstream 5xx body to the caller must fail PS."""
    bus = EvidenceBus()
    trace = "run-pe-leak"
    spec = post_effect_drill(trace, API, k=1, n=1, fault=FaultType.HTTP_503,
                             idempotent=False)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_503,
               served_from=ServedFrom.MESSAGE, status_code=503,
               leaked_raw_error=True)

    result = score_run(spec, bus.events(trace))
    assert result.ps is False
    assert result.ts is False


def test_order_sensitive_clean_run_scores_ts1() -> None:
    """Rival answer discarded, nothing committed, customer informed."""
    bus = EvidenceBus()
    trace = "run-os-clean"
    spec = order_sensitive_drill(trace, API, k=1, n=1)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.RIVAL_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.MESSAGE)

    result = score_run(spec, bus.events(trace))
    assert result.prem is False
    assert result.ts is True, result.explain()


def test_order_sensitive_detects_premature_commit() -> None:
    """Committing on rival data is the exact bug this pattern exists for."""
    bus = EvidenceBus()
    trace = "run-os-prem"
    spec = order_sensitive_drill(trace, API, k=1, n=1)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.RIVAL_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert result.prem is True
    assert result.ts is False


def test_order_sensitive_detects_serving_the_rival_payload() -> None:
    """Passing the stale answer through as real data must fail.

    The canonical race bug: nothing was duplicated, yet the customer was
    shown wrong data.
    """
    bus = EvidenceBus()
    trace = "run-os-stale"
    spec = order_sensitive_drill(trace, API, k=1, n=1)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.RIVAL_RESPONSE,
               served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert result.prem is True
    assert result.ts is False


def test_order_sensitive_detects_missed_fault() -> None:
    bus = EvidenceBus()
    trace = "run-os-miss"
    spec = order_sensitive_drill(trace, API, k=1, n=1)

    healthy_call(bus, trace)

    result = score_run(spec, bus.events(trace))
    assert result.miss is True
    assert result.ts is False


def test_order_sensitive_detects_duplicate_effect() -> None:
    """Two commits inside one call, with the rival present."""
    bus = EvidenceBus()
    trace = "run-os-dup"
    spec = order_sensitive_drill(trace, API, k=1, n=1)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.RIVAL_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert result.mult is True
    assert result.ts is False


def test_k_of_n_headline_case_only_third_call_degrades() -> None:
    """THE demo case: 4 calls, only call 3 breaks, the rest serve live."""
    bus = EvidenceBus()
    trace = "run-k3-clean"
    spec = k_of_n_drill(trace, API, k=3, n=4)

    healthy_call(bus, trace)                                   # 1 - live
    healthy_call(bus, trace)                                   # 2 - live
    bus.record(trace, API, Phase.SEND)                          # 3 starts
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500,
               served_from=ServedFrom.NONE)                      # 3 breaks
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE,
               note="call 3 fell back to cache")                # 3 recovers
    healthy_call(bus, trace)                                    # 4 - live

    result = score_run(spec, bus.events(trace))
    assert result.ts is True, result.explain()

    fault_calls = [e.call_index for e in bus.events(trace) if e.fault]
    assert fault_calls == [3]


def test_k_of_n_detects_fault_on_wrong_occurrence() -> None:
    """Fault leaking onto a healthy call = imprecise injector."""
    bus = EvidenceBus()
    trace = "run-k-prem"
    spec = k_of_n_drill(trace, API, k=3, n=4)

    healthy_call(bus, trace)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500,
               served_from=ServedFrom.NONE)                      # wrongly hit call 2
    healthy_call(bus, trace)
    healthy_call(bus, trace)

    result = score_run(spec, bus.events(trace))
    assert result.prem is True
    assert result.ts is False


def test_k_of_n_detects_missed_fault() -> None:
    bus = EvidenceBus()
    trace = "run-k-miss"
    spec = k_of_n_drill(trace, API, k=3, n=4)

    for _ in range(4):
        healthy_call(bus, trace)

    result = score_run(spec, bus.events(trace))
    assert result.miss is True
    assert result.ts is False


def test_k_of_n_fails_when_a_healthy_call_also_dies() -> None:
    """The fault spreading past its target call must fail CW.

    Degrading everything is not "protection".
    """
    bus = EvidenceBus()
    trace = "run-k-spread"
    spec = k_of_n_drill(trace, API, k=3, n=4)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.NONE, status_code=503)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert result.cw is False
    assert result.ts is False


def test_k_of_n_detects_duplicate_effect() -> None:
    """A retry re-applying the effect inside the failing call must fail."""
    bus = EvidenceBus()
    trace = "run-k-dup"
    spec = k_of_n_drill(trace, API, k=3, n=4, idempotent=False)

    healthy_call(bus, trace, effect=True)
    healthy_call(bus, trace)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    healthy_call(bus, trace)

    result = score_run(spec, bus.events(trace))
    assert result.mult is True
    assert result.ts is False


def test_effects_in_separate_calls_are_not_a_duplicate() -> None:
    """Two calls each committing once is normal traffic, not Mult."""
    bus = EvidenceBus()
    trace = "run-k-nodup"
    spec = k_of_n_drill(trace, API, k=3, n=4, idempotent=False)

    healthy_call(bus, trace, effect=True)
    healthy_call(bus, trace)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)
    healthy_call(bus, trace)

    result = score_run(spec, bus.events(trace))
    assert result.mult is False
    assert result.ts is True, result.explain()


def test_unrelated_api_does_not_influence_verdict() -> None:
    """A second API's legitimate commit must not be blamed on this drill."""
    bus = EvidenceBus()
    trace = "run-multi"
    spec = post_effect_drill(trace, "payments", k=1, n=1, idempotent=False)

    bus.record(trace, "orders", Phase.SEND)
    bus.record(trace, "orders", Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, "orders", Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    bus.record(trace, "payments", Phase.SEND)
    bus.record(trace, "payments", Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, "payments", Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, "payments", Phase.RECV, served_from=ServedFrom.CACHE)

    result = score_run(spec, bus.events(trace))
    assert result.mult is False
    assert result.ts is True, result.explain()


def test_k_of_n_survives_calls_that_log_two_rows() -> None:
    """Row-heavy calls must not shift the targeted call number."""
    bus = EvidenceBus()
    trace = "t-k-rows"
    spec = k_of_n_drill(trace, API, k=2, n=3)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE)

    result = score_run(spec, bus.events(trace))
    assert result.miss is False, result.explain()
    assert result.prem is False
    assert result.ts is True, result.explain()


def test_drill_using_a_non_default_fault_is_not_scored_as_missed() -> None:
    """Judges must read spec.fault, never a hardcoded literal."""
    bus = EvidenceBus()
    trace = "t-customfault"
    spec = order_sensitive_drill(trace, API, k=1, n=1, fault=FaultType.DELAY)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DELAY,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.MESSAGE)

    result = score_run(spec, bus.events(trace))
    assert result.miss is False, result.explain()
    assert result.ts is True, result.explain()




# ---------------------------------------------------------------------------
# Regression guards - bugs found in review, each one a false PASS
#
# Every test in this section reproduces a trace that the previous scorer
# graded TS=PASS. If any of them ever passes, that bug is back.
# ---------------------------------------------------------------------------


def test_answer_served_before_the_fault_does_not_count_as_withstanding() -> None:
    """A live answer logged BEFORE the fault is not evidence of withstanding.

    Previously the check was "call_index >= faulted_call", which accepted an
    answer produced before the fault ever landed.
    """
    bus = EvidenceBus()
    trace = "reg-prefault"
    spec = post_effect_drill(trace, API, k=1, n=1, idempotent=False)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)

    result = score_run(spec, bus.events(trace))
    assert result.cw is False
    assert result.ts is False


def test_served_from_on_a_bookkeeping_row_cannot_manufacture_a_pass() -> None:
    """CW must ignore `served_from` on rows that are not the response phase.

    A `served_from=LIVE` accidentally set on a SEND or POST_EFFECT row used
    to satisfy CW outright, because the old check never filtered on phase.
    """
    bus = EvidenceBus()
    trace = "reg-bookkeeping"
    spec = post_effect_drill(trace, API, k=1, n=1, idempotent=False)

    bus.record(trace, API, Phase.SEND)
    # Bookkeeping row that wrongly claims to have served the caller.
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True,
               served_from=ServedFrom.LIVE)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.NONE)

    result = score_run(spec, bus.events(trace))
    assert result.cw is False
    assert result.ts is False


def test_healthy_later_call_cannot_answer_for_a_silent_faulted_call() -> None:
    """CW must ask about the faulted call, not just 'was anything served'.

    This was a tautology: with k < n, any live row after k satisfied the
    check, so the faulted call could be completely silent and still pass.
    """
    bus = EvidenceBus()
    trace = "reg-silent"
    spec = k_of_n_drill(trace, API, k=3, n=4)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500, served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert result.cw is False
    assert result.ts is False


def test_truncated_trace_fails_even_though_nothing_degraded() -> None:
    """A promised call that never happened is itself the failure.

    Only observed calls used to be expected, so bailing out early scored a
    perfect run. `total_occurrences` is the drill's promise and must be read.
    """
    bus = EvidenceBus()
    trace = "reg-truncated"
    spec = k_of_n_drill(trace, API, k=3, n=4)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500, served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)
    # call 4 never happens

    result = score_run(spec, bus.events(trace))
    assert result.cw is False
    assert result.ts is False
    assert any("never" in n for n in result.notes)


def test_post_effect_row_without_a_commit_does_not_open_the_window() -> None:
    """The window means "the commit happened", not merely "POST_EFFECT was logged".

    Previously a POST_EFFECT row with effect_applied=False satisfied the guard,
    so the drill never created its outcome-uncertain window yet still passed.
    """
    bus = EvidenceBus()
    trace = "reg-nocommit"
    spec = post_effect_drill(trace, API, k=1, n=1, idempotent=False)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=False)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)

    result = score_run(spec, bus.events(trace))
    assert result.prem is True
    assert result.ts is False


def test_fault_at_the_wrong_phase_is_premature() -> None:
    """A fault on the right call but the wrong phase never struck the window."""
    bus = EvidenceBus()
    trace = "reg-wrongphase"
    spec = k_of_n_drill(trace, API, k=3, n=4)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.PRE_EFFECT, fault=FaultType.HTTP_500,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert result.prem is True
    assert result.ts is False


def test_guard_on_a_different_api_does_not_open_the_window() -> None:
    """The guard names its own api_key, and FiRun allows it to differ.

    Ignoring that let an unrelated dependency's rows open the window.
    """
    bus = EvidenceBus()
    trace = "reg-guardapi"
    spec = DrillSpec(
        run_id=trace,
        pattern=Pattern.POST_EFFECT,
        fault=FaultType.DROP_RESPONSE,
        target=FaultTarget(api_key="payments", phase=Phase.RECV, occurrence=1),
        guard=GuardAfter(api_key="ledger", phase=Phase.POST_EFFECT, min_count=1),
        total_occurrences=1,
        idempotent=False,
    )

    bus.record(trace, "ledger", Phase.SEND)          # guard never satisfied
    bus.record(trace, "payments", Phase.SEND)
    bus.record(trace, "payments", Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, "payments", Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, "payments", Phase.RECV, served_from=ServedFrom.CACHE)

    result = score_run(spec, bus.events(trace))
    assert result.prem is True
    assert result.ts is False


def test_guard_on_a_different_api_opens_the_window_when_its_evidence_exists() -> None:
    """A cross-api guard is legal, so the grader must agree with the injector.

    `DrillSpec` lets `guard.api_key` differ from `target.api_key`, and the
    injector's `guard_satisfied` counts evidence on the GUARD's api. The
    window check used to search only the target-scoped rows, so it could
    never see the ledger commit: the injector fired, and the scorer answered
    "the guard window never opened" while denying evidence that was plainly
    in the trace. A grader that contradicts the injector cannot defend its
    headline number on stage.
    """
    bus = EvidenceBus()
    trace = "reg-guardapi-open"
    spec = DrillSpec(
        run_id=trace,
        pattern=Pattern.POST_EFFECT,
        fault=FaultType.DROP_RESPONSE,
        target=FaultTarget(api_key="payments", phase=Phase.RECV, occurrence=1),
        guard=GuardAfter(api_key="ledger", phase=Phase.POST_EFFECT, min_count=1),
        total_occurrences=1,
        idempotent=False,
    )

    bus.record(trace, "ledger", Phase.SEND)
    bus.record(trace, "ledger", Phase.POST_EFFECT, effect_applied=True)  # window opens
    bus.record(trace, "payments", Phase.SEND)

    # First: the injector really does fire on this trace, so the grader below
    # is not the only opinion in the room.
    decision = GuardEvaluator(bus).should_fire(trace, spec.target, spec.guard)
    assert decision.should_fire is True, decision.reason

    bus.record(trace, "payments", Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, "payments", Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, "payments", Phase.RECV, served_from=ServedFrom.CACHE)

    result = score_run(spec, bus.events(trace))
    assert result.prem is False, result.notes
    assert result.ts is True, result.explain()


def test_send_guard_window_is_not_filtered_on_a_commit_that_cannot_exist() -> None:
    """A guard watching SEND must not be judged as if it watched a commit.

    The window filter used to be keyed on the PATTERN: any post_effect drill
    demanded `effect_applied` on the guard's evidence rows. A SEND row never
    carries that flag, so a post_effect drill guarded on SEND could never
    open its window and scored premature on every trace, including perfect
    ones. The filter belongs to the guard's own phase, not to the pattern.
    """
    bus = EvidenceBus()
    trace = "reg-sendguard"
    spec = DrillSpec(
        run_id=trace,
        pattern=Pattern.POST_EFFECT,
        fault=FaultType.DROP_RESPONSE,
        target=FaultTarget(api_key=API, phase=Phase.RECV, occurrence=1),
        guard=GuardAfter(api_key=API, phase=Phase.SEND, min_count=1),
        total_occurrences=1,
        idempotent=False,
    )

    bus.record(trace, API, Phase.SEND)                               # window opens
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)

    result = score_run(spec, bus.events(trace))
    assert result.prem is False, result.notes
    assert result.ts is True, result.explain()


def test_commit_on_a_later_call_is_not_a_commit_on_rival_data() -> None:
    """Correct handling, previously failed.

    The rival arrives on call 1 and is discarded; the system then commits once
    on call 2 using fresh real data. The old check spanned the whole timeline
    and punished that, even though it is exactly right behaviour.
    """
    bus = EvidenceBus()
    trace = "reg-latercommit"
    spec = order_sensitive_drill(trace, API, k=1, n=2)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.RIVAL_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.MESSAGE)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert result.prem is False
    assert result.ts is True, result.explain()


def test_commit_on_the_rivals_own_call_is_still_caught() -> None:
    """The fix above must not weaken the check it replaced."""
    bus = EvidenceBus()
    trace = "reg-samecall"
    spec = order_sensitive_drill(trace, API, k=1, n=1)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.RIVAL_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert result.prem is True
    assert result.ts is False


def test_non_rival_fault_served_live_is_not_a_stale_payload() -> None:
    """A DELAY fault that still delivered live data is not a rival leak.

    Guards against re-introducing a hardcoded rival_response check.
    """
    bus = EvidenceBus()
    trace = "reg-delay"
    spec = order_sensitive_drill(trace, API, k=1, n=1, fault=FaultType.DELAY)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DELAY,
               served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert result.prem is False
    assert result.ts is True, result.explain()


def test_healthy_call_that_died_after_serving_live_is_unhealthy() -> None:
    """The call's LAST response row decides health, not any qualifying row."""
    bus = EvidenceBus()
    trace = "reg-diedlater"
    spec = k_of_n_drill(trace, API, k=2, n=3)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500, served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.NONE, status_code=503)

    result = score_run(spec, bus.events(trace))
    assert result.cw is False
    assert result.ts is False


def test_off_phase_row_does_not_claim_the_window_was_never_struck() -> None:
    """An extra tagged row is not a wrong-phase fire.

    The wrong-phase note used to appear whenever ANY fault row sat off the
    target phase, so a trace that logged a PRE_EFFECT row and then struck the
    targeted RECV phase anyway was told "the targeted window was never
    struck". The window WAS struck, so the note was a false accusation and
    the run failed for a fault that did not happen.
    """
    bus = EvidenceBus()
    trace = "reg-wrongphase-struck"
    spec = k_of_n_drill(trace, API, k=2, n=3)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.PRE_EFFECT, fault=FaultType.HTTP_500,
               served_from=ServedFrom.NONE)                       # tagged early
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500,
               served_from=ServedFrom.NONE)                       # the real fire
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert not any("never struck" in note for note in result.notes), result.notes
    assert result.prem is False, result.notes
    assert result.ts is True, result.explain()


def test_faulted_call_that_served_then_died_is_not_withstanding() -> None:
    """The faulted call gets the same last-row rule as a healthy call.

    `_answered` used to take the FIRST servable response row while
    `_served_live` takes the LAST, so this exact shape failed on a healthy
    call (`test_healthy_call_that_died_after_serving_live_is_unhealthy`) and
    passed on the faulted one. The call recovered from cache and then died
    again, so the customer's last word on that call was another failure.
    """
    bus = EvidenceBus()
    trace = "reg-faulteddied"
    spec = k_of_n_drill(trace, API, k=2, n=3)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.SEND)                                   # call 2 breaks
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)     # recovered
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.NONE,
               status_code=503)                                         # then died
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert result.cw is False
    assert result.ts is False


def test_no_fault_at_all_cannot_withstand_anything() -> None:
    """Pin `cw is False` when the requested fault never fired.

    Nothing in `_answered` can judge withstanding without a fault, so its
    early return is the only thing keeping a faultless trace from looking
    correct. Pin that here rather than trusting a `min()` over an empty list
    to raise, which is how the dead `fired_on` fallback got away for so long.
    """
    bus = EvidenceBus()
    trace = "reg-nofault-cw"
    spec = post_effect_drill(trace, API, k=1, n=1, idempotent=False)

    healthy_call(bus, trace, effect=True)

    result = score_run(spec, bus.events(trace))
    assert result.cw is False
    assert result.miss is True
    assert result.ts is False


def test_empty_trace_never_passes_for_any_pattern() -> None:
    """S-01 was only ever pinned for k-of-n; all three patterns must fail."""
    for spec in (
        post_effect_drill("e1", API, k=1, n=1),
        order_sensitive_drill("e2", API, k=1, n=1),
        k_of_n_drill("e3", API, k=3, n=4),
    ):
        result = score_run(spec, [])
        assert result.ts is False, spec.pattern
        assert result.miss is True, spec.pattern


def test_k_of_n_with_n_equals_one_has_no_healthy_calls() -> None:
    """S-12: with n=1 there is nothing healthy to check, so CW rests on call 1."""
    bus = EvidenceBus()
    trace = "reg-n1"
    spec = k_of_n_drill(trace, API, k=1, n=1)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500, served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)

    result = score_run(spec, bus.events(trace))
    assert result.ts is True, result.explain()
    assert result.cw is True


def test_guard_min_count_greater_than_one_is_enforced() -> None:
    """min_count is 1 in every other scored test, so this branch is untested."""
    bus = EvidenceBus()
    trace = "reg-mincount"
    spec = DrillSpec(
        run_id=trace,
        pattern=Pattern.POST_EFFECT,
        fault=FaultType.DROP_RESPONSE,
        target=FaultTarget(api_key=API, phase=Phase.RECV, occurrence=2),
        guard=GuardAfter(api_key=API, phase=Phase.POST_EFFECT, min_count=2),
        total_occurrences=2,
        idempotent=False,
    )

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE, served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)

    result = score_run(spec, bus.events(trace))
    assert result.ts is True, result.explain()

    # One commit short of what the guard demanded: the window never opened.
    short = EvidenceBus()
    short.record(trace, API, Phase.SEND)
    short.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    short.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    short.record(trace, API, Phase.SEND)
    short.record(trace, API, Phase.POST_EFFECT, effect_applied=False)
    short.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE, served_from=ServedFrom.NONE)
    short.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)

    assert score_run(spec, short.events(trace)).prem is True


def test_target_k_beyond_n_is_missed_not_passed() -> None:
    """H-02: k=5 with n=2 means the drill can never fire."""
    bus = EvidenceBus()
    trace = "reg-kgtn"
    spec = k_of_n_drill(trace, API, k=5, n=2)

    for _ in range(2):
        bus.record(trace, API, Phase.SEND)
        bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert result.miss is True
    assert result.ts is False


def test_a_foreign_fault_type_does_not_count_as_our_drill() -> None:
    """Only `spec.fault` matters; another fault in the trace is not ours."""
    bus = EvidenceBus()
    trace = "reg-foreign"
    spec = post_effect_drill(trace, API, k=1, n=1)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500, served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE, served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)

    result = score_run(spec, bus.events(trace))
    assert result.miss is False
    assert result.ts is True, result.explain()


def test_three_effects_in_one_call_reports_the_count() -> None:
    """dup_count must reflect every extra application, not just the second."""
    bus = EvidenceBus()
    trace = "reg-three"
    spec = post_effect_drill(trace, API, k=1, n=1, idempotent=False)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE, served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)

    result = score_run(spec, bus.events(trace))
    assert result.mult is True
    assert any("3 times" in n for n in result.notes), result.notes


def test_fault_on_k_and_on_a_healthy_call_is_premature_but_not_missed() -> None:
    """Leaking onto a second call is Prem only - the targeted call did fire."""
    bus = EvidenceBus()
    trace = "reg-multileak"
    spec = k_of_n_drill(trace, API, k=2, n=3)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500, served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500, served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)

    result = score_run(spec, bus.events(trace))
    assert result.prem is True
    assert result.miss is False
    assert result.ts is False


def test_breaker_state_alone_does_not_fail_a_drill() -> None:
    """The scorer ignores breaker state; pin that contract deliberately."""
    bus = EvidenceBus()
    trace = "reg-breaker"
    spec = k_of_n_drill(trace, API, k=2, n=3)

    bus.record(trace, API, Phase.SEND, breaker_state=BreakerState.CLOSED)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.SEND, breaker_state=BreakerState.HALF_OPEN)
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500, served_from=ServedFrom.NONE,
               breaker_state=BreakerState.HALF_OPEN)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)
    bus.record(trace, API, Phase.SEND, breaker_state=BreakerState.OPEN)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200,
               breaker_state=BreakerState.OPEN)

    assert score_run(spec, bus.events(trace)).ts is True


def test_non_default_target_phase_is_honoured() -> None:
    """B9's untested half: judges must read spec.target.phase, not RECV."""
    bus = EvidenceBus()
    trace = "reg-phase"
    spec = DrillSpec(
        run_id=trace,
        pattern=Pattern.K_OF_N,
        fault=FaultType.TIMEOUT,
        target=FaultTarget(api_key=API, phase=Phase.PRE_EFFECT, occurrence=2),
        guard=GuardAfter(api_key=API, phase=Phase.SEND, min_count=1),
        total_occurrences=3,
    )

    # Calls must be written in order, because call_index advances on SEND.
    # Call 1 - healthy.
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.PRE_EFFECT, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    # Call 2 - the targeted call.
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.PRE_EFFECT, fault=FaultType.TIMEOUT, served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.PRE_EFFECT, served_from=ServedFrom.DEFAULT)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.MESSAGE)

    # Call 3 - healthy.
    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.PRE_EFFECT, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert result.miss is False, result.explain()
    assert result.prem is False
    assert result.ts is True, result.explain()


def test_unrelated_api_cannot_answer_for_the_faulted_call() -> None:
    """Scoping must gate CW, not only Mult."""
    bus = EvidenceBus()
    trace = "reg-crossapi"
    spec = post_effect_drill(trace, "payments", k=1, n=1, idempotent=False)

    bus.record(trace, "orders", Phase.SEND)
    bus.record(trace, "orders", Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, "orders", Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)
    bus.record(trace, "payments", Phase.SEND)
    bus.record(trace, "payments", Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, "payments", Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)

    result = score_run(spec, bus.events(trace))
    assert result.cw is False
    assert result.ts is False


# ---------------------------------------------------------------------------
# Contract-level requirements
# ---------------------------------------------------------------------------


def test_empty_trace_never_passes() -> None:
    """No evidence at all must fail, not default to a free pass."""
    spec = k_of_n_drill("empty", API, k=3, n=4)
    result = score_run(spec, [])
    assert result.ts is False
    assert result.miss is True


def test_ts_lives_on_the_outcome_too() -> None:
    """evaluate() and score_run() must never disagree about TS."""
    bus = EvidenceBus()
    trace = "t-ts"
    spec = k_of_n_drill(trace, API, k=3, n=4)
    for _ in range(3):
        healthy_call(bus, trace)

    outcome = evaluate(spec, bus.events(trace))
    assert isinstance(outcome, DrillOutcome)
    assert outcome.ts == score_run(spec, bus.events(trace)).ts


def test_score_result_explains_itself() -> None:
    """The dashboard prints explain(), so it must be complete and readable."""
    bus = EvidenceBus()
    trace = "t-explain"
    spec = post_effect_drill(trace, API, k=1, n=1, idempotent=False)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)

    text = score_run(spec, bus.events(trace)).explain()
    for token in ("TS=", "CW=", "PS=", "Prem=", "Miss=", "Mult="):
        assert token in text


# ---------------------------------------------------------------------------
# test_open_contract_questions
#
# DOCUMENTATION, NOT SETTLED TRUTH.
#
# Every test in this section pins the behaviour the scorer has TODAY on a
# trace we believe it scores wrong. None of the three questions has been
# decided, so the expectations below are PROVISIONAL and are expected to
# flip once an answer lands. The questions themselves - what we are asking,
# why it matters, the trace, and who must decide - are written out at the
# bottom of backend/scoring.py. Read them before "fixing" anything here.
# ---------------------------------------------------------------------------


def test_b1_a_retry_logged_as_a_new_call_hides_the_double_charge() -> None:
    """PROVISIONAL - undecided (scoring.py, B1: retry accounting).

    `EvidenceBus.record` advances `call_index` on every SEND, so the retry is
    call 2 and `_duplicated_within_call` (one effect per call) reads a clean
    trace. This is the canonical double charge of the dangerous pattern and
    it currently scores TS=PASS. If B1 is decided as "a retry is a second
    attempt on the SAME call", both expectations below flip to False.
    """
    bus = EvidenceBus()
    trace = "q-b1-retry"
    spec = post_effect_drill(trace, API, k=1, n=1, idempotent=False)

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)
    bus.record(trace, API, Phase.SEND)                             # the retry
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)  # charged twice
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    # The premise of the whole question: the retry was logged as a new call.
    assert [e.call_index for e in bus.events(trace)] == [1, 1, 1, 1, 2, 2, 2]

    result = score_run(spec, bus.events(trace))
    assert result.mult is False   # provisional: the double charge is missed
    assert result.ts is True      # provisional: and the run still passes


def test_b2_total_occurrences_is_only_enforced_in_k_of_n() -> None:
    """PROVISIONAL - undecided (scoring.py, B2: promise, or just a label?).

    `total_occurrences` is read only by `_healthy_calls`, which only the
    k-of-n judge calls, so a post_effect drill that promised four calls and
    ran one scores exactly like a drill that ran all four. If the answer is
    "promise", the grader owes these two judges the same treatment and every
    expectation below flips to False.
    """
    bus = EvidenceBus()
    trace = "q-b2-post"
    spec = post_effect_drill(trace, API, k=1, n=4, idempotent=False)

    bus.record(trace, API, Phase.SEND)                            # one of four
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)

    result = score_run(spec, bus.events(trace))
    assert result.ts is True   # provisional: three promised calls never ran

    rival_bus = EvidenceBus()
    rival_trace = "q-b2-rival"
    rival = order_sensitive_drill(rival_trace, API, k=1, n=2)

    rival_bus.record(rival_trace, API, Phase.SEND)                # one of two
    rival_bus.record(rival_trace, API, Phase.RECV, fault=FaultType.RIVAL_RESPONSE,
                     served_from=ServedFrom.NONE)
    rival_bus.record(rival_trace, API, Phase.RECV, served_from=ServedFrom.MESSAGE)

    assert score_run(rival, rival_bus.events(rival_trace)).ts is True  # provisional


def test_b3_idempotent_false_is_read_nowhere_by_the_scorer() -> None:
    """PROVISIONAL - undecided (scoring.py, B3: does idempotent change Mult?).

    `spec.idempotent` is not read anywhere in the scorer, so a drill that
    declares the operation unsafe - the flag that means "doing this twice
    would cause real harm" - is graded by the same duplicate rule as a safe
    read. This is B1's trace seen from the policy side: if B3 is decided as
    "a re-applied effect on a non-idempotent drill is Mult", ps and mult
    below flip, once B1 makes the two attempts visible to the grader at all.
    """
    bus = EvidenceBus()
    trace = "q-b3-idempotent"
    spec = post_effect_drill(trace, API, k=1, n=1, idempotent=False)
    assert spec.idempotent is False

    bus.record(trace, API, Phase.SEND)
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, fault=FaultType.DROP_RESPONSE,
               served_from=ServedFrom.NONE)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)
    bus.record(trace, API, Phase.SEND)                             # the retry
    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)  # unsafe re-apply
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    result = score_run(spec, bus.events(trace))
    assert result.mult is False   # provisional: PS cannot see the re-apply
    assert result.ps is True      # provisional: "policy respected" was claimed
