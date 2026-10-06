"""
Regression tests for the P4 review fixes (owner: Pushkar; fixes Riya's P4).

Each test here pins a bug that shipped in `backend/main.py` / `backend/compare.py`
and was found in review. They are kept in one file so the review's delta is
easy to read: if you are looking at why a line in P4 exists, the matching test
is one of these.

No database, no network - same discipline as `test_main.py`: a fake session and
a stubbed `resilient_get`, so the suite stays in the millisecond range.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import EvidenceBus, Phase, ServedFrom
from backend.breaker import BreakerRegistry
from backend.compare import side_metrics
from backend.main import MAX_DRILL_OCCURRENCES, _plan_shape, app
from backend.models import FiRunRow, RequestLogRow
from backend.proxy import ResilientResponse


# ---------------------------------------------------------------------------
# Test doubles (minimal - `test_main.py` has the full-featured one)
# ---------------------------------------------------------------------------


class FakeSession:
    """The subset of AsyncSession that `main.py` touches, with a fixed store."""

    def __init__(self, run_rows=None) -> None:
        self.added: list = []
        self.run_rows = run_rows or {}
        self.commits = 0

    def add(self, row) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, stmt):
        return _Result([])

    async def scalar(self, stmt):
        return 0

    async def get(self, model, pk):
        return self.run_rows.get(pk)


class _Result:
    def __init__(self, rows) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


def _no_database():
    raise RuntimeError("no database in the unit suite")


def _build_client(monkeypatch, run_rows=None):
    """Enter a TestClient with a fake session; caller uses it as a context."""
    from backend import main as main_mod
    from backend.db import get_session as real_get_session

    fake = FakeSession(run_rows=run_rows)

    async def _get_session():
        yield fake

    app.dependency_overrides[real_get_session] = _get_session
    monkeypatch.setattr(main_mod, "get_sessionmaker", _no_database, raising=False)
    monkeypatch.setattr(main_mod, "_registry", lambda: BreakerRegistry(), raising=False)
    return fake


@pytest.fixture
def client(monkeypatch):
    fake = _build_client(monkeypatch)
    try:
        with TestClient(app) as test_client:
            test_client.fake_session = fake
            yield test_client
    finally:
        app.dependency_overrides.clear()


def _fi_body(run_id: str, api_key: str = "weather",
             total_occurrences: int = 1) -> dict:
    return {
        "run_id": run_id,
        "pattern": "k_of_n",
        "fault": "http_500",
        "target": {"api_key": api_key, "phase": "recv", "occurrence": 1},
        "guard": {"api_key": api_key, "phase": "send", "min_count": 1},
        "total_occurrences": total_occurrences,
        "idempotent": True,
    }


def _completed_run(run_id: str) -> FiRunRow:
    """A finished `fi_runs` row, as if a drill already ran under this id."""
    return FiRunRow(
        run_id=run_id, pattern="k_of_n", fault="http_500",
        target_api="weather", target_k=1, target_phase="recv",
        guard_api="weather", guard_phase="send", guard_min_count=1,
        idempotent=True, total_occurrences=1,
        ts=True, cw=True, ps=True, prem=False, miss=False, mult=False,
        spec=_fi_body(run_id),
    )


# ---------------------------------------------------------------------------
# compare.py - a call is (trace_id, api_key, call_index)
# ---------------------------------------------------------------------------


def test_side_metrics_does_not_merge_calls_from_different_traces():
    """Keying on (api_key, call_index) collapsed three traces into one call."""
    bus = EvidenceBus()
    for trace in ("t1", "t2", "t3"):
        bus.record(trace, "weather", Phase.SEND)
    for trace, served in (("t1", ServedFrom.LIVE),
                          ("t2", ServedFrom.LIVE),
                          ("t3", ServedFrom.NONE)):
        bus.record(trace, "weather", Phase.RECV, served_from=served)

    metrics = side_metrics(
        bus.events("t1") + bus.events("t2") + bus.events("t3")
    )

    assert metrics.calls == 3
    assert metrics.success == 2


def test_unrelated_traces_are_not_a_duplicate():
    """Two separate traces each committing once is not a double charge."""
    bus = EvidenceBus()
    for trace in ("t1", "t2"):
        bus.record(trace, "weather", Phase.SEND)
        bus.record(trace, "weather", Phase.POST_EFFECT, effect_applied=True)
        bus.record(trace, "weather", Phase.RECV, served_from=ServedFrom.LIVE)

    metrics = side_metrics(bus.events("t1") + bus.events("t2"))

    assert metrics.duplicates == 0


def test_a_duplicate_within_one_trace_is_still_caught():
    """The fix must not go too far and miss a real double charge."""
    bus = EvidenceBus()
    bus.record("t1", "weather", Phase.SEND)
    bus.record("t1", "weather", Phase.POST_EFFECT, effect_applied=True)
    bus.record("t1", "weather", Phase.POST_EFFECT, effect_applied=True)
    bus.record("t1", "weather", Phase.RECV, served_from=ServedFrom.LIVE)

    assert side_metrics(bus.events("t1")).duplicates == 1


# ---------------------------------------------------------------------------
# main.py - the upstream URL must not be injectable
# ---------------------------------------------------------------------------


def test_plan_shape_url_encodes_the_address():
    """A raw `&` in the address injected extra upstream query parameters."""
    urls = _plan_shape("a&limit=100")

    assert "q=a%26limit%3D100" in urls["geocode"]
    assert "&limit=100&format" not in urls["geocode"]


def test_plan_shape_url_encodes_a_space():
    urls = _plan_shape("New Delhi")

    assert " " not in urls["weather"]
    assert "New%20Delhi" in urls["weather"]


# ---------------------------------------------------------------------------
# main.py - /fi/run input handling
# ---------------------------------------------------------------------------


def test_fi_run_unknown_api_key_is_422_not_500(client):
    """An unregistered api_key raised KeyError, which FastAPI served as 500."""
    response = client.post("/fi/run", json=_fi_body("bad-api-1", api_key="nope"))

    assert response.status_code == 422
    assert "nope" in response.json()["detail"]


def test_fi_run_rejects_an_unbounded_occurrence_count(client):
    """Without a cap, one request could issue millions of upstream calls."""
    response = client.post(
        "/fi/run",
        json=_fi_body("huge-1", total_occurrences=MAX_DRILL_OCCURRENCES + 1),
    )

    assert response.status_code == 422


def test_fi_run_duplicate_run_id_returns_the_first_verdict(monkeypatch):
    """Re-firing a finished run_id hit fi_runs' PK and 500'd."""
    run_id = "already-done-1"
    fake = _build_client(monkeypatch, run_rows={run_id: _completed_run(run_id)})
    try:
        # No resilient_get stub: the route must return before calling upstream.
        with TestClient(app, raise_server_exceptions=False) as test_client:
            response = test_client.post("/fi/run", json=_fi_body(run_id))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["run_id"] == run_id
    assert response.json()["ts"] is True
    assert fake.commits == 0, "the finished run's plan row was re-inserted"


def test_fi_run_persists_the_real_attempt_count(client, monkeypatch):
    """Every occurrence row was stored as attempt=1 regardless of retries."""
    from backend import main as main_mod

    async def _fake(api_key, url, **kwargs):
        bus = kwargs.get("bus")
        trace_id = kwargs.get("trace_id")
        if bus is not None:
            bus.record(trace_id, api_key, Phase.SEND)
            bus.record(trace_id, api_key, Phase.RECV,
                       served_from=ServedFrom.LIVE, status_code=200)
        return ResilientResponse(
            api_key=api_key, trace_id=trace_id, data={"ok": True},
            served_from=ServedFrom.LIVE, status_code=200,
            breaker_state=None, attempts=3, latency_ms=1.0,
        )

    monkeypatch.setattr(main_mod, "resilient_get", _fake)

    response = client.post("/fi/run", json=_fi_body("attempts-1"))

    assert response.status_code == 200
    rows = [r for r in client.fake_session.added
            if isinstance(r, RequestLogRow)]
    assert rows, "no evidence rows were written"
    assert all(r.attempt == 3 for r in rows), (
        f"attempts lost: {[r.attempt for r in rows]}"
    )


# ---------------------------------------------------------------------------
# main.py - the control arm must actually be unprotected
# ---------------------------------------------------------------------------


def test_control_arm_gets_no_breaker_no_cache_and_no_retry(client, monkeypatch):
    """The control arm reused the experiment's ladder (shared cache key) and
    P2's retry budget, so it could be served the protected arm's cached value.
    """
    from backend import main as main_mod

    captured: list[dict] = []

    async def _fake(api_key, url, **kwargs):
        captured.append({"api_key": api_key, **kwargs})
        return ResilientResponse(
            api_key=api_key, trace_id=kwargs.get("trace_id"), data={"ok": True},
            served_from=ServedFrom.LIVE, status_code=200,
            breaker_state=None, attempts=1, latency_ms=1.0,
        )

    monkeypatch.setattr(main_mod, "resilient_get", _fake)

    client.post("/route/plan")

    control = [c for c in captured if c.get("breaker") is None]
    experiment = [c for c in captured if c.get("breaker") is not None]
    assert len(control) == 2 and len(experiment) == 2, (
        "expected one control and one experiment call per dependency"
    )

    for call in control:
        assert call["policy"].max_attempts == 1, "control arm still retries"
        assert call["fallback"].cache is None, "control reads a cached value"
        assert call["fallback"].default is None

    for call in experiment:
        assert call["fallback"].cache is not None, (
            "the protected arm lost its cache rung"
        )
