"""
P6 integration tests - the parts wired together, not one at a time.

Owner: Pushkar (P6). The unit suites prove each part in isolation; these prove
the whole path: a request goes through P2's real proxy (retry, breaker,
fallback), P1's guard decides when a drill fault fires, P1's scorer grades the
evidence, and - in the last test - P4's HTTP route stores the result in P3.

Nothing here re-mocks the thing under test. The upstream is faked with
`httpx.MockTransport` (a transport, not a `resilient_get` stub), so the real
retry loop, the real breaker gate and the real fallback ladder all run.

The three the work-division asks for:
  * k-of-n end to end - only the targeted call falls back
  * OPEN -> HALF_OPEN -> CLOSED on recovery
  * the storm guard: while OPEN, zero upstream calls and zero retries
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from backend import (
    BreakerState,
    EvidenceBus,
    FaultType,
    Phase,
    ServedFrom,
)
from backend.breaker import CircuitBreaker
from backend.faults import k_of_n_drill
from backend.proxy import FallbackLadder, resilient_get
from backend.schemas import ApiPolicy
from backend.scoring import score_run

UPSTREAM = "https://up.test/x"


class FakeClock:
    """A clock we move by hand, so the breaker's sleep window is instant."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _policy(**over) -> ApiPolicy:
    """A fast test policy: no retries mid-flight, tiny breaker thresholds.

    `max_attempts=1` keeps a single call to a single HTTP attempt so a test
    measures the drill, not the retry timing. The breaker thresholds are small
    so a handful of calls can open it.
    """
    base = dict(
        api_key="weather",
        base_url="https://up.test",
        timeout_s=1.0,
        max_attempts=1,
        backoff_initial_s=0.001,
        backoff_max_s=0.002,
        jitter_s=0.001,
        breaker_window=20,
        breaker_error_threshold=0.25,
        breaker_min_volume=4,
        breaker_sleep_s=10.0,
        half_open_probes=2,
    )
    base.update(over)
    return ApiPolicy(**base)


def _ok_transport(calls: list | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _dead_transport(calls: list | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        raise httpx.ConnectError("upstream unreachable", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _recv_for_call(events, call_index: int):
    """The last RECV row belonging to one logical call."""
    rows = [e for e in events if e.phase is Phase.RECV and e.call_index == call_index]
    return rows[-1] if rows else None


# ---------------------------------------------------------------------------
# 1. k-of-n end to end
# ---------------------------------------------------------------------------


def test_k_of_n_end_to_end_only_the_targeted_call_falls_back():
    """Four real calls through the proxy; the drill breaks call 3 and only 3.

    The upstream is genuinely up (every response is 200) - the fault is
    injected by P1's guard at the intended timing, which is the whole point
    of temporal fault injection.
    """
    spec = k_of_n_drill("it-kgtn-1", "weather", k=3, n=4,
                        fault=FaultType.HTTP_500)
    policy = _policy()
    breaker = CircuitBreaker(policy, clock=FakeClock())
    bus = EvidenceBus()
    ladder = FallbackLadder(default=lambda: {"cached": "weather"})
    client = _ok_transport()

    async def drive():
        for _ in range(4):
            await resilient_get(
                "weather", UPSTREAM, policy=policy, trace_id=spec.run_id,
                bus=bus, breaker=breaker, fallback=ladder, spec=spec,
                client=client,
            )
        await client.aclose()
        return bus.events(spec.run_id)

    events = asyncio.run(drive())
    result = score_run(spec, events)

    assert result.ts is True, result.notes

    call3 = _recv_for_call(events, 3)
    assert call3 is not None
    assert call3.served_from is ServedFrom.DEFAULT, (
        "the faulted call did not fall back"
    )

    for index in (1, 2, 4):
        recv = _recv_for_call(events, index)
        assert recv is not None and recv.served_from is ServedFrom.LIVE, (
            f"call {index} should have been untouched by the drill"
        )


def test_a_single_occurrence_drill_breaks_exactly_one_call():
    """n=1, k=1: the simplest drill, still end to end."""
    spec = k_of_n_drill("it-kgtn-2", "weather", k=1, n=1)
    policy = _policy()
    bus = EvidenceBus()
    client = _ok_transport()

    async def drive():
        await resilient_get(
            "weather", UPSTREAM, policy=policy, trace_id=spec.run_id,
            bus=bus, breaker=CircuitBreaker(policy, clock=FakeClock()),
            fallback=FallbackLadder(default=lambda: {"cached": True}),
            spec=spec, client=client,
        )
        await client.aclose()
        return bus.events(spec.run_id)

    result = score_run(spec, asyncio.run(drive()))
    assert result.ts is True, result.notes


# ---------------------------------------------------------------------------
# 2. the breaker cycle
# ---------------------------------------------------------------------------


def test_breaker_opens_then_half_opens_then_closes():
    """The full state walk, driven through real proxy calls.

    Failures go through `resilient_get` (not `record_failure` directly) so the
    breaker is exercised the way production exercises it. Every gate is taken
    by a real call - taking one by hand would consume the probe budget and
    make the HALF_OPEN step unreachable.
    """
    clock = FakeClock()
    policy = _policy()
    breaker = CircuitBreaker(policy, clock=clock)
    bus = EvidenceBus()
    dead = _dead_transport()

    async def drive(client, count=1):
        for _ in range(count):
            await resilient_get(
                "weather", UPSTREAM, policy=policy, trace_id="it-cycle",
                bus=bus, breaker=breaker, client=client,
            )

    asyncio.run(drive(dead, policy.breaker_min_volume))
    asyncio.run(dead.aclose())
    assert breaker.state is BreakerState.OPEN

    # Past the sleep window, the next call is a probe: OPEN -> HALF_OPEN.
    clock.advance(policy.breaker_sleep_s + 1)
    live = _ok_transport()

    asyncio.run(drive(live))                  # probe 1 of 2
    assert breaker.state is BreakerState.HALF_OPEN

    asyncio.run(drive(live))                  # probe 2 of 2 -> close
    asyncio.run(live.aclose())
    assert breaker.state is BreakerState.CLOSED


# ---------------------------------------------------------------------------
# 3. the storm guard
# ---------------------------------------------------------------------------


def test_open_breaker_touches_nothing_and_never_retries():
    """While OPEN: zero upstream calls, zero attempts, a clean fallback."""
    clock = FakeClock()
    policy = _policy()
    breaker = CircuitBreaker(policy, clock=clock)
    for _ in range(policy.breaker_min_volume):
        breaker.record_failure()
    assert breaker.state is BreakerState.OPEN

    calls: list = []
    client = _ok_transport(calls)
    bus = EvidenceBus()

    async def drive():
        response = await resilient_get(
            "weather", UPSTREAM, policy=policy, trace_id="it-storm",
            bus=bus, breaker=breaker,
            fallback=FallbackLadder(message="upstream unavailable"),
            client=client,
        )
        await client.aclose()
        return response

    response = asyncio.run(drive())

    assert calls == [], "the storm guard let a request reach the upstream"
    assert response.attempts == 0, "an OPEN breaker must not retry"
    assert response.served_from is ServedFrom.MESSAGE
    assert response.breaker_state is BreakerState.OPEN


def test_a_refused_call_still_gets_its_own_call_index():
    """The refusal writes bookkeeping evidence, so call indexing never drifts."""
    clock = FakeClock()
    policy = _policy()
    breaker = CircuitBreaker(policy, clock=clock)
    for _ in range(policy.breaker_min_volume):
        breaker.record_failure()
    bus = EvidenceBus()
    client = _ok_transport()

    async def drive():
        await resilient_get(
            "weather", UPSTREAM, policy=policy, trace_id="it-index",
            bus=bus, breaker=breaker, client=client,
        )
        await client.aclose()
        return bus.events("it-index")

    events = asyncio.run(drive())
    sends = [e for e in events if e.phase is Phase.SEND]
    assert len(sends) == 1
    assert sends[0].call_index == 1


# ---------------------------------------------------------------------------
# 4. retries stay one logical call
# ---------------------------------------------------------------------------


def test_a_retried_call_is_still_one_call_index():
    """A call that fails twice then succeeds is ONE call, not three."""
    policy = _policy(max_attempts=3, breaker_min_volume=100)
    breaker = CircuitBreaker(policy, clock=FakeClock())
    bus = EvidenceBus()
    attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def drive():
        response = await resilient_get(
            "weather", UPSTREAM, policy=policy, trace_id="it-retry",
            bus=bus, breaker=breaker, client=client,
        )
        await client.aclose()
        return response

    response = asyncio.run(drive())

    assert response.attempts == 3
    assert response.served_from is ServedFrom.LIVE
    events = bus.events("it-retry")
    recvs = [e for e in events if e.phase is Phase.RECV]
    assert {e.call_index for e in recvs} == {1}, "retries drifted the call index"


# ---------------------------------------------------------------------------
# 5. the whole stack through HTTP + the database
# ---------------------------------------------------------------------------


def test_fi_run_end_to_end_through_http_proxy_and_store():
    """POST /fi/run -> real proxy -> scorer -> P3 store -> read it back.

    The only test here that needs Postgres. It replaces the app's HTTP client
    with a MockTransport, so P4's route, P2's proxy and P3's persistence all
    run for real; only the upstream socket is fake.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy import delete

    from backend.db import get_sessionmaker
    from backend.main import app
    from backend.models import FiRunRow, RequestLogRow

    # skip when there is no database, like the other P3-backed tests
    from tests.test_data import _database_or_skip
    _database_or_skip()

    run_id = "it-e2e-1"
    body = {
        "run_id": run_id,
        "pattern": "k_of_n",
        "fault": "http_500",
        "target": {"api_key": "weather", "phase": "recv", "occurrence": 3},
        "guard": {"api_key": "weather", "phase": "send", "min_count": 3},
        "total_occurrences": 4,
        "idempotent": True,
    }

    async def cleanup():
        factory = get_sessionmaker()
        async with factory() as s:
            await s.execute(delete(RequestLogRow)
                            .where(RequestLogRow.trace_id == run_id))
            await s.execute(delete(FiRunRow).where(FiRunRow.run_id == run_id))
            await s.commit()

    asyncio.run(cleanup())
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            # every upstream response is 200; the guard injects the fault
            app.state.jizo.client = _ok_transport()
            response = client.post("/fi/run", json=body)
            assert response.status_code == 200, response.text
            verdict = response.json()
            assert verdict["ts"] is True, verdict["explain"]

            stored = client.get(f"/fi/runs/{run_id}")
            assert stored.status_code == 200
            assert stored.json()["run_id"] == run_id
    finally:
        asyncio.run(cleanup())
