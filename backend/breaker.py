"""
JIZO - Part 2 circuit breaker: the switch that stops calling a broken API.

Think of this as a doorman for one dependency. While things are healthy it
waves every caller through (CLOSED). Once too many calls fail it stops
opening the door and just serves the fallback (OPEN). After a short wait it
peeks out to see whether the dependency recovered (HALF-OPEN).

Three states [Falahah et al. 2021 Sec. 3; Luo & Girard 2026 Sec. 1.7]:

    CLOSED    -> requests flow; failures are counted in a sliding window
    OPEN      -> fast-fail immediately, serve the fallback; after the sleep
                 window, move to HALF-OPEN
    HALF_OPEN -> allow a limited number of probes; success -> CLOSED,
                 failure -> OPEN again

Where the numbers come from
---------------------------
Every threshold is read from `ApiPolicy`, which P1 froze with the sourced
values. Nothing here invents a number.

    window 100 requests        Falahah 2021 Sec. 3 parameter set
    error threshold 25%        20-30% is the best sensitivity/stability
                               band [Pasunoori 2025 Sec. 2]
    volume threshold 20        min calls before tripping [Falahah 2021 Sec. 3]
    sleep window 10 s          demo setting; production would use 1->32 s
                               exponential backoff [build doc Sec. 4.4]
    half-open 10 probes / 5 s  94.7% recovery-detection accuracy
                               [Pasunoori 2025 Sec. 2]
    bulkhead pool 20           per-dependency pool, so one slow dependency
                               cannot consume every thread [Luo & Girard
                               2026 Abstract - bulkhead + breaker synergy]

Two behaviours matter more than the thresholds:

1. **The retry-storm guard.** While the breaker is OPEN we must not retry.
   Aggressive retries during an outage increase recovery time by ~38%
   [Luo & Girard Sec. 4.4]. So `allow_call()` refuses while OPEN, and
   `should_retry()` refuses while OPEN.

2. **Failing safe.** A breaker that opens too eagerly looks broken; one that
   opens too late lets the cascade through. The `volume_threshold` exists for
   exactly this - do not trip on a thin sample where one unlucky failure
   would otherwise read as 100% errors.
"""

from __future__ import annotations

import time
from collections import deque
from threading import RLock
from typing import Callable, Optional

from enum import Enum

from .schemas import ApiPolicy, BreakerState


def _now() -> float:
    """Current time in seconds. Wrapped so tests can freeze or fake it."""
    return time.monotonic()


class GateResult(Enum):
    """The three answers to "may I call the dependency right now?".

    An enum rather than `Optional[bool]` because the honest answer has three
    states, and the common case is a two-way choice. Collapsing them is what
    makes a breaker quietly misbehave: a caller checking
    `if breaker.acquire_slot():` must be able to treat "allowed" as truthy.
    """

    REFUSED = "refused"
    ADMITTED = "admitted"
    PROBE = "probe"

    @property
    def allowed(self) -> bool:
        """True when the caller may proceed to call upstream."""
        return self is not GateResult.REFUSED

    @property
    def was_probe(self) -> bool:
        """Feed this straight into `record_success` / `record_failure`."""
        return self is GateResult.PROBE

    def __bool__(self) -> bool:
        """So `if not breaker.acquire_slot():` does the obvious thing."""
        return self is not GateResult.REFUSED


class BulkheadPool:
    """A per-dependency limit on how many calls may run at the same time.

    Why this exists: without it, one slow dependency occupies every worker
    and healthy dependencies starve. A pool per dependency contains the blast
    radius. Pool size comes from `ApiPolicy.bulkhead_max_concurrency`
    [Luo & Girard 2026: bulkhead + breaker gives finer fault containment].

    A non-blocking acquire: if the pool is full the call is rejected rather
    than queued, because queueing a request to a dependency that is already
    saturated is how you turn a slowdown into a timeout.
    """

    def __init__(self, max_concurrency: int) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._max = max_concurrency
        self._in_flight = 0
        self._rejected = 0
        self._lock = RLock()

    @property
    def max_concurrency(self) -> int:
        return self._max

    @property
    def in_flight(self) -> int:
        """How many calls are running right now."""
        return self._in_flight

    @property
    def available(self) -> int:
        """How many more calls could start without exceeding the limit."""
        return max(self._max - self._in_flight, 0)

    def acquire(self) -> bool:
        """Take a slot. Returns False when the pool is full (never blocks)."""
        with self._lock:
            if self._in_flight >= self._max:
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        """Give a slot back. Call this in a `finally` so it always happens."""
        with self._lock:
            self._in_flight = max(self._in_flight - 1, 0)

    def snapshot(self) -> dict[str, int]:
        """Pool gauges for the dashboard (mirrors the Hystrix thread-pool row)."""
        return {
            "max": self._max,
            "in_flight": self._in_flight,
            "available": self.available,
            "rejected": self.rejected,
        }

    @property
    def rejected(self) -> int:
        """How many calls were turned away because the pool was full."""
        return self._rejected

    def note_rejection(self) -> None:
        """Record that a call was refused. Public so the breaker can call it."""
        with self._lock:
            self._rejected += 1


class CircuitBreaker:
    """The CLOSED / OPEN / HALF_OPEN state machine for one dependency.

    Usage from the proxy:

        probe = breaker.acquire_slot()
        if probe is None:
            return fallback(...)          # fast-fail, do not even try
        try:
            result = await call()
            breaker.record_success(was_probe=probe)
            return result
        except Exception:
            breaker.record_failure(was_probe=probe)
            if breaker.should_retry():
                ...                       # back off and try again
        finally:
            breaker.release_slot()

    Two things here are load-bearing, not decoration:

    * `acquire_slot()` is the ONE gate. Do not also call `allow_call()` - that
      spends a second probe slot for the same request and quietly halves the
      recovery budget.
    * `was_probe` must be whatever `acquire_slot()` returned. Only a genuine
      probe proves the dependency recovered. If you pass `False` (the default)
      for a real probe, this breaker will trip and then never close again.

    State transitions are computed from the sliding window, so the breaker
    needs no background thread.
    """

    def __init__(
        self,
        policy: ApiPolicy,
        clock: Callable[[], float] = _now,
    ) -> None:
        self.policy = policy
        self._clock = clock
        # Reentrant on purpose: `record_success`/`record_failure` read
        # `self.state` while already holding the lock, and a plain Lock would
        # deadlock on that re-entry.
        self._lock = RLock()

        self._state: BreakerState = BreakerState.CLOSED

        # Sliding window of booleans: True = failed. A deque is the right
        # shape because it is already a queue - the oldest sample falls off
        # the back once the window is full.
        self._window: deque[bool] = deque(maxlen=policy.breaker_window)

        # When the breaker last opened, so we know when the sleep window ends.
        self._opened_at: Optional[float] = None

        # HALF_OPEN probe accounting: how many probes this window, and how
        # many succeeded.
        self._probe_count = 0
        self._probe_successes = 0
        self._probe_window_start: Optional[float] = None

        self.bulkhead = BulkheadPool(policy.bulkhead_max_concurrency)
        self._transition_log: list[
            tuple[BreakerState, BreakerState, float, float]
        ] = []

    # -- reading state ----------------------------------------------------

    @property
    def state(self) -> BreakerState:
        """Current state. PURE - reading never changes the machine.

        Deliberately free of side effects. An earlier version applied the
        sleep-window transition here, which meant any *reader* - the dashboard
        polling `/breaker/state`, or the log transition helper - could drive
        the breaker into probing. Observers must not be able to act.
        """
        with self._lock:
            return self._state

    def effective_state_for_reporting(self) -> BreakerState:
        """What the breaker WOULD decide on the next request, without deciding.

        Lets `/breaker/state` be truthful about a breaker that has slept out
        its window, while leaving the state machine untouched.
        """
        with self._lock:
            if self._state is BreakerState.OPEN and self._sleep_window_elapsed():
                return BreakerState.HALF_OPEN
            return self._state

    @property
    def effective_state(self) -> BreakerState:
        """The state a caller about to make a request should reason about.

        This is where the lazy OPEN -> HALF_OPEN transition happens, and it
        is the only place. Call it from `allow_call()` / `should_retry()`, i.e.
        on the request path, never from a dashboard or a log line.
        """
        with self._lock:
            if self._state is BreakerState.OPEN and self._sleep_window_elapsed():
                self._to_half_open()
            return self._state

    # -- state questions ---------------------------------------------------
    #
    # These are deliberately PURE. They read `state`, not `effective_state`.
    #
    # That was a real bug: they used to read `effective_state`, which performs
    # the OPEN -> HALF_OPEN step. So `if breaker.is_open:` - which reads like
    # a question, not a command - moved the machine, and a dashboard polling
    # `is_half_open` armed a probe budget on a dependency nobody called.
    #
    # The request path does not need these to act: `allow_call()` and
    # `should_retry()` already consult `effective_state` themselves, so the
    # transition happens exactly where a real call is being decided.

    @property
    def is_open(self) -> bool:
        """True when the breaker is recorded as OPEN right now.

        Note this is the *recorded* state. A breaker whose sleep window has
        passed would let the next call through, but still reports
        `is_open == True` until that call actually arrives. That is the
        honest answer to "what does the breaker think right now?", and it is
        what `snapshot()` and `GET /breaker/state` show too.
        """
        return self.state is BreakerState.OPEN

    @property
    def is_closed(self) -> bool:
        return self.state is BreakerState.CLOSED

    @property
    def is_half_open(self) -> bool:
        return self.state is BreakerState.HALF_OPEN

    def error_pct(self) -> float:
        """Failure ratio across the current window, as a percentage.

        Returns 0.0 for an empty window. P3 mirrors this into
        `breaker_transitions` and P5 shows it on the dashboard.
        """
        with self._lock:
            if not self._window:
                return 0.0
            return 100.0 * sum(self._window) / len(self._window)

    def window_size(self) -> int:
        """How many samples the window currently holds."""
        with self._lock:
            return len(self._window)

    def transitions(self) -> list[
            tuple[BreakerState, BreakerState, float, float]
        ]:
        """Every state change as `(from, to, at, error_pct_at_that_moment)`.

        P3 persists this timeline. The 4th field is the failure percentage
        when the change happened, not when the row is read.
        """
        with self._lock:
            return list(self._transition_log)

    # -- the storm guard --------------------------------------------------

    def allow_call(self) -> bool:
        """May we attempt a call right now? This is the storm guard.

        Returns False while OPEN, so the caller fast-fails with the fallback
        instead of retrying into a known-broken dependency.

        While HALF_OPEN the probe budget applies: at most `half_open_probes`
        calls per `half_open_window_s`. The slot is RESERVED here, so this is
        the one place a probe is spent.

        Use this alone. Calling `acquire_slot()` as well burns two probes for
        one request, which silently halves the recovery-detection budget.
        """
        state = self.effective_state
        if state is BreakerState.OPEN:
            return False
        if state is BreakerState.HALF_OPEN:
            with self._lock:
                if not self._probe_budget_available():
                    return False
                self._probe_count += 1
                self._probe_window_start = self._clock()
                return True
        return True

    def should_retry(self, attempt: int = 1) -> bool:
        """May a failed call be retried?

        Three gates, all of which must pass:

        1. `CLOSED` only. Retrying an open dependency is the retry storm
           (+38% recovery time [Luo & Girard Sec. 4.4]); retrying during
           HALF_OPEN would defeat the probe budget we are testing with.
        2. The call must be idempotent. A non-idempotent operation may have
           committed before the response was lost, so retrying is how one
           dropped response becomes a double charge. This is the flag that
           makes the 51%-of-operations-are-state-changing finding actionable
           [Tan et al. 2026 Table I].
        3. Attempts left. `attempt` is 1-based: after attempt 1 fails,
           pass `attempt=1` to ask whether to try again.
        """
        if self.effective_state is not BreakerState.CLOSED:
            return False
        if not self.policy.idempotent:
            return False
        return attempt < self.policy.max_attempts

    def acquire_slot(self) -> GateResult:
        """The single gate: storm guard + probe budget + bulkhead in one call.

        Use THIS instead of pairing `allow_call()` with the bulkhead. Calling
        both spends two probe slots per one request.

        Returns a `GateResult`:
            `REFUSED`  - do not call upstream, serve the fallback. No probe
                         was spent.
            `ADMITTED` - call allowed, and this was ordinary traffic. Pass
                         `was_probe=False` to `record_success` / `record_failure`.
            `PROBE`    - call allowed, and this was a real HALF_OPEN probe.
                         Pass `was_probe=True` to `record_success` /
                         `record_failure`, or recovery cannot happen.

        Both allowed results are truthy and `REFUSED` is falsy, so the common
        `if not breaker.acquire_slot(): fallback()` is correct. A bare
        `Optional[bool]` cannot do this: "refused" and "admitted, not a probe"
        are both falsy in one of the two directions, and getting it backwards
        serves a fallback for a call that was made while leaking its slot.

        Returning the flag is what makes `was_probe` reliable. Deriving it
        from a second read of `effective_state()` can mislabel a real probe if
        another thread trips the breaker in between.

        Remember `release_slot()` when the request finishes, in a `finally`.
        """
        if not self.allow_call():
            return GateResult.REFUSED
        if not self.bulkhead.acquire():
            # The pool refused, so no request will be sent. Give the probe
            # slot back: ten pool rejections must not be able to exhaust the
            # whole recovery budget and strand the breaker in HALF_OPEN.
            self.bulkhead.note_rejection()
            self._refund_probe()
            return GateResult.REFUSED
        if self._state is BreakerState.HALF_OPEN:
            return GateResult.PROBE
        return GateResult.ADMITTED

    def _refund_probe(self) -> None:
        """Return a probe slot that was reserved for a request never sent."""
        with self._lock:
            if self._probe_count > 0:
                self._probe_count -= 1

    def release_slot(self) -> None:
        """Return the bulkhead slot taken by `acquire_slot()`.

        Symmetric with `acquire_slot()`, so a caller cannot leak a slot by
        forgetting which object to release.
        """
        self.bulkhead.release()

    # -- recording outcomes -----------------------------------------------

    def record_success(self, was_probe: bool = False) -> None:
        """Record a call that worked.

        Pass whatever `acquire_slot()` returned. `was_probe=True` only for a
        call admitted while the breaker was HALF_OPEN, and ONLY when that
        probe was actually sent upstream. Passing True for a call that never
        left the process would let in-flight replies from before the trip
        "prove" recovery without the dependency ever being tested.
        """
        with self._lock:
            state = self._state
            if state is BreakerState.HALF_OPEN:
                if not was_probe:
                    # A reply from a request admitted while CLOSED. It says
                    # nothing about whether the dependency recovered, so it
                    # must not close the breaker.
                    return
                self._probe_successes += 1
                # Enough clean probes and the dependency is trusted again.
                if self._probe_successes >= self.policy.half_open_probes:
                    self._to_closed()
                return
            self._window.append(False)

    def record_failure(self, was_probe: bool = False) -> None:
        """Record a call that failed. May trip the breaker.

        `was_probe` mirrors `record_success`: True only when a genuine
        HALF_OPEN probe was actually sent. A late failure from before the trip
        still counts against the window, but it cannot close or reopen the
        breaker on its own.
        """
        with self._lock:
            state = self._state
            if state is BreakerState.HALF_OPEN:
                # One failed probe is enough: the dependency is still broken.
                # A non-probe failure must not be able to reopen it.
                if was_probe:
                    self._to_open()
                return
            self._window.append(True)
            if self._should_trip():
                self._to_open()

    # -- internals -------------------------------------------------------

    def _sleep_window_elapsed(self) -> bool:
        if self._opened_at is None:
            return True
        return (self._clock() - self._opened_at) >= self.policy.breaker_sleep_s

    def _should_trip(self) -> bool:
        """Is the failure ratio bad enough to open?

        Two gates, in order:
          1. enough samples (`breaker_min_volume`) - do not trip on thin data
          2. error ratio at or above the threshold
        """
        if len(self._window) < self.policy.breaker_min_volume:
            return False
        ratio = sum(self._window) / len(self._window)
        return ratio >= self.policy.breaker_error_threshold

    def _to_open(self) -> None:
        if self._state is BreakerState.OPEN:
            return
        self._log_transition(BreakerState.OPEN)
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        self._reset_probes()

    def _to_half_open(self) -> None:
        self._log_transition(BreakerState.HALF_OPEN)
        self._state = BreakerState.HALF_OPEN
        self._reset_probes()
        self._probe_window_start = self._clock()

    def _to_closed(self) -> None:
        self._log_transition(BreakerState.CLOSED)
        self._state = BreakerState.CLOSED
        self._opened_at = None
        self._window.clear()
        self._reset_probes()

    def _reset_probes(self) -> None:
        self._probe_count = 0
        self._probe_successes = 0
        self._probe_window_start = None

    def _log_transition(self, to_state: BreakerState) -> None:
        """Record a state change, with the error rate *at that moment*.

        The rate is captured here rather than read later because a log line is
        evidence. If we computed it when the line is finally written, an old
        transition would be labelled with today's numbers - and the whole point
        of a transition row is answering "what was the failure rate when it
        opened?".
        """
        rate = (100.0 * sum(self._window) / len(self._window)) if self._window else 0.0
        self._transition_log.append((self._state, to_state, self._clock(), rate))

    def _probe_budget_available(self) -> bool:
        """Are we still inside the half-open probe budget?

        At most `half_open_probes` calls per `half_open_window_s`. Past the
        window a fresh budget starts.
        """
        if self._probe_window_start is None:
            return True
        elapsed = self._clock() - self._probe_window_start
        if elapsed >= self.policy.half_open_window_s:
            self._probe_window_start = self._clock()
            self._probe_count = 0
            return True
        return self._probe_count < self.policy.half_open_probes

    # -- introspection for the dashboard ---------------------------------

    def snapshot(self) -> dict:
        """Everything the dashboard needs, as plain data.

        Kept separate from the state machine so P5 never has to know how the
        breaker works internally.
        """
        with self._lock:
            # Deliberately reads `_state`, not `state`/`effective_state`: a
            # dashboard polling this must never advance the state machine.
            return {
                "api_key": self.policy.api_key,
                "state": self._state.value,
                "error_pct": round(
                    (100.0 * sum(self._window) / len(self._window))
                    if self._window else 0.0,
                    2,
                ),
                "window_size": len(self._window),
                "open_counters": len(
                    [t for t in self._transition_log if t[1] is BreakerState.OPEN]
                ),
                "bulkhead": self.bulkhead.snapshot(),
            }


class BreakerRegistry:
    """One breaker per `api_key`, created on demand.

    A registry rather than a global variable, so tests and short-lived
    processes do not leak state between runs - the same reason `EvidenceBus`
    is never a singleton in P1.
    """

    def __init__(self, clock: Callable[[], float] = _now) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._clock = clock
        # Un-synchronised get-then-assign could hand two callers two breakers
        # for the same api_key, and the registry would only remember one. The
        # orphan would then run its own storm guard, invisible to states().
        self._lock = RLock()

    def get(self, policy: ApiPolicy) -> CircuitBreaker:
        """Fetch the breaker for a policy, creating it on first use."""
        with self._lock:
            breaker = self._breakers.get(policy.api_key)
            if breaker is None:
                breaker = CircuitBreaker(policy, clock=self._clock)
                self._breakers[policy.api_key] = breaker
            return breaker

    def states(self) -> dict[str, str]:
        """Per-api breaker state, which is what `GET /breaker/state` returns.

        Reports the *effective* view without performing the transition: a
        breaker whose sleep window has passed would admit traffic, so showing
        OPEN forever would be a lie on a dashboard. Reading raw `_state` and
        calling `effective_state` here would make a GET request drive the state
        machine, which is exactly what the purity fix exists to prevent.
        """
        with self._lock:
            return {
                key: b.effective_state_for_reporting()
                for key, b in self._breakers.items()
            }

    def snapshot(self) -> dict[str, dict]:
        """Per-api snapshot for the dashboard's always-visible strip."""
        with self._lock:
            breakers = list(self._breakers.items())
        return {key: breaker.snapshot() for key, breaker in breakers}

    def reset(self) -> None:
        """Drop every breaker. Used between tests."""
        with self._lock:
            self._breakers.clear()