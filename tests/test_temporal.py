"""
Acceptance tests for JIZO Part 1 - the contracts, evidence bus and guards.

This file covers MY slice of P1 (Pushkar):
  * backend/schemas.py  - ApiPolicy, FaultTarget, GuardAfter, EvidenceEvent,
                          DrillSpec, FiRun and their validation rules
  * backend/faults.py   - EvidenceBus, GuardEvaluator, pattern helpers

What this suite proves, in plain terms:
  * An After-guard refuses to fire until the required evidence exists.
  * It fires on exactly the k-th CALL, no earlier and no later.
  * The bus keeps concurrent traces and concurrent APIs isolated.
  * Call numbering is not row numbering, so "the 3rd call" stays honest.
  * Pattern helpers always pair a target with a matching guard.
  * Invalid input is rejected at the door rather than silently accepted.

Scoring a run (TS, CW/PS/Prem/Miss/Mult) is Riya's slice and is covered
separately in tests/test_scoring.py.

No network, no database - everything is stub evidence, so the suite runs in
milliseconds and cannot flake.
"""

from __future__ import annotations

import pytest

from backend import (
    ApiPolicy,
    BreakerState,
    DrillSpec,
    EvidenceBus,
    EvidenceEvent,
    FaultTarget,
    FaultType,
    FiRun,
    GuardAfter,
    GuardEvaluator,
    Phase,
    Pattern,
    ServedFrom,
    build_spec,
    find_fault_events,
    k_of_n_drill,
    order_sensitive_drill,
    post_effect_drill,
)

API = "weather"  # generic api key - JIZO is not weather-specific


def healthy_call(bus: EvidenceBus, trace: str, *, effect: bool = False) -> None:
    """One call that completes normally and serves live data."""
    bus.record(trace, API, Phase.SEND)
    if effect:
        bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)


# ---------------------------------------------------------------------------
# Guard mechanics - the part that makes a fault precise
# ---------------------------------------------------------------------------


def test_guard_holds_until_after_evidence_exists() -> None:
    """Before POST_EFFECT is seen, the guard must refuse to fire."""
    bus = EvidenceBus()
    evaluator = GuardEvaluator(bus)
    spec = post_effect_drill("run-guard", API, k=1, n=4)

    bus.record("run-guard", API, Phase.SEND)
    decision = evaluator.should_fire("run-guard", spec.target, spec.guard)
    assert decision.should_fire is False
    assert "waiting for guard evidence" in decision.reason

    # Now the commit happens, so the dangerous window is genuinely open.
    bus.record("run-guard", API, Phase.POST_EFFECT, effect_applied=True)
    decision = evaluator.should_fire("run-guard", spec.target, spec.guard)
    assert decision.should_fire is True, decision.reason


def test_guard_fires_only_on_exact_k() -> None:
    """Earlier calls untouched; exactly k fires; later calls refused.

    This is the core claim of the whole project: the drill hits the intended
    call and nothing else.
    """
    bus = EvidenceBus()
    evaluator = GuardEvaluator(bus)
    trace = "run-guard-k"
    spec = k_of_n_drill(trace, API, k=3, n=4)

    fired_at: list[int] = []
    for _ in range(4):
        bus.record(trace, API, Phase.SEND)
        decision = evaluator.should_fire(trace, spec.target, spec.guard)
        if decision.should_fire:
            fired_at.append(decision.occurrence)
        bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    assert fired_at == [3], f"fault must fire only on call 3, fired on {fired_at}"


def test_guard_refuses_after_target_window_passed() -> None:
    """Once we are past k, firing would hit a healthy call - so refuse."""
    bus = EvidenceBus()
    evaluator = GuardEvaluator(bus)
    trace = "run-guard-past"
    spec = k_of_n_drill(trace, API, k=1, n=4)

    for _ in range(2):
        bus.record(trace, API, Phase.SEND)
        bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

    decision = evaluator.should_fire(trace, spec.target, spec.guard)
    assert decision.should_fire is False
    assert "already passed" in decision.reason


def test_guard_never_fires_before_any_call_starts() -> None:
    """With no SEND logged, the guard holds rather than firing on nothing."""
    bus = EvidenceBus()
    evaluator = GuardEvaluator(bus)
    spec = order_sensitive_drill("run-nocall", API, k=1, n=4)

    decision = evaluator.should_fire("run-nocall", spec.target, spec.guard)
    assert decision.should_fire is False


def test_guard_satisfied_reports_evidence_count() -> None:
    """guard_satisfied() must reflect how much evidence actually exists."""
    bus = EvidenceBus()
    evaluator = GuardEvaluator(bus)
    guard = GuardAfter(api_key=API, phase=Phase.SEND, min_count=2)
    trace = "run-count"

    assert evaluator.guard_satisfied(trace, guard) is False
    bus.record(trace, API, Phase.SEND)
    assert evaluator.guard_satisfied(trace, guard) is False
    bus.record(trace, API, Phase.SEND)
    assert evaluator.guard_satisfied(trace, guard) is True


def test_guard_requires_the_guard_evidence_not_just_any_evidence() -> None:
    """Evidence on a different phase or a different api must not satisfy it."""
    bus = EvidenceBus()
    evaluator = GuardEvaluator(bus)
    guard = GuardAfter(api_key=API, phase=Phase.POST_EFFECT, min_count=1)
    trace = "run-wrong-evidence"

    bus.record(trace, API, Phase.SEND)          # wrong phase
    bus.record(trace, "other-api", Phase.POST_EFFECT, effect_applied=True)  # wrong api
    assert evaluator.guard_satisfied(trace, guard) is False

    bus.record(trace, API, Phase.POST_EFFECT, effect_applied=True)  # correct
    assert evaluator.guard_satisfied(trace, guard) is True


def test_guard_never_opens_its_own_window() -> None:
    """A guard must only ever tighten; it cannot grant itself permission."""
    bus = EvidenceBus()
    evaluator = GuardEvaluator(bus)
    trace = "run-tighten"
    loose = GuardAfter(api_key=API, phase=Phase.SEND, min_count=1)
    tight = GuardAfter(api_key=API, phase=Phase.SEND, min_count=5)

    bus.record(trace, API, Phase.SEND)
    assert evaluator.guard_satisfied(trace, loose) is True
    assert evaluator.guard_satisfied(trace, tight) is False


# ---------------------------------------------------------------------------
# Evidence bus - bookkeeping correctness
# ---------------------------------------------------------------------------


def test_occurrence_counter_is_per_api_and_phase() -> None:
    """Counters are scoped, so one API's calls never inflate another's k."""
    bus = EvidenceBus()
    bus.record("t1", "alpha", Phase.SEND)
    bus.record("t1", "alpha", Phase.SEND)
    bus.record("t1", "beta", Phase.SEND)

    assert bus.occurrence_count("t1", "alpha", Phase.SEND) == 2
    assert bus.occurrence_count("t1", "beta", Phase.SEND) == 1
    assert bus.occurrence_count("t1", "alpha", Phase.RECV) == 0


def test_call_index_is_not_row_count() -> None:
    """One call logging attempt + fallback still counts as ONE call.

    Regression: k in "k-of-n" was read off a row counter, so any call with
    two RECV rows silently shifted every later call number.
    """
    bus = EvidenceBus()
    trace = "t-calls"

    bus.record(trace, API, Phase.SEND)          # call 1
    bus.record(trace, API, Phase.RECV, fault=FaultType.HTTP_500,
               served_from=ServedFrom.NONE)     # row 2 of RECV
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.CACHE)  # row 3 of RECV
    bus.record(trace, API, Phase.SEND)          # call 2 - must still be call 2
    bus.record(trace, API, Phase.RECV, served_from=ServedFrom.LIVE)

    assert bus.call_count(trace, API) == 2
    recv_calls = [e.call_index for e in bus.phase_events(trace, API, Phase.RECV)]
    assert recv_calls == [1, 1, 2]
    assert len(bus.call_events(trace, API, 1)) == 3


def test_bus_keeps_traces_separate() -> None:
    """Two concurrent requests must not see each other's evidence."""
    bus = EvidenceBus()
    bus.record("trace-a", API, Phase.SEND)
    bus.record("trace-b", API, Phase.SEND)

    assert len(bus.events("trace-a")) == 1
    assert len(bus.events("trace-b")) == 1
    assert set(bus.traces()) == {"trace-a", "trace-b"}


def test_occurrence_count_on_unseen_key_does_not_create_it() -> None:
    """Reading an unseen counter must not insert it (no unbounded growth)."""
    bus = EvidenceBus()
    assert bus.occurrence_count("nobody", "nothing", Phase.SEND) == 0
    assert bus.call_count("nobody", "nothing") == 0
    assert bus.events("never-seen") == []


def test_reset_clears_one_trace_only() -> None:
    """reset(trace) must not disturb other in-flight requests."""
    bus = EvidenceBus()
    bus.record("keep", API, Phase.SEND)
    bus.record("drop", API, Phase.SEND)

    bus.reset("drop")

    assert len(bus.events("keep")) == 1
    assert bus.events("drop") == []
    assert bus.call_count("keep", API) == 1
    assert bus.call_count("drop", API) == 0


def test_reset_all_clears_counters_too() -> None:
    """A full reset must restart numbering, not just drop the events."""
    bus = EvidenceBus()
    bus.record("t", API, Phase.SEND)
    bus.record("t", API, Phase.SEND)
    assert bus.call_count("t", API) == 2

    bus.reset()

    assert bus.events("t") == []
    assert bus.call_count("t", API) == 0


def test_call_events_returns_one_call_in_order() -> None:
    """call_events() must gather every phase of that call, in order."""
    bus = EvidenceBus()
    trace = "t-onecall"
    healthy_call(bus, trace, effect=True)
    healthy_call(bus, trace)

    first = bus.call_events(trace, API, 1)
    assert [e.phase for e in first] == [Phase.SEND, Phase.POST_EFFECT, Phase.RECV]

    second = bus.call_events(trace, API, 2)
    assert [e.phase for e in second] == [Phase.SEND, Phase.RECV]
    assert second[-1].served_from is ServedFrom.LIVE


def test_explicit_call_index_is_respected() -> None:
    """P2 may pass call_index explicitly when it owns the numbering."""
    bus = EvidenceBus()
    event = bus.record("t", API, Phase.RECV, call_index=7)
    assert event.call_index == 7


def test_recorded_event_carries_every_field() -> None:
    """The bus must not silently drop data the scorer will need."""
    bus = EvidenceBus()
    event = bus.record(
        "t", API, Phase.POST_EFFECT,
        effect_applied=True,
        breaker_state=BreakerState.OPEN,
        latency_ms=12.5,
        leaked_raw_error=False,
        note="hello",
    )
    assert isinstance(event, EvidenceEvent)
    assert event.effect_applied is True
    assert event.breaker_state is BreakerState.OPEN
    assert event.latency_ms == 12.5
    assert event.note == "hello"


def test_find_fault_events_can_filter_by_type() -> None:
    """Riya's scorer needs to ask about one specific fault type."""
    bus = EvidenceBus()
    bus.record("t", API, Phase.RECV, fault=FaultType.HTTP_500, served_from=ServedFrom.NONE)
    bus.record("t", API, Phase.RECV, fault=FaultType.DROP_RESPONSE, served_from=ServedFrom.NONE)
    events = bus.events("t")

    assert len(find_fault_events(events)) == 2
    assert len(find_fault_events(events, FaultType.HTTP_500)) == 1


# ---------------------------------------------------------------------------
# Pattern helpers - target and guard must always be paired correctly
# ---------------------------------------------------------------------------


def test_pattern_specs_pair_target_and_guard_correctly() -> None:
    """The most common source of bogus drills is a mispaired guard."""
    pe = post_effect_drill("r1", API)
    os_ = order_sensitive_drill("r2", API)
    kn = k_of_n_drill("r3", API, k=3, n=4)

    assert pe.guard.phase is Phase.POST_EFFECT
    assert os_.guard.phase is Phase.SEND
    assert kn.target.occurrence == 3 and kn.total_occurrences == 4
    assert {pe.pattern, os_.pattern, kn.pattern} == set(Pattern)


def test_post_effect_drill_is_unsafe_to_retry_by_default() -> None:
    """The dangerous pattern must default to non-idempotent, visibly."""
    assert post_effect_drill("r", API).idempotent is False
    assert k_of_n_drill("r", API).idempotent is True


def test_build_spec_dispatches_to_the_right_helper() -> None:
    """Callers can pass a pattern name and get a correctly paired spec."""
    for pattern, expected_guard in (
        (Pattern.POST_EFFECT, Phase.POST_EFFECT),
        (Pattern.ORDER_SENSITIVE, Phase.SEND),
        (Pattern.K_OF_N, Phase.SEND),
    ):
        spec = build_spec("run", pattern, API, k=2, n=4)
        assert isinstance(spec, DrillSpec)
        assert spec.guard.phase is expected_guard
        assert spec.target.occurrence == 2
        assert spec.total_occurrences == 4


def test_build_spec_accepts_overrides() -> None:
    """Fault and idempotency overrides must reach the built spec."""
    spec = build_spec(
        "run", Pattern.K_OF_N, API, k=2, n=3,
        fault=FaultType.TIMEOUT, idempotent=False,
    )
    assert spec.fault is FaultType.TIMEOUT
    assert spec.idempotent is False


def test_drill_spec_summary_is_human_readable() -> None:
    """The summary is printed on the dashboard, so it must read sensibly."""
    text = k_of_n_drill("r", API, k=3, n=4).summary()
    assert "k_of_n" in text
    assert "3/4" in text
    assert "weather" in text


def test_guard_after_describes_itself() -> None:
    """Guard.describe() feeds logs, so pluralisation must be right."""
    assert "1 'send'" in GuardAfter(api_key=API, phase=Phase.SEND, min_count=1).describe()
    assert "2 'send'" in GuardAfter(api_key=API, phase=Phase.SEND, min_count=2).describe()


# ---------------------------------------------------------------------------
# Schema validation - reject bad input at the door
# ---------------------------------------------------------------------------


def test_guard_after_rejects_zero_min_count() -> None:
    """A guard demanding zero evidence is meaningless - reject it."""
    with pytest.raises(ValueError):
        GuardAfter(api_key=API, phase=Phase.POST_EFFECT, min_count=0)


def test_fault_target_rejects_occurrence_below_one() -> None:
    """Occurrences are 1-based; zero would silently mis-target a drill."""
    with pytest.raises(ValueError):
        FaultTarget(api_key=API, phase=Phase.RECV, occurrence=0)


def test_fault_target_rejects_blank_api_key() -> None:
    """A blank key would create a target that can never match evidence."""
    with pytest.raises(ValueError):
        FaultTarget(api_key="   ", phase=Phase.RECV)


def test_drill_spec_rejects_non_positive_total() -> None:
    with pytest.raises(ValueError):
        DrillSpec(
            run_id="r", pattern=Pattern.K_OF_N, fault=FaultType.HTTP_500,
            target=FaultTarget(api_key=API, phase=Phase.RECV, occurrence=1),
            guard=GuardAfter(api_key=API, phase=Phase.SEND),
            total_occurrences=0,
        )


def test_evidence_event_rejects_negative_latency() -> None:
    with pytest.raises(ValueError):
        EvidenceEvent(
            trace_id="t", api_key=API, phase=Phase.RECV,
            occurrence=1, call_index=1, latency_ms=-1.0,
        )


def test_unknown_enum_strings_are_rejected() -> None:
    """A typo from the FI console must fail loudly, not default silently."""
    with pytest.raises(ValueError):
        FaultTarget(api_key=API, phase="not_a_phase", occurrence=1)


# ---------------------------------------------------------------------------
# ApiPolicy - the sourced thresholds P2 will obey
# ---------------------------------------------------------------------------


def test_api_policy_defaults_match_the_sourced_tuning() -> None:
    """These numbers come from published tuning, so pin them in a test."""
    policy = ApiPolicy(api_key=API, base_url="https://example.test")
    assert policy.timeout_s == 3.0
    assert policy.max_attempts == 3
    assert policy.breaker_window == 100
    assert policy.breaker_error_threshold == 0.25
    assert policy.breaker_min_volume == 20
    assert policy.breaker_sleep_s == 10.0


def test_api_policy_is_immutable() -> None:
    """A policy must not be mutated mid-flight by accident."""
    policy = ApiPolicy(api_key=API, base_url="https://example.test")
    with pytest.raises(ValueError):
        policy.timeout_s = 99.0  # type: ignore[misc]


def test_backoff_grows_then_saturates_at_the_cap() -> None:
    """Delays must grow, and must never exceed the cap.

    Without the cap, attempt 10 would wait 0.075 * 2**10 = ~77 seconds,
    long after the customer has given up.
    """
    policy = ApiPolicy(api_key=API, base_url="https://example.test")
    first = policy.backoff_delay_s(0)
    later = policy.backoff_delay_s(3)
    assert first < later
    assert policy.backoff_delay_s(50) <= policy.backoff_max_s


def test_backoff_jitter_stays_within_bounds() -> None:
    """Jitter must stay inside [initial, initial + jitter] at attempt 0."""
    policy = ApiPolicy(api_key=API, base_url="https://example.test")
    for _ in range(50):
        delay = policy.backoff_delay_s(0)
        assert policy.backoff_initial_s <= delay <= policy.backoff_initial_s + policy.jitter_s


def test_api_policy_rejects_nonsense_values() -> None:
    with pytest.raises(ValueError):
        ApiPolicy(api_key=API, base_url="https://x.test", timeout_s=0)
    with pytest.raises(ValueError):
        ApiPolicy(api_key=API, base_url="https://x.test", max_attempts=0)
    with pytest.raises(ValueError):
        ApiPolicy(api_key=API, base_url="https://x.test", breaker_error_threshold=1.5)


# ---------------------------------------------------------------------------
# FiRun - the wire shape Riya's POST /fi/run will receive
# ---------------------------------------------------------------------------


def test_fi_run_converts_to_a_matching_spec() -> None:
    """Use to_spec() rather than hand-building, so semantics cannot drift."""
    wire = FiRun(
        run_id="t-wire",
        pattern=Pattern.K_OF_N,
        fault=FaultType.HTTP_500,
        target=FaultTarget(api_key=API, phase=Phase.RECV, occurrence=3),
        guard=GuardAfter(api_key=API, phase=Phase.SEND, min_count=1),
        total_occurrences=4,
    )
    spec = wire.to_spec()
    assert spec.run_id == "t-wire"
    assert spec.pattern is Pattern.K_OF_N
    assert spec.total_occurrences == 4
    assert spec.target.occurrence == 3
    assert spec.guard.phase is Phase.SEND


def test_fi_run_allows_single_occurrence_drills() -> None:
    """Constraints must match DrillSpec exactly (they once disagreed)."""
    wire = FiRun(
        run_id="t-single",
        pattern=Pattern.POST_EFFECT,
        fault=FaultType.DROP_RESPONSE,
        target=FaultTarget(api_key=API, phase=Phase.RECV, occurrence=1),
        guard=GuardAfter(api_key=API, phase=Phase.POST_EFFECT),
        total_occurrences=1,
    )
    assert wire.to_spec().total_occurrences == 1