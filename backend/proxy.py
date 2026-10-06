"""
JIZO - Part 2 resilient proxy.

``resilient_get()`` is the single safe doorway application code uses to
talk to an upstream API. It wraps one ``httpx`` request with:

    * a per-attempt timeout        (ApiPolicy.timeout_s)
    * a total retry budget         (2.2 x timeout_s, see config.py)
    * bounded retries with jitter  (ApiPolicy.backoff_delay_s)
    * a circuit-breaker gate       (breaker.CircuitBreaker.acquire_slot)
    * the JIZO fallback ladder     (live -> stale cache -> default ->
                                    clear message, FallbackLadder below)
    * structured JSON logging      (logging_conf.CallLogger)
    * Part 1 evidence events       (EvidenceBus.record: SEND, POST_EFFECT,
                                    RECV)

Nothing here re-implements another part:

    * the breaker state machine lives in ``breaker.py`` - we only call
      ``acquire_slot`` / ``release_slot`` / ``record_success`` /
      ``record_failure`` / ``should_retry`` / ``state`` / ``transitions``
    * the JSON line shape lives in ``logging_conf.py`` - we only call
      ``get_logger`` / ``CallLogger`` / ``log_breaker_transition``
    * the event contract lives in ``schemas.py`` - we only ever write
      fields that ``EvidenceEvent`` already declares
    * scoring lives in ``scoring.py`` - the proxy never grades a drill

Owner: Aditi (Part 2).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

import httpx

from .breaker import BreakerRegistry, CircuitBreaker, GateResult
from .config import RETRY_BUDGET_MULTIPLIER, load_policy
from .faults import EvidenceBus, GuardEvaluator
from .logging_conf import CallLogger, get_logger, log_breaker_transition
from .schemas import (
    ApiPolicy,
    BreakerState,
    DrillSpec,
    EvidenceEvent,
    FaultType,
    Pattern,
    Phase,
    ServedFrom,
)


# ===========================================================================
# Public result shapes
# ===========================================================================

@dataclass
class FallbackValue:
    """One rung of the fallback ladder, ready to hand to the caller."""

    data: Any
    served_from: ServedFrom
    status_code: Optional[int] = None
    note: Optional[str] = None


@dataclass
class FallbackLadder:
    """JIZO's fallback order: live -> stale cache -> default -> message.

    ``resilient_get`` ALWAYS tries the live upstream first; this object
    only runs once the live call can no longer be served. Every rung is
    a callable that returns either a usable value or ``None`` (meaning
    "nothing on this rung, try the next"). The final rung is a plain
    message string that always exists, so the ladder can never
    dead-end with a raw upstream error body.

    Who supplies what, in the demo team:

        cache   - Part 3 (Aayush). A lookup into the cached rows it
                  persists; returning None means "no cached row".
        default - Part 4 (Riya). The seeded demo defaults (Delhi weather,
                  backup city geocode, ...).
        message - a short human-readable line; always exists.

    Part 4's demo shows the chosen rung on screen as ``servedFrom``.
    """

    cache: Optional[Callable[[], Any]] = None
    default: Optional[Callable[[], Any]] = None
    message: str = "service unavailable"

    def resolve(self, reason: str) -> FallbackValue:
        """Walk the ladder and return the first rung that has a value.

        A provider that wants to signal "nothing here" must return
        ``None``. Raising is treated as a real bug in that provider and
        is left to propagate - a broken cache lookup should be visible,
        not silently masked behind a generic message.
        """
        if self.cache is not None:
            cached = self.cache()
            if cached is not None:
                return FallbackValue(
                    data=cached,
                    served_from=ServedFrom.CACHE,
                    note=f"stale cache ({reason})",
                )
        if self.default is not None:
            default = self.default()
            if default is not None:
                return FallbackValue(
                    data=default,
                    served_from=ServedFrom.DEFAULT,
                    note=f"built-in default ({reason})",
                )
        return FallbackValue(
            data=None,
            served_from=ServedFrom.MESSAGE,
            note=self.message,
        )


@dataclass
class ResilientResponse:
    """The single object every caller of ``resilient_get()`` receives.

    Everything the demo harness (Part 4) needs to draw one call on screen:

        api_key         which dependency this answer came from
        trace_id        which request this call belongs to
        data            the payload - live JSON, cached value, default,
                        or None when we could only serve a message
        served_from     honest label: live / cache / default / message
        status_code     the upstream status, if any (None on a pure
                        fallback - the caller decides the HTTP status)
        breaker_state   the value for the X-Breaker-State header, or
                        None when no breaker was wired for this call
        attempts        how many HTTP attempts were actually made
        latency_ms      total wall-clock time inside resilient_get
        fault           which drill fault (if any) fired on this call
        note            short human-readable explanation
        events          every Part 1 EvidenceEvent this call wrote
    """

    api_key: str
    trace_id: str
    data: Any
    served_from: ServedFrom
    status_code: Optional[int]
    breaker_state: Optional[BreakerState]
    attempts: int
    latency_ms: float
    fault: Optional[FaultType] = None
    note: Optional[str] = None
    events: list[EvidenceEvent] = field(default_factory=list)

    def breaker_header(self) -> dict[str, str]:
        """The ``X-Breaker-State`` header Part 4 should add to its response.

        The PRD asks for this header on every result. Returning a dict
        keeps the call site tiny::

            response.headers.update(result.breaker_header())

        An empty dict is the honest answer for a call that ran with no
        breaker wired - which is exactly the unprotected control path in
        Part 4's protected-vs-control compare.
        """
        if self.breaker_state is None:
            return {}
        return {"X-Breaker-State": self.breaker_state.value}


# ===========================================================================
# Shared state: one breaker registry and one courtesy limiter per process
# ===========================================================================

_default_registry: Optional[BreakerRegistry] = None


def get_breaker_registry() -> BreakerRegistry:
    """The process-wide breaker registry (one breaker per api_key).

    Created lazily, so importing this module has no side effects. Several
    parts share it:

        * Part 4 passes ``get_breaker_registry().get(policy)`` into
          ``resilient_get(breaker=...)`` and reads
          ``get_breaker_registry().states()`` for ``GET /breaker/state``
        * Part 5's dashboard reads ``snapshot()``
        * Part 3 mirrors ``transitions()`` into its table

    Tests call :func:`reset_breaker_registry` between cases.
    """
    global _default_registry
    if _default_registry is None:
        _default_registry = BreakerRegistry()
    return _default_registry


def reset_breaker_registry() -> None:
    """Drop every breaker. For tests and short-lived processes."""
    global _default_registry
    _default_registry = None


class CourtesyLimiter:
    """A per-dependency request-rate ceiling.

    The PRD's "Guards" row asks for a polite ceiling on public/shared
    APIs: "Nominatim 1 rps + User-Agent + cache". This is NOT the
    breaker and NOT the bulkhead - it only spaces requests out so we do
    not get ourselves rate-limited by an API we do not own.

    Deliberately simple: remember the last send time per api_key, and if
    the next request arrives too soon, sleep the remainder. One
    ``asyncio.Lock`` per api_key keeps concurrent callers honest without
    busy-waiting. It is a no-op for any policy whose ``courtesy_rps`` is
    ``None`` (which is the case for weather in the demo).
    """

    def __init__(self) -> None:
        self._last_sent: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, api_key: str) -> asyncio.Lock:
        lock = self._locks.get(api_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[api_key] = lock
        return lock

    async def wait(self, api_key: str, min_interval_s: float) -> None:
        """Sleep if necessary so two requests are at least this far apart."""
        if min_interval_s <= 0:
            return
        async with self._lock_for(api_key):
            now = time.monotonic()
            last = self._last_sent.get(api_key)
            if last is not None:
                elapsed = now - last
                if elapsed < min_interval_s:
                    await asyncio.sleep(min_interval_s - elapsed)
            self._last_sent[api_key] = time.monotonic()


_courtesy_limiter: Optional[CourtesyLimiter] = None


def get_courtesy_limiter() -> CourtesyLimiter:
    """The process-wide courtesy limiter (see CourtesyLimiter)."""
    global _courtesy_limiter
    if _courtesy_limiter is None:
        _courtesy_limiter = CourtesyLimiter()
    return _courtesy_limiter


# ===========================================================================
# Small private helpers
# ===========================================================================

#: HTTP methods with no side effects upstream - always safe to retry.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _new_trace_id() -> str:
    """A short unique id, in the same spirit as Part 1's examples."""
    return f"t-{uuid.uuid4().hex[:12]}"


def _is_retriable_status(status: int) -> bool:
    """HTTP statuses worth retrying: 429 too-many-requests, and any 5xx.

    Other 4xx responses are client errors - retrying repeats the same
    mistake, so the proxy falls back immediately instead.
    """
    return status == 429 or 500 <= status < 600


def _simulated_status(fault: FaultType, real_status: int) -> int:
    """The status code to record when we inject a drill fault.

    HTTP_500 / HTTP_503 record their own code, because that is what the
    caller "saw". Every other fault keeps the real upstream status, so
    the evidence still says what actually happened on the wire.
    """
    if fault is FaultType.HTTP_500:
        return 500
    if fault is FaultType.HTTP_503:
        return 503
    return real_status


def _decode_body(response: httpx.Response) -> Any:
    """Best-effort decode of the upstream body for the caller."""
    try:
        return response.json()
    except ValueError:
        # Not JSON (or truncated) - hand back the raw text instead of
        # raising in the caller's face.
        return response.text


def _breaker_state_of(
    breaker: Optional[CircuitBreaker],
) -> Optional[BreakerState]:
    """The recorded breaker state, or None when no breaker is wired.

    Uses the PURE ``state`` property, never ``effective_state``: reading
    a response must not advance the state machine. A breaker whose sleep
    window has elapsed will show OPEN on this header until the next real
    request arrives and moves it to HALF_OPEN.
    """
    if breaker is None:
        return None
    return breaker.state


def _record_recv(
    bus: Optional[EvidenceBus],
    trace_id: str,
    api_key: str,
    *,
    call_index: int,
    served_from: ServedFrom = ServedFrom.NONE,
    status_code: Optional[int] = None,
    fault: Optional[FaultType] = None,
    latency_ms: Optional[float] = None,
    breaker_state: Optional[BreakerState] = None,
    note: Optional[str] = None,
) -> Optional[EvidenceEvent]:
    """Write one RECV row on the bus, or return None when there is no bus.

    ``call_index`` is always passed explicitly, so a fallback row lands on
    the same logical call as its matching SEND - never drifting onto an
    earlier call when the caller retried.
    """
    if bus is None:
        return None
    return bus.record(
        trace_id,
        api_key,
        Phase.RECV,
        served_from=served_from,
        status_code=status_code,
        fault=fault,
        latency_ms=latency_ms,
        breaker_state=breaker_state,
        call_index=call_index,
        note=note,
    )


def _decide_fault(
    guard: Optional[GuardEvaluator],
    spec: Optional[DrillSpec],
    trace_id: str,
) -> Optional[FaultType]:
    """Ask Part 1's guard whether a drill should fire on this call.

    Returns the FaultType to simulate, or None. Only fires when both a
    guard and a spec were supplied; the guard is only built when the
    spec targets the same api_key as this call.
    """
    if guard is None or spec is None:
        return None
    decision = guard.should_fire(trace_id, spec.target, spec.guard)
    if not decision.should_fire:
        return None
    return spec.fault


def _can_retry(
    *,
    attempt_1based: int,
    method: str,
    policy: ApiPolicy,
    spec: Optional[DrillSpec],
    breaker: Optional[CircuitBreaker],
    started_at: float,
    budget_s: float,
) -> bool:
    """Should we make another attempt after a failed one?

    Five gates, all of which must pass:

        1. attempts left on the policy,
        2. wall-clock time left in the retry budget,
        3. the HTTP method is inherently safe (GET / HEAD / OPTIONS), or
           the policy marks the call idempotent,
        4. the active drill (if any) says this call is safe to retry -
           ``post_effect_drill`` ships ``idempotent=False`` precisely to
           stop a naive retry from committing the action twice,
        5. the breaker (if any) says retrying is allowed. While OPEN it
           refuses - that is the retry-storm guard, and it is why a
           storm cannot be made worse by us.

    Gate 4 exists because ``breaker.should_retry`` reads the *policy's*
    idempotency, not the drill's. Without it, a post-effect drill against
    a GET would be retried three times.
    """
    if attempt_1based >= policy.max_attempts:
        return False
    if (time.perf_counter() - started_at) >= budget_s:
        return False
    if method not in _SAFE_METHODS and not policy.idempotent:
        return False
    if spec is not None and not spec.idempotent:
        return False
    if breaker is not None and not breaker.should_retry(attempt_1based):
        return False
    return True


async def _courtesy_wait(policy: ApiPolicy) -> None:
    """Sleep if needed to respect the policy's request-rate ceiling.

    A no-op for any policy whose ``courtesy_rps`` is ``None``. Runs just
    before the HTTP attempt, and *outside* the per-attempt CallLogger, so
    the logged latency is real upstream latency, not our self-imposed
    politeness delay.
    """
    if policy.courtesy_rps is None or policy.courtesy_rps <= 0:
        return
    min_interval_s = 1.0 / policy.courtesy_rps
    await get_courtesy_limiter().wait(policy.api_key, min_interval_s)


# ===========================================================================
# The main entry point
# ===========================================================================

async def resilient_get(
    api_key: str,
    url: str,
    *,
    method: str = "GET",
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    policy: Optional[ApiPolicy] = None,
    trace_id: Optional[str] = None,
    bus: Optional[EvidenceBus] = None,
    breaker: Optional[CircuitBreaker] = None,
    fallback: Optional[FallbackLadder] = None,
    spec: Optional[DrillSpec] = None,
    commits: Optional[bool] = None,
    logger: Optional[Any] = None,
    client: Optional[httpx.AsyncClient] = None,
    timeout_s: Optional[float] = None,
) -> ResilientResponse:
    """Make one resilient HTTP call to an upstream API, then return it.

    Only ``api_key`` and ``url`` are required. The proxy picks safe
    defaults for whatever is missing:

        policy      -> ``config.load_policy(api_key)``
        trace_id    -> a fresh ``"t-..."`` id
        bus         -> None, so no Part 1 events are written
        breaker     -> None, so the call runs unprotected (this is how
                       Part 4 gets its "control" side of the compare)
        fallback    -> a clear MESSAGE rung
        spec        -> no drill fires
        logger      -> ``logging_conf.get_logger("jizo.proxy")``
        client      -> an ``httpx.AsyncClient`` opened and closed here

    To wire a drill, pass ``bus`` (the shared ``EvidenceBus``) and
    ``spec`` (the active ``DrillSpec`` for this api_key). The proxy then
    writes Part 1 events at the exact points a post-effect guard needs
    (SEND, then POST_EFFECT for committing calls) before it asks the
    guard - so a well-formed drill fires on the intended call k and
    nowhere else.

    ``commits`` says whether this call applies an upstream side effect.
    Left as None, the proxy infers: a POST_EFFECT drill implies a
    committing call, otherwise GET/HEAD/OPTIONS read and everything else
    writes. Callers who need to override (a "committing" GET, or a
    side-effect-free POST) can pass True or False explicitly.
    """

    # ---- 0. Resolve the pieces we were not given ---------------------
    if policy is None:
        policy = load_policy(api_key)
    if trace_id is None:
        trace_id = _new_trace_id()
    if logger is None:
        logger = get_logger("jizo.proxy")

    http_method = method.upper()
    per_attempt_timeout = timeout_s if timeout_s is not None else policy.timeout_s

    # Total wall-clock budget for the whole logical call, from the PRD's
    # "2.2x baseline" rule. Separate from `per_attempt_timeout`, which is
    # how long ONE attempt may hang.
    budget_s = policy.timeout_s * RETRY_BUDGET_MULTIPLIER

    if commits is None:
        # A POST_EFFECT drill implies this call commits something, even
        # when the HTTP method looks like a read.
        if spec is not None and spec.pattern is Pattern.POST_EFFECT:
            commits = True
        else:
            commits = http_method not in _SAFE_METHODS

    # Part 1 guard: only build it when the caller wired the bus AND a
    # drill that actually targets this api_key.
    guard: Optional[GuardEvaluator] = None
    if bus is not None and spec is not None and spec.target.api_key == api_key:
        guard = GuardEvaluator(bus)

    client_owned = client is None
    if client_owned:
        # A short-lived client for callers who did not bring one. We
        # follow redirects so a 3xx does not leak out as a fake success;
        # callers who pass their own client make that choice themselves.
        client = httpx.AsyncClient(follow_redirects=True)
    request_headers = dict(headers) if headers else None

    # ---- 1. Local state shared across attempts -----------------------
    events: list[EvidenceEvent] = []
    attempts_made = 0
    call_index: Optional[int] = None
    fired_fault: Optional[FaultType] = None
    post_effect_recorded = False
    last_reason = "upstream failed"
    started_at = time.perf_counter()

    try:
        # A local alias so the retry decision reads identically at every
        # call site below. `attempt_1based` is always passed explicitly,
        # so there is no loop-capture subtlety.
        def _should_retry(attempt_1based: int) -> bool:
            return _can_retry(
                attempt_1based=attempt_1based,
                method=http_method,
                policy=policy,
                spec=spec,
                breaker=breaker,
                started_at=started_at,
                budget_s=budget_s,
            )

        # ---- 2. The attempt loop -------------------------------------
        for attempt_i in range(policy.max_attempts):
            attempt_1based = attempt_i + 1

            # 2a. Breaker gate for THIS attempt. While OPEN this refuses,
            #     so we never touch the network - the storm guard. A
            #     bookkeeping SEND is still written so this logical call
            #     gets its own call_index; without it, the fallback RECV
            #     would be attributed to an earlier call.
            gate: Optional[GateResult] = None
            was_probe = False
            if breaker is not None:
                gate = breaker.acquire_slot()
                if not gate:
                    last_reason = "breaker refused this call"
                    logger.warning(
                        "breaker_refused",
                        trace_id=trace_id,
                        api_key=api_key,
                        breaker_state=breaker.state.value,
                    )
                    if bus is not None:
                        ev = bus.record(
                            trace_id, api_key, Phase.SEND,
                            call_index=call_index,
                            breaker_state=breaker.state,
                            note="breaker refused; not sent upstream",
                        )
                        call_index = ev.call_index
                        events.append(ev)
                    break
                was_probe = bool(gate.was_probe)

            can_retry = False
            try:
                attempts_made += 1

                # 2b. SEND row on Part 1's bus, BEFORE the request. The
                #     guard reads `call_count`, which this advances; on
                #     retries we pass the same call_index back in so the
                #     counter does not drift and "call 3" stays call 3.
                if bus is not None:
                    ev = bus.record(
                        trace_id, api_key, Phase.SEND,
                        call_index=call_index,
                        breaker_state=_breaker_state_of(breaker),
                    )
                    call_index = ev.call_index
                    events.append(ev)
                elif call_index is None:
                    call_index = 1

                # 2c. Courtesy rate limit (Nominatim 1 rps, and any other
                #     policy that sets courtesy_rps). Runs outside the
                #     CallLogger so the logged latency stays honest.
                await _courtesy_wait(policy)

                # 2d. One HTTP attempt, logged as one JSON line.
                with CallLogger(
                    logger, trace_id, api_key, attempt=attempt_1based,
                ) as call:
                    call.set_breaker_state(_breaker_state_of(breaker))

                    request_started = time.perf_counter()
                    try:
                        response = await client.request(
                            http_method,
                            url,
                            params=params,
                            headers=request_headers,
                            timeout=per_attempt_timeout,
                        )
                    except (httpx.TimeoutException, httpx.RequestError) as err:
                        # ---- Transport failure --------------------------
                        latency_ms = (
                            time.perf_counter() - request_started
                        ) * 1000.0
                        error_name = type(err).__name__
                        last_reason = (
                            "timeout"
                            if isinstance(err, httpx.TimeoutException)
                            else f"connection error ({error_name})"
                        )
                        call.set_served_from(ServedFrom.NONE)
                        call.failure(error_name)
                        if breaker is not None:
                            breaker.record_failure(was_probe=was_probe)
                        recv = _record_recv(
                            bus, trace_id, api_key,
                            call_index=call_index,
                            served_from=ServedFrom.NONE,
                            latency_ms=latency_ms,
                            breaker_state=_breaker_state_of(breaker),
                            note=last_reason,
                        )
                        if recv is not None:
                            events.append(recv)
                        can_retry = _should_retry(attempt_1based)

                    else:
                        # ---- We got an HTTP status -----------------------
                        latency_ms = (
                            time.perf_counter() - request_started
                        ) * 1000.0
                        status = response.status_code

                        if 200 <= status < 300:
                            # ---------- 2xx: maybe success, maybe a drill --
                            # Record POST_EFFECT for committing calls
                            # BEFORE asking the guard, so a post-effect
                            # guard sees the commit evidence it needs on
                            # this very call. Exactly once per logical
                            # call: retries must not fabricate a duplicate.
                            if commits and not post_effect_recorded and bus is not None:
                                pe = bus.record(
                                    trace_id, api_key, Phase.POST_EFFECT,
                                    call_index=call_index,
                                    effect_applied=True,
                                    status_code=status,
                                    breaker_state=_breaker_state_of(breaker),
                                )
                                events.append(pe)
                                post_effect_recorded = True

                            fault_here = _decide_fault(guard, spec, trace_id)

                            if fault_here is not None:
                                # The upstream committed the work (or we
                                # are simulating that), and the drill wants
                                # this occurrence to fail. Record the fault
                                # row and go to the fallback (or retry).
                                fired_fault = fault_here
                                simulated = _simulated_status(fault_here, status)
                                last_reason = f"drill fault: {fault_here.value}"
                                call.set_served_from(ServedFrom.NONE)
                                call.extra(fault=fault_here.value)
                                call.failure(
                                    f"fault_{fault_here.value}",
                                    status_code=simulated,
                                )
                                if breaker is not None:
                                    breaker.record_failure(was_probe=was_probe)
                                recv = _record_recv(
                                    bus, trace_id, api_key,
                                    call_index=call_index,
                                    served_from=ServedFrom.NONE,
                                    status_code=simulated,
                                    fault=fault_here,
                                    latency_ms=latency_ms,
                                    breaker_state=_breaker_state_of(breaker),
                                    note=last_reason,
                                )
                                if recv is not None:
                                    events.append(recv)
                                can_retry = _should_retry(attempt_1based)
                            else:
                                # ---------- Genuine success ------------
                                call.set_served_from(ServedFrom.LIVE)
                                call.success(status)
                                if breaker is not None:
                                    breaker.record_success(was_probe=was_probe)
                                recv = _record_recv(
                                    bus, trace_id, api_key,
                                    call_index=call_index,
                                    served_from=ServedFrom.LIVE,
                                    status_code=status,
                                    latency_ms=latency_ms,
                                    breaker_state=_breaker_state_of(breaker),
                                )
                                if recv is not None:
                                    events.append(recv)
                                return ResilientResponse(
                                    api_key=api_key,
                                    trace_id=trace_id,
                                    data=_decode_body(response),
                                    served_from=ServedFrom.LIVE,
                                    status_code=status,
                                    breaker_state=_breaker_state_of(breaker),
                                    attempts=attempts_made,
                                    latency_ms=(
                                        time.perf_counter() - started_at
                                    ) * 1000.0,
                                    fault=None,
                                    note=None,
                                    events=events,
                                )

                        elif _is_retriable_status(status):
                            # ---------- 429 / 5xx: retriable ----------
                            last_reason = f"HTTP {status}"
                            call.set_served_from(ServedFrom.NONE)
                            call.failure("HTTPStatusError", status_code=status)
                            if breaker is not None:
                                breaker.record_failure(was_probe=was_probe)
                            recv = _record_recv(
                                bus, trace_id, api_key,
                                call_index=call_index,
                                served_from=ServedFrom.NONE,
                                status_code=status,
                                latency_ms=latency_ms,
                                breaker_state=_breaker_state_of(breaker),
                                note=last_reason,
                            )
                            if recv is not None:
                                events.append(recv)
                            can_retry = _should_retry(attempt_1based)

                        else:
                            # ---------- 3xx / other 4xx: do not retry ----
                            # A client error (or an unhandled redirect
                            # when the caller brought their own client)
                            # will not get better by trying again.
                            last_reason = (
                                f"HTTP {status} (client error, not retried)"
                            )
                            call.set_served_from(ServedFrom.NONE)
                            call.failure("HTTPStatusError", status_code=status)
                            if breaker is not None:
                                breaker.record_failure(was_probe=was_probe)
                            recv = _record_recv(
                                bus, trace_id, api_key,
                                call_index=call_index,
                                served_from=ServedFrom.NONE,
                                status_code=status,
                                latency_ms=latency_ms,
                                breaker_state=_breaker_state_of(breaker),
                                note=last_reason,
                            )
                            if recv is not None:
                                events.append(recv)
                            can_retry = False
            finally:
                # Symmetric with `acquire_slot()`, and reached on every
                # path out of the attempt - including the early returns.
                if breaker is not None:
                    breaker.release_slot()

            # 2e. Give up, or back off and try again. The sleep happens
            #     AFTER the CallLogger has closed, so it is not rolled
            #     into the logged upstream latency.
            if not can_retry:
                break
            await asyncio.sleep(policy.backoff_delay_s(attempt_i))

        # ---- 3. Fallback ladder ---------------------------------------
        ladder = fallback if fallback is not None else FallbackLadder()
        value = ladder.resolve(last_reason)
        final_breaker_state = _breaker_state_of(breaker)

        final_event = _record_recv(
            bus, trace_id, api_key,
            call_index=call_index if call_index is not None else 1,
            served_from=value.served_from,
            status_code=value.status_code,
            breaker_state=final_breaker_state,
            note=value.note or last_reason,
        )
        if final_event is not None:
            events.append(final_event)

        logger.info(
            "fallback_served",
            trace_id=trace_id,
            api_key=api_key,
            served_from=value.served_from.value,
            reason=last_reason,
            attempts=attempts_made,
        )

        return ResilientResponse(
            api_key=api_key,
            trace_id=trace_id,
            data=value.data,
            served_from=value.served_from,
            status_code=value.status_code,
            breaker_state=final_breaker_state,
            attempts=attempts_made,
            latency_ms=(time.perf_counter() - started_at) * 1000.0,
            fault=fired_fault,
            note=value.note or last_reason,
            events=events,
        )

    finally:
        # Emit any breaker state change that happened during this call,
        # once per transition. log_breaker_transition dedupes off a
        # watermark on the breaker, so calling it every request is quiet.
        if breaker is not None:
            log_breaker_transition(breaker, logger)

        # Only close a client we opened ourselves.
        if client_owned:
            await client.aclose()


__all__ = [
    "CourtesyLimiter",
    "FallbackLadder",
    "FallbackValue",
    "ResilientResponse",
    "get_breaker_registry",
    "get_courtesy_limiter",
    "reset_breaker_registry",
    "resilient_get",
]