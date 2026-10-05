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
    DrillOutcome,
    EvidenceBus,
    EvidenceEvent,
    FaultType,
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