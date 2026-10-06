"""
Regression tests for the final cross-project bug hunt (owner: Pushkar).

Each test pins a defect found by reviewing the whole project before the demo
app was written. They are grouped here so the review's delta is one file.

No database and no network: the proxy tests use `httpx.MockTransport`, and the
dashboard tests build their rows in memory.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import httpx
import pytest

from backend import EvidenceBus, FaultType, Phase, ServedFrom
from backend.breaker import CircuitBreaker
from backend.dashboard import _radar_from, _view_for
from backend.faults import order_sensitive_drill, post_effect_drill
from backend.models import ApiRegistryRow, FiRunRow, RequestLogRow
from backend.proxy import resilient_get
from backend.schemas import ApiPolicy
from backend.scoring import score_run

UPSTREAM = "https://up.test/x"


def _policy(**over) -> ApiPolicy:
    base = dict(api_key="weather", base_url="https://up.test", timeout_s=1.0,
                max_attempts=3, backoff_initial_s=0.001, backoff_max_s=0.002,
                jitter_s=0.001, breaker_min_volume=100)
    base.update(over)
    return ApiPolicy(**base)


def _flaky_transport() -> tuple[httpx.AsyncClient, dict]:
    """503 twice, then 200 - so a retrying call succeeds on attempt 3."""
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        return httpx.Response(503) if seen["n"] < 3 else httpx.Response(200, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def test_order_sensitive_non_rival_fault_is_not_punished():
    """A commit after a DELAY is acting on the real answer, not a rival one.

    `committed_on_rival` was applied to every fault, so a legal non-rival
    fault on this pattern flipped CW/PS/Prem and failed a correct run.
    """
    bus = EvidenceBus()
    trace = "r2-scoring"
    bus.record(trace, "weather", Phase.SEND)
    bus.record(trace, "weather", Phase.RECV, served_from=ServedFrom.LIVE,
               fault=FaultType.DELAY, status_code=200)
    bus.record(trace, "weather", Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, "weather", Phase.RECV, served_from=ServedFrom.LIVE,
               status_code=200)
    spec = order_sensitive_drill(trace, "weather", k=1, n=1, fault=FaultType.DELAY)

    result = score_run(spec, bus.events(trace))

    assert result.ts is True, result.notes


# ---------------------------------------------------------------------------
# proxy retry gates
# ---------------------------------------------------------------------------


def _attempts_for(spec_arg, breaker=None, **policy_over):
    async def drive():
        client, seen = _flaky_transport()
        policy = _policy(**policy_over)
        await resilient_get("weather", UPSTREAM, policy=policy, trace_id="r2-pr",
                            bus=EvidenceBus(), breaker=breaker, spec=spec_arg,
                            client=client)
        await client.aclose()
        return seen["n"]

    return asyncio.run(drive())


def test_an_unrelated_non_idempotent_drill_does_not_block_retries():
    """A drill targeting another api_key must not switch retries off here."""
    unrelated = post_effect_drill("r2-other", "payments", idempotent=False)

    assert _attempts_for(None) == 3
    assert _attempts_for(unrelated) == 3


def test_wiring_a_breaker_does_not_change_retry_safety():
    """The protected path must not retry LESS than the unprotected one.

    Gate 5 used `breaker.should_retry()`, which re-applied `policy.idempotent`,
    so a safe GET stopped retrying the moment a breaker was wired.
    """
    policy = _policy(idempotent=False)

    without = _attempts_for(None, idempotent=False)
    with_breaker = _attempts_for(None, breaker=CircuitBreaker(policy),
                                 idempotent=False)

    assert without == with_breaker == 3


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------


def _run(**over) -> FiRunRow:
    base = dict(run_id="r2-run", pattern="k_of_n", fault="http_500",
                target_api="weather", target_k=1, target_phase="recv",
                guard_api="weather", guard_phase="send", guard_min_count=1,
                idempotent=True, total_occurrences=1, ts=False, cw=False,
                ps=True, prem=False, miss=True, mult=False, trace_id="r2-t",
                spec={}, timeline=[])
    base.update(over)
    return FiRunRow(**base)


def test_radar_detection_uses_the_stored_timeline():
    """An empty timeline made every failing run a false negative.

    `axes.detection` reads `result.timeline`; the dashboard passed `[]`, so
    the breaker could have caught every fault and detection still scored 0.
    """
    timeline = [{"trace_id": "r2-t", "api_key": "weather", "phase": "recv",
                 "served_from": "none", "occurrence": 1, "call_index": 1,
                 "breaker_state": "OPEN"}]
    radar = _radar_from([_run(timeline=timeline)], {"r2-t": {"weather"}})
    detection = {r["axis"]: r["value"] for r in radar}["detection"]

    assert detection == 100.0


def test_non_finite_latency_is_filtered_out():
    """NaN/Infinity latencies reach json.dumps, which emits invalid JSON and
    blanked the entire board in the browser."""
    registry = {"weather": ApiRegistryRow(api_key="weather",
                                          base_url="https://x.test")}
    logs = [
        RequestLogRow(trace_id="t", api_key="weather", phase="recv",
                      occurrence=1, call_index=1, served_from="live",
                      status_code=200, latency_ms=float("inf"),
                      ts=dt.datetime(2026, 1, 1)),
        RequestLogRow(trace_id="t", api_key="weather", phase="recv",
                      occurrence=1, call_index=2, served_from="live",
                      status_code=200, latency_ms=120.0,
                      ts=dt.datetime(2026, 1, 1)),
    ]

    view = _view_for("weather", registry, {}, logs, [])

    assert view["latency"]["p90"] == 120.0
    assert "Infinity" not in json.dumps(view, default=str)
    assert all(row["current"] != float("inf") for row in view["latencyRows"])


# ---------------------------------------------------------------------------
# /fi/run input validation
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, row) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def execute(self, stmt):
        class _R:
            def scalars(self_inner):
                return self_inner

            def all(self_inner):
                return []

        return _R()

    async def scalar(self, stmt):
        return 0

    async def get(self, model, pk):
        return None


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from backend import main as main_mod
    from backend.db import get_session as real_get_session
    from backend.main import app

    fake = _FakeSession()

    async def _get_session():
        yield fake

    def _no_database():
        raise RuntimeError("no database in the unit suite")

    app.dependency_overrides[real_get_session] = _get_session
    monkeypatch.setattr(main_mod, "get_sessionmaker", _no_database, raising=False)
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _body(run_id="r2-e2e", api_key="weather"):
    return {
        "run_id": run_id, "pattern": "k_of_n", "fault": "http_500",
        "target": {"api_key": api_key, "phase": "recv", "occurrence": 1},
        "guard": {"api_key": api_key, "phase": "send", "min_count": 1},
        "total_occurrences": 1, "idempotent": True,
    }


def test_fi_run_rejects_an_over_long_run_id(client):
    """VARCHAR(128) overflow used to reach asyncpg and surface as a 500."""
    response = client.post("/fi/run", json=_body(run_id="x" * 200))

    assert response.status_code == 422


def test_fi_run_rejects_an_over_long_guard_api_key(client):
    """guard_api is VARCHAR(64); target.api_key is caught earlier by load_policy."""
    response = client.post("/fi/run", json=_body(api_key="w" * 100))

    assert response.status_code == 422
