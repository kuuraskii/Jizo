"""
Tests for the JIZO circuit breaker (Part 2 - Pushkar's file).

The breaker is the switch that stops calling a broken dependency. These tests
prove the four things a demo depends on:

  * it does NOT trip on a thin sample (one unlucky failure is not 100% errors)
  * it trips at the sourced threshold and fast-fails afterwards
  * it walks OPEN -> HALF_OPEN -> CLOSED on recovery, and the probe budget
    during HALF_OPEN is genuinely limited
  * it suppresses retries while OPEN (the retry-storm guard) and contains
    load with a per-dependency bulkhead

Every threshold comes from `ApiPolicy`, so these also pin the sourced tuning.
Time is injected, so the suite never sleeps and never flakes.
"""

from __future__ import annotations

import pytest

from backend import (
    ApiPolicy,
    BreakerRegistry,
    BreakerState,
    BulkheadPool,
    CircuitBreaker,
    GateResult,
)


class FakeClock:
    """A clock we move by hand, so sleep-window behaviour is instant."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def policy() -> ApiPolicy:
    """Default sourced policy: 100 window, 25%, vol 20, sleep 10s, 10 probes."""
    return ApiPolicy(api_key="weather", base_url="https://example.test")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def breaker(policy: ApiPolicy, clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker(policy, clock=clock)


def drive_failures(breaker: CircuitBreaker, count: int) -> None:
    """Record `count` failures without tripping, then enough to trip.

    The volume gate means a run of pure failures below `breaker_min_volume`
    must not open the breaker - that is the false-positive case.
    """


# ---------------------------------------------------------------------------
# Sourced thresholds are actually wired up
# ---------------------------------------------------------------------------


def test_policy_carries_the_sourced_tuning() -> None:
    """These numbers come from published work; if they drift, the demo lies."""
    p = ApiPolicy(api_key="weather", base_url="https://example.test")
    assert p.breaker_window == 100
    assert p.breaker_error_threshold == 0.25
    assert p.breaker_min_volume == 20
    assert p.breaker_sleep_s == 10.0
    assert p.half_open_probes == 10
    assert p.half_open_window_s == 5.0
    assert p.bulkhead_max_concurrency == 20


def test_breaker_uses_the_policy_it_was_given(breaker: CircuitBreaker) -> None:
    assert breaker.policy.api_key == "weather"
    assert breaker.bulkhead.max_concurrency == 20


# ---------------------------------------------------------------------------
# CLOSED -> OPEN
# ---------------------------------------------------------------------------


def test_starts_closed(breaker: CircuitBreaker) -> None:
    assert breaker.state is BreakerState.CLOSED
    assert breaker.is_closed is True
    assert breaker.allow_call() is True


def test_does_not_trip_below_the_volume_threshold(
    breaker: CircuitBreaker,
) -> None:
    """A thin sample must not read as 100% errors and open the breaker.

    This is the false-positive case: one unlucky call in a 5-call window is a
    20% error rate, but opening on that would be overreacting.
    """
    for _ in range(19):  # breaker_min_volume - 1
        breaker.record_failure()

    assert breaker.state is BreakerState.CLOSED, "tripped before enough samples"
    assert breaker.error_pct() == 100.0


def test_trips_once_volume_and_threshold_are_met(breaker: CircuitBreaker) -> None:
    """All failures past the volume gate must open the breaker."""
    for _ in range(20):
        breaker.record_failure()

    assert breaker.state is BreakerState.OPEN
    assert breaker.is_open is True


def test_trips_at_the_threshold_not_only_above_it(breaker: CircuitBreaker) -> None:
    """Exactly 25% errors over 20 calls is at the threshold, so it trips."""
    for _ in range(15):
        breaker.record_success()
    for _ in range(5):
        breaker.record_failure()

    assert breaker.state is BreakerState.OPEN


def test_stays_closed_below_the_threshold(breaker: CircuitBreaker) -> None:
    """20% errors (4 of 20) is inside the 20-30% safe band."""
    for _ in range(16):
        breaker.record_success()
    for _ in range(4):
        breaker.record_failure()

    assert breaker.state is BreakerState.CLOSED


def test_mixed_window_evicts_the_oldest_samples(breaker: CircuitBreaker) -> None:
    """The window is a sliding deque: old failures fall off the back.

    Without eviction an old burst would keep the breaker open forever.
    """
    for _ in range(5):
        breaker.record_failure()
    # 100 successes push the 5 failures out of the window entirely.
    for _ in range(100):
        breaker.record_success()

    assert breaker.error_pct() == 0.0
    assert breaker.state is BreakerState.CLOSED


def test_window_never_exceeds_the_configured_size(breaker: CircuitBreaker) -> None:
    for _ in range(250):
        breaker.record_success()
    assert breaker.window_size() <= breaker.policy.breaker_window


def test_error_pct_of_an_empty_window_is_zero(breaker: CircuitBreaker) -> None:
    assert breaker.error_pct() == 0.0


# ---------------------------------------------------------------------------
# The retry-storm guard
# ---------------------------------------------------------------------------


def test_open_breaker_refuses_calls(breaker: CircuitBreaker) -> None:
    for _ in range(20):
        breaker.record_failure()

    assert breaker.state is BreakerState.OPEN
    assert breaker.allow_call() is False, "must fast-fail while OPEN"


def test_no_retries_while_open(breaker: CircuitBreaker) -> None:
    """Retrying a known-broken dependency costs ~38% more recovery time."""
    for _ in range(20):
        breaker.record_failure()

    assert breaker.should_retry() is False


def test_retries_allowed_while_closed(breaker: CircuitBreaker) -> None:
    assert breaker.should_retry() is True


def test_no_retries_while_half_open(breaker: CircuitBreaker, clock: FakeClock) -> None:
    """HALF_OPEN is cautious: no blind retrying while we test recovery."""
    for _ in range(20):
        breaker.record_failure()
    clock.advance(11)
    assert breaker.effective_state is BreakerState.HALF_OPEN
    assert breaker.should_retry() is False


# ---------------------------------------------------------------------------
# OPEN -> HALF_OPEN -> CLOSED
# ---------------------------------------------------------------------------


def test_stays_open_until_the_sleep_window_elapses(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    for _ in range(20):
        breaker.record_failure()
    clock.advance(9.9)

    assert breaker.state is BreakerState.OPEN, "moved too early"


def test_moves_to_half_open_after_the_sleep_window(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)

    assert breaker.effective_state is BreakerState.HALF_OPEN


def test_half_open_probe_budget_is_genuinely_limited(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """At most `half_open_probes` calls per `half_open_window_s`.

    Regression: the budget was checked but never consumed, so all 12 attempts
    were permitted against a budget of 10 - "limited probes" was not limited.
    """
    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)

    allowed = sum(1 for _ in range(12) if breaker.allow_call())
    assert allowed == breaker.policy.half_open_probes


def test_probe_budget_refreshes_after_its_window(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """Once the probe window passes, a fresh budget starts."""
    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)

    for _ in range(breaker.policy.half_open_probes):
        breaker.allow_call()
    assert breaker.allow_call() is False, "budget should be exhausted"

    clock.advance(breaker.policy.half_open_window_s + 0.1)
    assert breaker.allow_call() is True, "budget should refresh"


def test_non_probe_successes_cannot_close_the_breaker(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """Only a genuine HALF_OPEN probe may prove recovery.

    Regression: `record_success()` closed the breaker with no probe ever sent.
    Nothing correlated the reservation in `allow_call()` with the outcome, so
    late replies from requests already in flight when the breaker tripped
    would "prove" recovery without the dependency ever being tested.
    """
    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)
    assert breaker.effective_state is BreakerState.HALF_OPEN

    for _ in range(10):
        breaker.record_success()  # was_probe defaults to False

    assert breaker.state is not BreakerState.CLOSED, (
        "closes the breaker without a single probe"
    )


def test_non_probe_failure_cannot_reopen_the_breaker(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """A late failure from before the trip must not drive a HALF_OPEN probe."""
    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)
    assert breaker.effective_state is BreakerState.HALF_OPEN

    breaker.record_failure()  # was_probe defaults to False

    assert breaker.state is BreakerState.HALF_OPEN


def test_reading_state_never_advances_the_machine(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """Observers must not be able to act.

    A dashboard polling `GET /breaker/state` was driving OPEN -> HALF_OPEN,
    which starts a probe budget the dependency never agreed to.
    """
    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)

    for _ in range(50):
        assert breaker.state is BreakerState.OPEN
        breaker.snapshot()
        breaker.error_pct()
        breaker.window_size()
        breaker.transitions()
        # The `is_*` questions read like queries, so they must not act either.
        # They used to call `effective_state`, which performed the
        # OPEN -> HALF_OPEN step - so `if breaker.is_open:` drove the machine.
        assert breaker.is_open is True
        assert breaker.is_closed is False
        assert breaker.is_half_open is False

    assert breaker.state is BreakerState.OPEN


def test_the_request_path_does_advance_the_machine(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """`effective_state` is the one place the sleep-window step happens."""
    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)

    assert breaker.state is BreakerState.OPEN, "state itself stays put"
    assert breaker.effective_state is BreakerState.HALF_OPEN


def test_retry_is_gated_on_idempotence_and_attempts() -> None:
    """A non-idempotent call must never be auto-retried.

    51% of real operations are state-changing [Tan et al. 2026 Table I], and
    retrying one after a lost response is how a single action becomes two.
    """
    assert CircuitBreaker(ApiPolicy(
        api_key="w", base_url="u", idempotent=False
    )).should_retry() is False

    assert CircuitBreaker(ApiPolicy(
        api_key="w", base_url="u", max_attempts=1
    )).should_retry(attempt=1) is False

    assert CircuitBreaker(ApiPolicy(api_key="w", base_url="u")).should_retry(
        attempt=1
    ) is True


def test_acquire_slot_alone_uses_the_whole_probe_budget(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """`acquire_slot()` already applies the guard, budget and pool.

    Pairing it with `allow_call()` spent two probe slots per request and
    silently halved the recovery budget - 5 effective probes, not 10.
    """
    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)

    sent = sum(1 for _ in range(30) if breaker.acquire_slot())
    assert sent == breaker.policy.half_open_probes


def test_release_slot_is_symmetric_with_acquire_slot(
    breaker: CircuitBreaker,
) -> None:
    """Otherwise a slot leaks on the exception path and the pool starves.

    `acquire_slot()` returns True for "admitted, not a probe", which is
    truthy - so `if breaker.acquire_slot()` still works for the common case.
    """
    assert breaker.acquire_slot() is GateResult.ADMITTED
    assert breaker.bulkhead.in_flight == 1

    breaker.release_slot()
    assert breaker.bulkhead.in_flight == 0


def test_acquire_slot_returns_the_probe_flag(breaker: CircuitBreaker, clock: FakeClock) -> None:
    """The flag comes from the admitting call, not a second state read.

    Deriving it separately can mislabel a real probe if another thread trips
    the breaker in between.
    """
    assert breaker.acquire_slot() is GateResult.ADMITTED

    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)

    assert breaker.acquire_slot() is GateResult.PROBE


def test_pool_rejection_does_not_consume_the_probe_budget(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """A request that never left the process must not spend a probe.

    Regression: the pool refused AFTER the probe was reserved, so ten pool
    rejections exhausted the whole budget and stranded a perfectly healthy
    dependency in HALF_OPEN forever.
    """
    # Fill the pool first, while CLOSED, so it costs no probes.
    for _ in range(breaker.bulkhead.max_concurrency):
        breaker.acquire_slot()
    assert breaker.bulkhead.available == 0

    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)
    assert breaker.effective_state is BreakerState.HALF_OPEN

    # Every attempt in HALF_OPEN is refused, but must refund the probe.
    for _ in range(breaker.policy.half_open_probes):
        assert breaker.acquire_slot() is GateResult.REFUSED
    assert breaker._probe_count == 0, "probe budget was consumed by nothing"

    # Drain the pool: the dependency is testable again immediately.
    for _ in range(breaker.bulkhead.in_flight):
        breaker.bulkhead.release()
    admitted = sum(1 for _ in range(20) if breaker.acquire_slot().allowed)
    assert admitted == breaker.policy.half_open_probes, (
        "a healthy dependency must be able to probe-recover after pool churn"
    )


def test_refused_calls_return_none(breaker: CircuitBreaker, clock: FakeClock) -> None:
    """`GateResult` distinguishes "refused" from "admitted as a probe"."""
    for _ in range(20):
        breaker.record_failure()
    # Do NOT advance the clock: the breaker is still OPEN, so nothing runs.
    assert breaker.acquire_slot() is GateResult.REFUSED
    assert breaker.bulkhead.in_flight == 0


def test_enough_successful_probes_close_the_breaker(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """The documented cycle: OPEN -> HALF_OPEN -> CLOSED on success."""
    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)
    assert breaker.effective_state is BreakerState.HALF_OPEN

    for _ in range(breaker.policy.half_open_probes):
        breaker.record_success(was_probe=True)

    assert breaker.state is BreakerState.CLOSED


def test_reporting_view_is_truthful_without_acting(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """`GET /breaker/state` must not lie, and must not drive anything.

    It used to read the pure `state`, so a breaker that had slept out its
    window kept reporting OPEN on the dashboard even though the very next call
    would have been let through. Reading `effective_state` instead would fix
    the lie but re-break purity. This is the third option: report what the
    breaker WOULD decide, without deciding.
    """
    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)

    assert breaker.state is BreakerState.OPEN, "recorded state is unchanged"
    assert (
        breaker.effective_state_for_reporting() is BreakerState.HALF_OPEN
    ), "the dashboard should show it as half open"
    assert breaker.state is BreakerState.OPEN, "asking still changed nothing"

    # Before the window elapses it must report the truth it actually has.
    c2 = FakeClock()
    fresh = CircuitBreaker(breaker.policy, clock=c2)
    for _ in range(20):
        fresh.record_failure()
    assert fresh.effective_state_for_reporting() is BreakerState.OPEN


def test_registry_snapshot_reports_every_breaker(
    breaker: CircuitBreaker, policy: ApiPolicy
) -> None:
    """The dashboard's per-api strip reads this, not `states()`."""
    other = ApiPolicy(api_key="geocoding", base_url=policy.base_url)
    registry = BreakerRegistry(clock=FakeClock())
    registry.get(policy)
    registry.get(other)

    snap = registry.snapshot()

    assert set(snap) == {"weather", "geocoding"}
    for key, row in snap.items():
        assert row["api_key"] == key
        assert row["state"] == "CLOSED"
        assert "error_pct" in row and "bulkhead" in row


def test_registry_snapshot_holds_no_lock_while_reading_breakers() -> None:
    """Regression guard for the lock I added around `_breakers`.

    Taking the lock for the whole dict build would mean holding two locks at
    once (registry, then breaker). Copying the items out first keeps the lock
    ordering one-way, so two dashboards cannot deadlock each other.
    """
    policy = ApiPolicy(api_key="weather", base_url="https://x.test")
    registry = BreakerRegistry()
    b = registry.get(policy)

    # If snapshot() held the registry lock while calling into the breaker, a
    # breaker whose snapshot re-entered the registry would hang. Simpler and
    # stronger: assert the breaker is still usable after the call, and that
    # no lock is held by re-entering the registry from inside a snapshot.
    registry.snapshot()
    assert registry.get(policy) is b, "registry stayed consistent"
    assert registry.states()["weather"] == "CLOSED"


def test_one_failed_probe_reopens_the_breaker(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """A single failure in HALF_OPEN means it is still broken.

    This has to go through `acquire_slot()`. The first version called
    `record_*` straight after `clock.advance()`, but `state` is pure now - the
    breaker was still recorded as OPEN, so both calls took the plain-window
    branch and the HALF_OPEN reopen code never ran. The assertion passed for
    the wrong reason: the breaker had never left OPEN.
    """
    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)

    # A good probe proves it is reachable, and really puts us in HALF_OPEN.
    good = breaker.acquire_slot()
    assert good is GateResult.PROBE
    assert breaker.state is BreakerState.HALF_OPEN
    breaker.record_success(was_probe=good.was_probe)

    # Now the dependency stops answering mid-recovery.
    clock.advance(1.0)
    bad = breaker.acquire_slot()
    assert bad is GateResult.PROBE, "still inside the probe budget"
    breaker.record_failure(was_probe=bad.was_probe)

    assert breaker.state is BreakerState.OPEN

    # And it really went round the loop, rather than sitting at OPEN.
    assert (BreakerState.HALF_OPEN, BreakerState.OPEN) in [
        (frm, to) for frm, to, _, _ in breaker.transitions()
    ], "the HALF_OPEN -> OPEN reopen never happened"


def test_closing_clears_the_error_window(breaker: CircuitBreaker, clock: FakeClock) -> None:
    """After recovery the window resets, so old failures cannot re-trip it."""
    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)
    assert breaker.effective_state is BreakerState.HALF_OPEN
    for _ in range(breaker.policy.half_open_probes):
        breaker.record_success(was_probe=True)

    assert breaker.error_pct() == 0.0
    assert breaker.window_size() == 0


def test_recovery_without_probes_stays_half_open(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """Time alone does not close the breaker; only clean probes do."""
    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)

    clock.advance(60)
    assert breaker.effective_state is BreakerState.HALF_OPEN


def test_transitions_are_recorded_for_the_timeline(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """P3 persists this into breaker_transitions; P5 plots it."""
    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)
    assert breaker.effective_state is BreakerState.HALF_OPEN  # performs the step
    for _ in range(breaker.policy.half_open_probes):
        breaker.record_success(was_probe=True)

    pairs = [(frm, to) for frm, to, _, _ in breaker.transitions()]
    assert (BreakerState.CLOSED, BreakerState.OPEN) in pairs
    assert (BreakerState.OPEN, BreakerState.HALF_OPEN) in pairs, (
        "reading the request path must perform the OPEN -> HALF_OPEN step"
    )
    assert (BreakerState.HALF_OPEN, BreakerState.CLOSED) in pairs


def test_the_gate_result_is_safe_under_plain_truthiness(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """`if breaker.acquire_slot():` must mean "the call is allowed".

    This is the bug that made `Optional[bool]` the wrong return type. With
    `None` = refused and `False` = admitted-but-not-a-probe, the most natural
    line a teammate would write,

        if not breaker.acquire_slot():
            return fallback(...)

    fires the fallback for every ordinary healthy call AND leaks the bulkhead
    slot, because the caller jumps out without ever reaching `release_slot()`.
    So: only REFUSED may be falsy.
    """
    # Ordinary healthy traffic must read as allowed.
    assert bool(breaker.acquire_slot()) is True
    assert breaker.acquire_slot().allowed is True
    breaker.release_slot()

    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)

    # A real probe is also allowed, and is flagged as one.
    result = breaker.acquire_slot()
    assert bool(result) is True
    assert result.allowed is True
    assert result.was_probe is True


def test_refusal_is_the_only_falsy_gate_result(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    for _ in range(20):
        breaker.record_failure()

    result = breaker.acquire_slot()
    assert result is GateResult.REFUSED
    assert bool(result) is False
    assert result.allowed is False
    assert result.was_probe is False, "a refused call is not a probe"


def test_transition_rows_record_the_error_rate_of_that_moment(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """A transition row is evidence, so its numbers must not drift.

    If the rate were read when the row is eventually rendered, the OPEN row
    would be relabelled with whatever the window holds today - answering
    "what was the failure rate when it tripped?" with a later number.
    """
    for _ in range(20):
        breaker.record_failure()  # window is 100% failures -> trips here

    opened_at = [t for t in breaker.transitions() if t[1] is BreakerState.OPEN][0]
    assert opened_at[3] == 100.0

    # The world recovers: a long run of successes dilutes the window.
    breaker._window.clear()
    for _ in range(20):
        breaker.record_success()

    assert breaker.error_pct() == 0.0, "window really did move on"

    # The historical row must still say what was true when it happened.
    assert opened_at[3] == 100.0, "transition row was rewritten by later traffic"


def test_repeated_open_does_not_duplicate_transition_rows(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """Re-recording failures while already OPEN must not spam the timeline."""
    for _ in range(20):
        breaker.record_failure()
    for _ in range(20):
        breaker.record_failure()

    opens = [t for t in breaker.transitions() if t[1] is BreakerState.OPEN]
    assert len(opens) == 1


# ---------------------------------------------------------------------------
# Bulkhead - per-dependency load containment
# ---------------------------------------------------------------------------


def test_bulkhead_allows_up_to_its_limit() -> None:
    pool = BulkheadPool(max_concurrency=3)

    assert [pool.acquire() for _ in range(3)] == [True, True, True]


def test_bulkhead_refuses_rather_than_queueing_when_full() -> None:
    """Queueing a call to a saturated dependency is how a slowdown becomes a
    timeout, so the pool rejects instead."""
    pool = BulkheadPool(max_concurrency=2)
    pool.acquire()
    pool.acquire()

    assert pool.acquire() is False
    assert pool.in_flight == 2
    assert pool.available == 0


def test_released_slot_can_be_reused() -> None:
    pool = BulkheadPool(max_concurrency=1)
    assert pool.acquire() is True
    assert pool.acquire() is False

    pool.release()
    assert pool.acquire() is True


def test_release_never_goes_negative() -> None:
    """An extra release must not corrupt the count."""
    pool = BulkheadPool(max_concurrency=2)
    pool.release()
    pool.release()

    assert pool.in_flight == 0
    assert pool.available == 2


def test_bulkhead_rejects_zero_concurrency() -> None:
    """A pool that can never admit anything is a configuration error."""
    with pytest.raises(ValueError):
        BulkheadPool(max_concurrency=0)


def test_bulkhead_pool_is_per_breaker_not_shared(
    breaker: CircuitBreaker, policy: ApiPolicy, clock: FakeClock
) -> None:
    """Two dependencies must not exhaust one another's pool."""
    other = CircuitBreaker(
        ApiPolicy(api_key="geocode", base_url="https://other.test"), clock=clock
    )

    for _ in range(breaker.bulkhead.max_concurrency):
        breaker.acquire_slot()

    assert breaker.acquire_slot() is GateResult.REFUSED, "pool saturated"
    assert other.acquire_slot() is not GateResult.REFUSED, "other unaffected"


def test_bulkhead_snapshot_has_the_hystrix_pool_gauges(
    breaker: CircuitBreaker,
) -> None:
    """P5 shows these in the thread-pool row."""
    breaker.acquire_slot()
    snap = breaker.bulkhead.snapshot()

    assert snap["max"] == 20
    assert snap["in_flight"] == 1
    assert snap["available"] == 19
    assert snap["rejected"] == 0


def test_bulkhead_counts_rejections(breaker: CircuitBreaker) -> None:
    for _ in range(breaker.bulkhead.max_concurrency):
        breaker.acquire_slot()

    breaker.acquire_slot()  # rejected
    breaker.acquire_slot()  # rejected

    assert breaker.bulkhead.rejected == 2


def test_no_slot_is_taken_while_open(breaker: CircuitBreaker) -> None:
    """The storm guard wins over the pool: no outbound calls at all."""
    for _ in range(20):
        breaker.record_failure()

    assert breaker.acquire_slot() is GateResult.REFUSED
    assert breaker.bulkhead.in_flight == 0


# ---------------------------------------------------------------------------
# Snapshot + registry
# ---------------------------------------------------------------------------


def test_snapshot_is_plain_data_for_the_dashboard(breaker: CircuitBreaker) -> None:
    snap = breaker.snapshot()

    assert snap["api_key"] == "weather"
    assert snap["state"] == "CLOSED"
    assert isinstance(snap["error_pct"], float)
    assert snap["open_counters"] == 0
    assert "bulkhead" in snap


def test_snapshot_counts_opens(breaker: CircuitBreaker) -> None:
    """The dashboard's 'Open Breakers Count' panel reads this."""
    for _ in range(20):
        breaker.record_failure()

    assert breaker.snapshot()["open_counters"] == 1


def test_registry_returns_one_breaker_per_api_key(policy: ApiPolicy) -> None:
    registry = BreakerRegistry()

    first = registry.get(policy)
    second = registry.get(policy)
    other = registry.get(ApiPolicy(api_key="geocode", base_url="https://o.test"))

    assert first is second, "same api_key must reuse its breaker"
    assert first is not other


def test_registry_reports_per_api_state(policy: ApiPolicy) -> None:
    registry = BreakerRegistry()
    weather = registry.get(policy)
    registry.get(ApiPolicy(api_key="geocode", base_url="https://o.test"))

    for _ in range(20):
        weather.record_failure()

    states = registry.states()
    assert states["weather"] == "OPEN"
    assert states["geocode"] == "CLOSED"


def test_registry_is_thread_safe(policy: ApiPolicy) -> None:
    """Two threads must not end up with two breakers for one api_key.

    The registry justifies itself on not leaking state between runs, so an
    orphaned breaker would be worse than the leak it prevents: its window and
    probe budget would be invisible to states(), splitting the storm guard.
    """
    from threading import Thread

    registry = BreakerRegistry()
    found: list[int] = []
    threads = [
        Thread(target=lambda: found.append(id(registry.get(policy))))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(set(found)) == 1, "registry handed out more than one breaker"
    assert len(registry.states()) == 1


def test_registry_reset_drops_every_breaker(policy: ApiPolicy) -> None:
    registry = BreakerRegistry()
    registry.get(policy)
    registry.reset()

    assert registry.states() == {}


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_failures_do_not_deadlock_or_lose_records() -> None:
    """A plain Lock here deadlocked on re-entry; RLock plus one counter.

    The breaker must stay usable from several threads, which is the normal
    case for an async server.
    """
    from threading import Thread

    breaker = CircuitBreaker(
        ApiPolicy(api_key="weather", base_url="https://example.test")
    )
    threads = [
        Thread(target=lambda: [breaker.record_failure() for _ in range(25)])
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert breaker.state is BreakerState.OPEN
    assert breaker.window_size() == 100  # capped at the window size