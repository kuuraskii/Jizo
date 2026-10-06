"""
Tests for the JIZO FastAPI application.

Owner: Riya (P4). Implementation: `backend/main.py`.

Scope: this file tests `main.py` only. The comparator has its own suite
(`test_compare.py`), the grader has `test_scoring.py`, and the protector has
`test_breaker.py`. Nothing here re-tests another part - it tests the wiring
between them.

## No database, no network

Every test here runs against `fastapi.testclient.TestClient` with P3's
``get_session`` dependency overridden by a fake session, and with
``resilient_get`` replaced so no socket is ever opened. That keeps the suite
in the millisecond range and impossible to flake, which is the discipline the
rest of the project follows.

The three things worth being careful about, and which therefore have their own
groups below:

1. **A database outage must not stop the app starting.** The lifespan is
   best-effort by design; a registry warm-up failure is logged, not raised.
2. **/health never 500s and /ready 503s when not ready.** These are the
   endpoints an orchestrator and a human both rely on.
3. **Reading breaker state must not advance it.** A dashboard polling
   /breaker/state once a second cannot be allowed to arm a probe budget
   (edgecases BR-30, BR-40).

Run:
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_main.py -v
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import BreakerState, FaultType, Phase, ServedFrom
from backend.breaker import BreakerRegistry
from backend.main import (
    VALUE_CACHE_MAX,
    AppState,
    DashboardHub,
    app,
    lifespan,
)
from backend.models import RequestLogRow
from backend.proxy import ResilientResponse
from backend.schemas import DrillSpec, EvidenceEvent


API = "weather"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeSession:
    """An AsyncSession stand-in that records what was written.

    Implements exactly the surface `main.py` uses: `add`, `commit`, `execute`,
    `get`. Deliberately not a mock library - a fake that fails loudly on an
    unexpected call is better than one that silently accepts it.
    """

    def __init__(self, rows: Optional[list[RequestLogRow]] = None,
                 run_rows: Optional[dict] = None) -> None:
        self.added: list[RequestLogRow] = []
        self.rows = list(rows or [])
        self.run_rows = run_rows or {}
        self.commits = 0

    def add(self, row) -> None:
        """Synchronous, exactly like SQLAlchemy's AsyncSession.add.

        P3's `save_run` calls `session.add(row)` without awaiting, because that
        is how the real API works. An async stub would silently swallow every
        row - the coroutine would be created and never awaited, so assertions on
        `added` would fail with no obvious cause.
        """
        self.added.append(row)

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, stmt):
        """Return rows matching the where-clause of `main.py`'s arm query.

        Searches BOTH the pre-seeded `rows` and everything the route has
        written via `add()`. A real session returns its own flushed rows here,
        so a fake that only searched `rows` would make the route's read-back
        look empty - which is exactly how the mode-tagging bug stayed hidden.
        """
        bound = self._bound_params(stmt)
        pool = self.rows + [
            r for r in self.added if hasattr(r, "trace_id")
        ]
        matched = [r for r in pool if self._matches(r, bound)]
        return _FakeResult(matched)

    async def scalar(self, stmt):
        """`store._evidence_already_stored` counts existing rows for a trace.

        Returning 0 means "no evidence stored yet", so `save_run` proceeds to
        write - which is the state a fresh drill run is in.
        """
        return 0

    async def get(self, model, pk):
        return self.run_rows.get(pk)

    @staticmethod
    def _bound_params(stmt) -> dict:
        """Pull column -> value pairs out of a select's where criteria."""
        from sqlalchemy import BinaryExpression

        found: dict[str, Any] = {}
        for criterion in getattr(stmt, "_where_criteria", ()):
            if not isinstance(criterion, BinaryExpression):
                continue
            try:
                found[str(criterion.left)] = criterion.right.value
            except Exception:  # noqa: BLE001 - non-literal bind, skip
                continue
        return found

    @staticmethod
    def _matches(row: RequestLogRow, bound: dict) -> bool:
        for column, value in bound.items():
            name = column.split(".")[-1]
            if getattr(row, name, None) != value:
                return False
        return True


class _FakeResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


def _row(**kwargs) -> RequestLogRow:
    """A `request_logs` row with sensible defaults."""
    base = dict(
        trace_id="t-1", api_key=API, phase="recv", occurrence=1, call_index=1,
        served_from="live", effect_applied=False, leaked_raw_error=False,
        mode="experiment", fault=None,
    )
    base.update(kwargs)
    return RequestLogRow(**base)


def _event(**kwargs) -> EvidenceEvent:
    base = dict(trace_id="t-1", api_key=API, phase=Phase.RECV,
                served_from=ServedFrom.LIVE, occurrence=1, call_index=1)
    base.update(kwargs)
    return EvidenceEvent(**base)


@pytest.fixture
def client(monkeypatch):
    """A TestClient whose lifespan ran, with a fake session injected.

    Two things have to be done the *right* way here, both learned the hard way:

    * ``Depends(get_session)`` is bound when the route is decorated, so
      monkeypatching the module attribute does nothing. The supported override
      is ``app.dependency_overrides``.
    * the lifespan creates its own ``AppState``, so ``app.state.jizo`` is read
      *after* the client is entered - not the object created out here.
    """
    from backend import main as main_mod
    from backend.db import get_session as real_get_session

    registry = BreakerRegistry()
    fake = FakeSession()

    async def _get_session():
        yield fake

    app.dependency_overrides[real_get_session] = _get_session

    # No database in the unit suite: fail the warm-up instantly rather than
    # waiting on a refused TCP connection. The lifespan is supposed to survive
    # exactly this, and `test_app_starts_even_when_the_registry_cannot_be_warmed`
    # covers that path deliberately.
    def _no_database():
        raise RuntimeError("no database in the unit suite")

    monkeypatch.setattr(main_mod, "get_sessionmaker", _no_database, raising=False)
    monkeypatch.setattr(main_mod, "_registry", lambda: registry, raising=False)

    try:
        with TestClient(app) as test_client:
            test_client.jizo_state = app.state.jizo
            test_client.registry = registry
            test_client.fake_session = fake
            yield test_client
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Group 1 - the lifespan must never be fatal
# ---------------------------------------------------------------------------


def test_app_starts_even_when_the_registry_cannot_be_warmed(monkeypatch):
    """A DB outage must not stop the process starting.

    Otherwise /health cannot report that the database is the problem - which is
    the one thing a health endpoint exists to say.
    """
    from backend import main as main_mod

    def _explode():
        # Synchronous, because `get_sessionmaker()` is a sync factory call.
        raise RuntimeError("connection refused")

    monkeypatch.setattr(main_mod, "get_sessionmaker", _explode)

    app.state.jizo = AppState()

    async def _drive():
        async with lifespan(app):
            # The lifespan creates its own AppState, so read it back rather
            # than holding the object created above.
            return app.state.jizo

    used = asyncio.run(_drive())
    assert used.registry_warmed is False
    assert used.registry_error is not None
    assert "RuntimeError" in used.registry_error


def test_startup_registers_policies_and_creates_the_http_client(monkeypatch):
    """P3's policies must reach P2, or a .env edit changes nothing live."""
    from backend import main as main_mod
    from backend.schemas import ApiPolicy

    captured: dict = {}

    async def _fake_registry(_session):
        return [ApiPolicy(api_key="weather", base_url="https://example.test")]

    def _fake_register(policies):
        captured["keys"] = [p.api_key for p in policies]
        return captured["keys"]

    class _Factory:
        def __call__(self):
            return _Ctx()

    class _Ctx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(main_mod, "get_sessionmaker", _Factory)
    monkeypatch.setattr(main_mod, "load_registry", _fake_registry)
    monkeypatch.setattr(main_mod, "register_registry", _fake_register)

    state = AppState()
    app.state.jizo = state
    observed: dict = {}

    async def _drive():
        async with lifespan(app):
            # Inspect *inside* the context: shutdown closes the client and
            # nulls the attribute, so reading it afterwards proves nothing.
            observed["warmed"] = app.state.jizo.registry_warmed
            observed["has_client"] = app.state.jizo.client is not None
        observed["closed_after"] = app.state.jizo.client is None

    asyncio.run(_drive())

    assert captured["keys"] == ["weather"]
    assert observed["warmed"] is True
    assert observed["has_client"] is True, "startup must create the shared client"
    assert observed["closed_after"] is True, "shutdown must close it"


def test_shutdown_closes_the_client_and_the_engine(monkeypatch):
    from backend import main as main_mod

    disposed: list[bool] = []

    async def _dispose():
        disposed.append(True)

    monkeypatch.setattr(main_mod, "dispose_engine", _dispose)
    monkeypatch.setattr(
        main_mod, "get_sessionmaker",
        lambda: (_ for _ in ()).throw(RuntimeError("no db")),
        raising=False,
    )

    async def _drive():
        async with lifespan(app):
            pass

    asyncio.run(_drive())
    assert disposed == [True]


# ---------------------------------------------------------------------------
# Group 2 - health and readiness
# ---------------------------------------------------------------------------


def test_health_answers_200_even_when_unhealthy(client, monkeypatch):
    """Liveness is "this process answers". The body carries the truth.

    Returning 503 here would make an orchestrator restart a process that is
    correctly reporting an outage.
    """
    async def _unhealthy():
        return {"status": "unhealthy", "ok": False, "tables": {}}

    monkeypatch.setattr("backend.main.check_health", _unhealthy)

    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "unhealthy"


def test_health_does_not_propagate_a_database_failure(client, monkeypatch):
    """P3's contract is that check_health never raises. Prove the route holds."""

    async def _never_raises():
        raise AssertionError("check_health raised")

    monkeypatch.setattr("backend.main.check_health", _never_raises)
    assert client.get("/health").status_code == 200


def test_ready_returns_503_when_not_ready(client, monkeypatch):
    async def _not_ready():
        return {"ready": False, "reason": "database unreachable"}

    monkeypatch.setattr("backend.main.check_ready", _not_ready)

    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["ready"] is False


def test_ready_returns_200_when_ready(client, monkeypatch):
    async def _ready():
        return {"ready": True, "reason": "ok"}

    monkeypatch.setattr("backend.main.check_ready", _ready)
    assert client.get("/ready").status_code == 200


# ---------------------------------------------------------------------------
# Group 3 - breaker state must be read-only
# ---------------------------------------------------------------------------


def test_breaker_state_reports_every_registered_api(client):
    response = client.get("/breaker/state")
    assert response.status_code == 200
    body = response.json()
    assert "states" in body and "snapshot" in body


def test_polling_breaker_state_never_advances_the_machine(client):
    """BR-30 / BR-40: a dashboard poll must not drive the state machine.

    Trip a real breaker, let the sleep window elapse, then poll 20 times. If the
    route called `effective_state` or `acquire_slot`, the breaker would move to
    HALF_OPEN and arm a probe budget nobody requested.
    """
    from backend.schemas import ApiPolicy

    registry = client.jizo_state.registry
    policy = ApiPolicy(api_key=API, base_url="https://example.test")
    breaker = registry.get(policy)

    # Trip it with real failures, then pretend the sleep window has passed.
    for _ in range(policy.breaker_min_volume):
        breaker.record_failure()
    assert breaker.state is BreakerState.OPEN

    breaker._opened_at = breaker._clock() - policy.breaker_sleep_s - 1

    for _ in range(20):
        assert client.get("/breaker/state").status_code == 200

    assert breaker.state is BreakerState.OPEN, "a GET drove the breaker"
    assert breaker._probe_count == 0, "a GET armed a probe budget"


def test_breaker_state_reports_half_open_truthfully_without_acting(client):
    """After the sleep window the report says HALF_OPEN; the machine stays OPEN."""
    from backend.schemas import ApiPolicy

    registry = client.jizo_state.registry
    policy = ApiPolicy(api_key=API, base_url="https://example.test")
    breaker = registry.get(policy)
    for _ in range(policy.breaker_min_volume):
        breaker.record_failure()
    breaker._opened_at = breaker._clock() - policy.breaker_sleep_s - 1

    body = client.get("/breaker/state").json()
    assert body["states"][API] == "HALF_OPEN"
    assert breaker.state is BreakerState.OPEN


# ---------------------------------------------------------------------------
# Group 4 - the in-process value cache
# ---------------------------------------------------------------------------


def test_cache_stores_and_recalls_a_live_answer():
    state = AppState()
    state.remember("weather:Delhi", {"temp": 21})

    assert state.recall("weather:Delhi") == {"temp": 21}
    assert state.recall("geocode:Delhi") is None, "a miss must be None, not an error"


def test_cache_is_bounded_and_evicts_the_oldest():
    state = AppState()
    for i in range(VALUE_CACHE_MAX + 10):
        state.remember(f"k{i}", i)

    assert len(state.cache) <= VALUE_CACHE_MAX
    assert "k0" not in state.cache, "oldest insertion should be evicted"
    assert f"k{VALUE_CACHE_MAX + 9}" in state.cache


def test_cache_does_not_overwrite_an_existing_key():
    """A stale remembered value must not replace a fresher one."""
    state = AppState()
    state.remember("k", "first")
    state.remember("k", "second")
    assert state.recall("k") == "first"


def test_ladder_rungs_are_cache_then_default_then_message():
    """Ordering LIVE -> CACHE -> DEFAULT -> MESSAGE is preserved.

    LIVE is not a rung: P2 tries the live upstream before walking the ladder.
    """
    state = AppState()
    state.remember("weather:Delhi", "cached")

    ladder = state.ladder_for("weather:Delhi", default="default")
    assert ladder.resolve("down").served_from is ServedFrom.CACHE

    empty = state.ladder_for("geocode:Delhi", default="default")
    assert empty.resolve("down").served_from is ServedFrom.DEFAULT

    nothing = state.ladder_for("payment:x", default=None)
    assert nothing.resolve("down").served_from is ServedFrom.MESSAGE


def test_a_broken_cache_lookup_propagates_rather_than_masking(monkeypatch):
    """P2's contract: a failing provider is a visible bug, not a silent message."""
    state = AppState()

    def _boom():
        raise RuntimeError("cache exploded")

    ladder = state.ladder_for("k")
    ladder.cache = _boom

    with pytest.raises(RuntimeError, match="cache exploded"):
        ladder.resolve("down")


# ---------------------------------------------------------------------------
# Group 5 - evidence persistence
# ---------------------------------------------------------------------------


def test_persist_writes_one_row_per_event_with_the_right_mode(client):
    from backend.main import _persist_events

    events = [
        _event(phase=Phase.SEND, served_from=ServedFrom.NONE),
        _event(phase=Phase.RECV, served_from=ServedFrom.CACHE),
    ]
    session = client.fake_session

    written = asyncio.run(_persist_events(session, events, mode="control"))

    assert written == 2
    assert len(session.added) == 2
    assert all(r.mode == "control" for r in session.added)
    assert all(r.trace_id == "t-1" for r in session.added)


def test_persist_carries_the_five_scorer_fields(client):
    """A row missing these could never be re-scored - the table's whole point."""
    from backend.main import _persist_events

    event = _event(
        effect_applied=True, leaked_raw_error=True,
        fault=FaultType.HTTP_500, served_from=ServedFrom.CACHE,
    )
    session = client.fake_session
    asyncio.run(_persist_events(session, [event], mode="experiment"))

    row = session.added[0]
    assert row.effect_applied is True
    assert row.leaked_raw_error is True
    assert row.fault == "http_500"
    assert row.served_from == "cache"
    assert row.call_index == 1


def test_persist_with_no_events_is_a_no_op(client):
    from backend.main import _persist_events

    session = client.fake_session
    assert asyncio.run(_persist_events(session, [], mode="control")) == 0
    assert session.added == []


def test_load_arm_filters_by_trace_api_and_mode(client):
    """The mode split is what makes the comparison possible at all."""
    from backend.main import _load_arm

    rows = [
        _row(trace_id="t-1", api_key=API, mode="control"),
        _row(trace_id="t-1", api_key=API, mode="experiment"),
        _row(trace_id="t-1", api_key="geocode", mode="experiment"),
        _row(trace_id="t-2", api_key=API, mode="experiment"),
    ]
    client.fake_session.rows = rows

    loaded = asyncio.run(_load_arm(client.fake_session, "t-1", API, "control"))
    assert len(loaded) == 1
    assert all(isinstance(e, EvidenceEvent) for e in loaded)
    assert loaded[0].served_from is ServedFrom.LIVE


def test_load_arm_returns_empty_for_an_unknown_trace(client):
    from backend.main import _load_arm

    client.fake_session.rows = []
    assert asyncio.run(_load_arm(client.fake_session, "nope", API, "control")) == []


# ---------------------------------------------------------------------------
# Group 6 - /route/plan
# ---------------------------------------------------------------------------


def _stub_resilient(monkeypatch, served=ServedFrom.LIVE, breaker_wired=True,
                    attempts=1, attempts_by_api=None, fail=False):
    """Replace resilient_get so no socket is opened and calls are recorded.

    Crucially this stub writes to the **bus it was handed**, exactly as P2's
    real proxy does. An earlier version returned hand-built EvidenceEvents and
    left the bus empty, which made every "was the evidence persisted?" test fail
    for the wrong reason - `main.py` correctly persists what the bus recorded,
    so a stub that records nothing is not the real thing.
    """
    calls: list[dict] = []

    async def _fake(api_key, url, **kwargs):
        calls.append({"api_key": api_key, "url": url, **kwargs})
        bus = kwargs.get("bus")
        trace_id = kwargs.get("trace_id", "t-stub")

        def _record(phase, served_from, **extra):
            if bus is None:
                return None
            return bus.record(trace_id, api_key, phase,
                              served_from=served_from, **extra)

        # Write to the bus the real proxy would have written to.
        _record(Phase.SEND, ServedFrom.NONE)
        if served is ServedFrom.NONE:
            _record(Phase.RECV, ServedFrom.NONE, fault=FaultType.HTTP_500,
                    status_code=500)
        else:
            _record(Phase.RECV, served)

        if fail:
            raise RuntimeError("upstream unreachable")

        this_attempts = (
            (attempts_by_api or {}).get(api_key, attempts)
            if attempts_by_api
            else attempts
        )

        return ResilientResponse(
            api_key=api_key,
            trace_id=trace_id,
            data={"ok": True},
            served_from=served,
            status_code=200 if served is not ServedFrom.NONE else None,
            breaker_state=BreakerState.CLOSED if kwargs.get("breaker") else None,
            attempts=this_attempts,
            latency_ms=1.0,
            events=list(bus.events(trace_id)) if bus is not None else [],
        )

    monkeypatch.setattr("backend.main.resilient_get", _fake)
    return calls


def test_route_plan_returns_200_and_a_serialisable_body(client, monkeypatch):
    _stub_resilient(monkeypatch)

    response = client.post("/route/plan")
    assert response.status_code == 200

    body = response.json()
    assert body["mode"] == "experiment"
    assert set(body["result"]) == {"weather", "geocode"}
    assert body["trace_id"]


def test_route_plan_shares_one_trace_id_across_both_dependencies(client, monkeypatch):
    """One request, one trace - otherwise the two legs cannot be correlated."""
    calls = _stub_resilient(monkeypatch)

    body = client.post("/route/plan").json()
    trace_ids = {c["trace_id"] for c in calls}
    assert trace_ids == {body["trace_id"]}


def test_route_plan_uses_the_same_url_for_both_arms(client, monkeypatch):
    """A comparison where the arms asked different questions proves nothing."""
    calls = _stub_resilient(monkeypatch)
    client.post("/route/plan")

    by_api: dict[str, set] = {}
    for call in calls:
        by_api.setdefault(call["api_key"], set()).add(call["url"])
    for urls in by_api.values():
        assert len(urls) == 1, "control and experiment asked different URLs"


def test_route_plan_wires_a_breaker_only_on_the_experiment_arm(client, monkeypatch):
    calls = _stub_resilient(monkeypatch)
    client.post("/route/plan")

    wired = [c["api_key"] for c in calls if c.get("breaker") is not None]
    assert sorted(wired) == ["geocode", "weather"], "both arms ran protected"


def test_route_plan_rejects_an_unknown_mode(client, monkeypatch):
    _stub_resilient(monkeypatch)

    response = client.post("/route/plan", params={"mode": "experimentational"})
    assert response.status_code == 422, "a typo must be a 422, not a 500"


def test_route_plan_uses_the_protected_arm_by_default(client, monkeypatch):
    _stub_resilient(monkeypatch)
    assert client.post("/route/plan").json()["mode"] == "experiment"


def test_route_plan_creates_no_fi_runs_row(client, monkeypatch):
    """Ordinary traffic is not a drill - fi_runs stays for /fi/run."""
    _stub_resilient(monkeypatch)
    client.post("/route/plan")

    from backend.models import FiRunRow

    assert not any(isinstance(r, FiRunRow) for r in client.fake_session.added)


def test_route_plan_persists_both_arms_under_their_own_modes(client, monkeypatch):
    """Both modes must be present, and each row must carry its OWN arm's mode.

    This test used to assert the opposite - that every row carried the single
    requested mode - which silently certified the bug where both arms' evidence
    was written under one label. With that bug, the experiment arm's rows were
    stored as `control`, so `_load_arm(..., "experiment")` returned nothing and
    the comparison read "control had 2 calls, experiment had 0".
    """
    _stub_resilient(monkeypatch)
    client.post("/route/plan", params={"mode": "control"})

    rows = client.fake_session.added
    assert rows, "no evidence rows were written"

    modes = {r.mode for r in rows}
    assert modes == {"control", "experiment"}, (
        f"both arms must be persisted, got {modes}"
    )

    # Each mode must carry both dependencies: the arms differ by protection,
    # not by which upstream was called.
    for mode in ("control", "experiment"):
        apis = {r.api_key for r in rows if r.mode == mode}
        assert apis == {"weather", "geocode"}, f"{mode} missing a dependency"


def test_route_plan_persists_the_requested_arm_regardless_of_mode(
    client, monkeypatch
):
    """Serving control must not change what gets stored for the other arm."""
    _stub_resilient(monkeypatch)
    client.post("/route/plan", params={"mode": "experiment"})

    modes = {r.mode for r in client.fake_session.added}
    assert modes == {"control", "experiment"}


def test_route_plan_comparison_reads_both_arms_from_storage(client, monkeypatch):
    """The end-to-end proof: both arms must come back populated.

    Guards against a regression where the rows exist but the mode filter still
    cannot separate them, which is what made the delta meaningless.
    """
    _stub_resilient(monkeypatch)
    body = client.post("/route/plan").json()

    for api_key, report in body["comparison"].items():
        assert report["control"]["calls"] > 0, f"{api_key}: control arm is empty"
        assert report["experiment"]["calls"] > 0, f"{api_key}: experiment arm is empty"


def test_route_plan_comparison_reports_the_requested_arm(client, monkeypatch):
    """`compare_sides` must be told the arm, not left on DEFAULT_MODE."""
    _stub_resilient(monkeypatch)

    body = client.post("/route/plan", params={"mode": "control"}).json()
    assert body["comparison"]["weather"]["mode"] == "control"

    body = client.post("/route/plan", params={"mode": "experiment"}).json()
    assert body["comparison"]["weather"]["mode"] == "experiment"


def test_route_plan_trace_id_is_unique_and_well_formed(client, monkeypatch):
    """A uuid4 suffix, matching P2's `t-<hex>` convention.

    `id(request)` was the previous source: a CPython address, which is not
    unique across requests because a freed Request's address can be reused.
    """
    _stub_resilient(monkeypatch)

    first = client.post("/route/plan").json()["trace_id"]
    second = client.post("/route/plan").json()["trace_id"]

    assert first != second, "two requests shared a trace id"
    for trace_id in (first, second):
        assert trace_id.startswith("t-plan-")
        suffix = trace_id.split("-", 2)[2]
        assert len(suffix) == 12
        int(suffix, 16)  # raises if it is not hex


def test_route_plan_shares_one_trace_id_across_both_arms(client, monkeypatch):
    """One trace per HTTP request, even though there are two buses.

    Both arms must be correlatable by trace, or `request_logs` cannot answer
    "show me everything this request touched".
    """
    calls = _stub_resilient(monkeypatch)
    body = client.post("/route/plan").json()

    trace_ids = {c["trace_id"] for c in calls}
    assert trace_ids == {body["trace_id"]}

    saved = {r.trace_id for r in client.fake_session.added}
    assert saved == {body["trace_id"]}, "arms must persist under one trace"


def test_route_plan_uses_a_separate_bus_per_arm(client, monkeypatch):
    """Attribution must be structural, not a positional slice of one bus."""
    calls = _stub_resilient(monkeypatch)
    client.post("/route/plan")

    buses = {id(c["bus"]) for c in calls if c.get("bus") is not None}
    assert len(buses) == 2, "both arms shared one EvidenceBus"


def test_route_plan_records_the_real_attempt_count(client, monkeypatch):
    """`attempt` carries the call's TOTAL, not a per-row identifier.

    `EvidenceEvent` has no attempt field and the proxy reuses one `call_index`
    across retries, so no row can identify its own attempt. Recording the real
    total beats a hardcoded 1 - a call that took three tries no longer claims
    one - but it is call-level precision only.
    """
    _stub_resilient(monkeypatch, attempts=3)
    client.post("/route/plan")

    rows = client.fake_session.added
    assert rows, "no rows written"
    assert all(r.attempt == 3 for r in rows), "the real attempt count was lost"


def test_route_plan_keeps_each_dependency_attempt_count_distinct(
    client, monkeypatch
):
    """Weather's three tries must not be stamped onto geocode's one.

    The two upstreams genuinely differ: geocode is rate-limited to 1 rps and
    usually succeeds first time. Collapsing them with `max()` would make a
    clean geocode call look like it took three attempts.
    """
    _stub_resilient(monkeypatch, attempts_by_api={"weather": 3, "geocode": 1})
    client.post("/route/plan")

    by_api = {}
    for row in client.fake_session.added:
        by_api.setdefault(row.api_key, set()).add(row.attempt)

    assert by_api["weather"] == {3}, f"weather: {by_api['weather']}"
    assert by_api["geocode"] == {1}, f"geocode: {by_api['geocode']}"


def test_route_plan_attempt_counts_survive_per_arm(client, monkeypatch):
    """Both arms keep their own per-dependency counts, not just one arm."""
    _stub_resilient(monkeypatch, attempts_by_api={"weather": 2, "geocode": 1})
    client.post("/route/plan")

    for mode in ("control", "experiment"):
        rows = [r for r in client.fake_session.added if r.mode == mode]
        assert rows, f"{mode} arm wrote nothing"
        for row in rows:
            expected = 2 if row.api_key == "weather" else 1
            assert row.attempt == expected, (
                f"{mode}/{row.api_key} got attempt={row.attempt}"
            )


def test_route_plan_attempt_defaults_to_one_when_unknown(client, monkeypatch):
    """A dependency that raised has no real attempt count, so it records 1.

    `_attempts_by_api` skips a None attempts value, so the honest default is
    kept rather than a fabricated number.
    """
    from backend.main import _attempts_by_api

    results = {
        "weather": {"api_key": "weather", "attempts": None, "error": "RuntimeError"},
        "geocode": {"api_key": "geocode", "attempts": 2},
    }
    assert _attempts_by_api(results) == {"geocode": 2}
    assert _attempts_by_api({}) == {}


#: The core keys every dependency result must carry, success or failure.
CORE_RESULT_KEYS = {
    "api_key", "served_from", "status_code", "attempts",
    "latency_ms", "breaker_state", "data", "note",
}


def test_route_plan_success_and_failure_share_one_result_shape(
    client, monkeypatch
):
    """A consumer must not need a branch to read `attempts` or `latency_ms`.

    The failure shape used to be three keys against the success shape's eight,
    so a dashboard reading `result["weather"]["attempts"]` got a KeyError
    exactly when the upstream was down - the moment the panel matters most.
    """
    _stub_resilient(monkeypatch)
    healthy = client.post("/route/plan").json()["result"]

    async def _weather_down(api_key, url, **kwargs):
        if api_key == "weather":
            raise RuntimeError("upstream down")
        bus, tid = kwargs.get("bus"), kwargs.get("trace_id")
        bus.record(tid, api_key, Phase.SEND, served_from=ServedFrom.NONE)
        bus.record(tid, api_key, Phase.RECV, served_from=ServedFrom.LIVE)
        return ResilientResponse(
            api_key=api_key, trace_id=tid, data={"ok": True},
            served_from=ServedFrom.LIVE, status_code=200,
            breaker_state=BreakerState.CLOSED, attempts=1, latency_ms=1.0,
            events=[],
        )

    monkeypatch.setattr("backend.main.resilient_get", _weather_down)
    degraded = client.post("/route/plan").json()["result"]

    for api_key in ("weather", "geocode"):
        assert set(degraded[api_key]) >= CORE_RESULT_KEYS, (
            f"{api_key} failure is missing core keys: "
            f"{CORE_RESULT_KEYS - set(degraded[api_key])}"
        )
        assert set(healthy[api_key]) >= CORE_RESULT_KEYS


def test_route_plan_failure_populates_unavailable_fields_with_none(
    client, monkeypatch
):
    """None, not a missing key and not a fabricated value."""

    async def _weather_down(api_key, url, **kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr("backend.main.resilient_get", _weather_down)
    body = client.post("/route/plan").json()

    for api_key in ("weather", "geocode"):
        result = body["result"][api_key]
        assert result["served_from"] == "none"
        for field in ("status_code", "attempts", "latency_ms",
                      "breaker_state", "data", "note"):
            assert result[field] is None, f"{api_key}.{field} was not None"


def test_route_plan_failure_keeps_error_as_an_extra_field(client, monkeypatch):
    """`error` is additive, never a replacement for the core keys."""

    async def _weather_down(api_key, url, **kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr("backend.main.resilient_get", _weather_down)
    result = client.post("/route/plan").json()["result"]["weather"]

    assert result["error"] == "RuntimeError"
    assert set(result) - CORE_RESULT_KEYS == {"error"}


def test_route_plan_success_has_no_error_field(client, monkeypatch):
    _stub_resilient(monkeypatch)
    result = client.post("/route/plan").json()["result"]["weather"]
    assert "error" not in result


def test_route_plan_sends_the_configured_nominatim_user_agent(
    client, monkeypatch
):
    """The UA comes from P3's resolved config, not a hardcoded string.

    Nominatim's usage policy asks every client to identify itself with a
    contactable User-Agent, and P3 already resolves `NOMINATIM_UA` through its
    env > vault > .env > default chain.
    """
    import backend.main as main_mod

    calls = _stub_resilient(monkeypatch)
    client.post("/route/plan")

    geocode = [c for c in calls if c["api_key"] == "geocode"]
    weather = [c for c in calls if c["api_key"] == "weather"]
    assert geocode and weather

    configured = main_mod.get_config().nominatim_ua
    assert configured, "P3 resolved an empty User-Agent"
    for call in geocode:
        assert call["headers"]["User-Agent"] == configured
    for call in weather:
        assert call["headers"] is None, "weather needs no User-Agent"


def test_route_plan_persists_both_arms_even_when_one_arm_explodes(
    client, monkeypatch
):
    """Regression: the `finally` used to reference names bound inside `try`.

    If the control arm raised, `control` and `control_bus_used` were unbound in
    the handler, so it raised UnboundLocalError and replaced the real error.
    The bus objects live outside the `try` precisely so persistence cannot be
    skipped.
    """
    calls: list[str] = []

    async def _explodes_on_control(api_key, url, **kwargs):
        bus, tid = kwargs.get("bus"), kwargs.get("trace_id")
        breaker = kwargs.get("breaker")
        if breaker is None:
            # Control arm: raise AFTER writing evidence, then blow up in the
            # results loop by returning an object with a hostile attribute.
            bus.record(tid, api_key, Phase.SEND, served_from=ServedFrom.NONE)
            bus.record(tid, api_key, Phase.RECV, served_from=ServedFrom.LIVE)

            class Hostile:
                served_from = property(
                    lambda self: (_ for _ in ()).throw(
                        RuntimeError("results loop exploded")
                    )
                )

            return Hostile()
        calls.append("experiment")
        bus.record(tid, api_key, Phase.SEND, served_from=ServedFrom.NONE)
        bus.record(tid, api_key, Phase.RECV, served_from=ServedFrom.LIVE)
        return ResilientResponse(
            api_key=api_key, trace_id=tid, data={"ok": True},
            served_from=ServedFrom.LIVE, status_code=200,
            breaker_state=BreakerState.CLOSED, attempts=1, latency_ms=1.0,
            events=[],
        )

    monkeypatch.setattr("backend.main.resilient_get", _explodes_on_control)

    with pytest.raises(RuntimeError) as excinfo:
        client.post("/route/plan")

    # The ORIGINAL error must surface, not UnboundLocalError.
    assert not isinstance(excinfo.value, NameError), (
        f"the finally block masked the real error: {excinfo.value!r}"
    )
    assert "exploded" in str(excinfo.value)

    # And the experiment arm's evidence was still persisted.
    modes = {r.mode for r in client.fake_session.added}
    assert "experiment" in modes, "the finished arm was not persisted"


def test_route_plan_answers_when_one_dependency_explodes(client, monkeypatch):
    """One dead upstream must not lose the other dependency's answer."""

    async def _half_dead(api_key, url, **kwargs):
        if api_key == "weather":
            raise RuntimeError("upstream down")
        return ResilientResponse(
            api_key=api_key, trace_id=kwargs.get("trace_id", "t"),
            data={"ok": True}, served_from=ServedFrom.CACHE, status_code=None,
            breaker_state=None, attempts=3, latency_ms=5.0, events=[],
        )

    monkeypatch.setattr("backend.main.resilient_get", _half_dead)

    body = client.post("/route/plan").json()
    assert body["result"]["weather"]["served_from"] == "none"
    assert body["result"]["geocode"]["served_from"] == "cache"


def test_route_plan_remembers_a_live_answer_for_the_fallback(client, monkeypatch):
    _stub_resilient(monkeypatch, served=ServedFrom.LIVE)
    client.post("/route/plan", params={"address": "Pune"})

    assert client.jizo_state.recall("weather:Pune") == {"ok": True}


def test_route_plan_does_not_remember_a_fallback_answer(client, monkeypatch):
    """Only LIVE populates the cache, or the cache fills with stale data."""
    _stub_resilient(monkeypatch, served=ServedFrom.CACHE)
    client.post("/route/plan", params={"address": "Chennai"})

    assert client.jizo_state.recall("weather:Chennai") is None


def test_route_plan_includes_a_per_api_comparison(client, monkeypatch):
    _stub_resilient(monkeypatch)
    body = client.post("/route/plan").json()

    assert set(body["comparison"]) == {"weather", "geocode"}
    for report in body["comparison"].values():
        assert set(report) == {"mode", "control", "experiment", "delta"}


# ---------------------------------------------------------------------------
# Group 7 - /fi/run and /fi/runs/{id}
# ---------------------------------------------------------------------------


def _fi_body(**overrides) -> dict:
    from backend.schemas import GuardAfter, FaultTarget, Pattern

    body = dict(
        run_id="run-test-1",
        pattern=Pattern.K_OF_N.value,
        fault=FaultType.HTTP_500.value,
        target=dict(api_key=API, phase=Phase.RECV.value, occurrence=1),
        guard=dict(api_key=API, phase=Phase.SEND.value, min_count=1),
        total_occurrences=1,
        idempotent=True,
    )
    body.update(overrides)
    return body


def test_fi_run_rejects_a_malformed_request(client):
    """Bad input is rejected at the door, not deep in the drill."""
    assert client.post("/fi/run", json={"run_id": "x"}).status_code == 422


def test_fi_run_records_intent_before_the_call(client, monkeypatch):
    """A run that dies mid-flight must still leave a row saying what it wanted."""
    from backend.models import FiRunRow

    async def _boom(api_key, url, **kwargs):
        raise RuntimeError("upstream unreachable")

    monkeypatch.setattr("backend.main.resilient_get", _boom)

    client.post("/fi/run", json=_fi_body())

    intents = [r for r in client.fake_session.added if isinstance(r, FiRunRow)]
    assert len(intents) == 1, "intent was not recorded"
    assert intents[0].ts is None, "intent must not carry a verdict yet"


def test_fi_run_reports_502_when_the_call_fails(client, monkeypatch):
    async def _boom(api_key, url, **kwargs):
        raise RuntimeError("upstream unreachable")

    monkeypatch.setattr("backend.main.resilient_get", _boom)

    response = client.post("/fi/run", json=_fi_body())
    assert response.status_code == 502
    assert "RuntimeError" in response.json()["detail"]


def test_fi_run_returns_the_verdict_from_p1_not_a_local_copy(client, monkeypatch):
    """The route must not re-derive the TS conjunction itself.

    Fed a fault-free trace, P1's scorer says miss=True so ts=False. If this
    route computed its own verdict it would disagree - which is the bug the
    route-by-route test exists to prevent.
    """
    async def _no_fault(api_key, url, **kwargs):
        return ResilientResponse(
            api_key=api_key, trace_id=kwargs.get("trace_id", "t"),
            data={"ok": True}, served_from=ServedFrom.LIVE, status_code=200,
            breaker_state=BreakerState.CLOSED, attempts=1, latency_ms=1.0,
            events=[
                EvidenceEvent(
                    trace_id=kwargs.get("trace_id", "t"), api_key=api_key,
                    phase=Phase.SEND, served_from=ServedFrom.NONE,
                    occurrence=1, call_index=1,
                ),
                EvidenceEvent(
                    trace_id=kwargs.get("trace_id", "t"), api_key=api_key,
                    phase=Phase.RECV, served_from=ServedFrom.LIVE,
                    occurrence=1, call_index=1,
                ),
            ],
        )

    monkeypatch.setattr("backend.main.resilient_get", _no_fault)

    body = client.post("/fi/run", json=_fi_body()).json()

    assert set(body) >= {"ts", "cw", "ps", "prem", "miss", "mult"}
    assert body["miss"] is True
    assert body["ts"] is False, "the route must agree with P1, not override it"


def test_fi_run_passes_the_spec_to_the_proxy(client, monkeypatch):
    calls = _stub_resilient(monkeypatch)
    client.post("/fi/run", json=_fi_body())

    spec = calls[0]["spec"]
    assert isinstance(spec, DrillSpec)
    assert spec.run_id == "run-test-1"
    assert spec.target.api_key == API


# ---------------------------------------------------------------------------
# Group 7b - /fi/run owns total_occurrences
# ---------------------------------------------------------------------------


def test_fi_run_with_total_occurrences_one_makes_exactly_one_call(
    client, monkeypatch
):
    """n=1 must behave as the single-call version did - no drift."""
    calls = _stub_resilient(monkeypatch)
    body = _fi_body()
    body["total_occurrences"] = 1

    client.post("/fi/run", json=body)

    assert len(calls) == 1, "n=1 must not loop"


def test_fi_run_executes_every_occurrence(client, monkeypatch):
    """n=3 means three logical calls, so the guard can ever reach call 3."""
    calls = _stub_resilient(monkeypatch)
    body = _fi_body()
    body["total_occurrences"] = 3

    client.post("/fi/run", json=body)

    assert len(calls) == 3, f"expected 3 occurrences, got {len(calls)}"


def test_fi_run_executes_four_occurrences_for_the_headline_drill(
    client, monkeypatch
):
    """The headline k-of-n case: 4 of 4."""
    calls = _stub_resilient(monkeypatch)
    body = _fi_body()
    body["total_occurrences"] = 4

    client.post("/fi/run", json=body)

    assert len(calls) == 4


def test_fi_run_occurrences_share_one_trace_and_one_bus(client, monkeypatch):
    """The scorer must see call_index 1..N, so the calls must be linked.

    Two separate buses would restart the counter at 1 every time and "call 3"
    would never exist.
    """
    calls = _stub_resilient(monkeypatch)
    body = _fi_body()
    body["total_occurrences"] = 3

    client.post("/fi/run", json=body)

    assert len({c["trace_id"] for c in calls}) == 1
    assert len({id(c["bus"]) for c in calls}) == 1, "occurrences shared a bus"
    assert all(c["bus"] is not None for c in calls)


def test_fi_run_occurrences_advance_call_index_one_at_a_time(
    client, monkeypatch
):
    """The guard reads call_count, so it must reach k for a k-of-n drill."""
    from backend.faults import EvidenceBus

    calls = _stub_resilient(monkeypatch)
    body = _fi_body()
    body["total_occurrences"] = 3

    client.post("/fi/run", json=body)

    bus = calls[0]["bus"]
    call_indexes = sorted({e.call_index for e in bus.events("run-test-1")})
    assert call_indexes == [1, 2, 3]


def test_fi_run_retries_do_not_increment_the_drill_call_index(
    client, monkeypatch
):
    """A retry is not another occurrence.

    The stub writes THREE sends for one logical call but passes one call_index,
    exactly as P2's proxy does. The drill must still see a single occurrence,
    so three such calls must yield call_index 1..3, not 1..9.
    """
    async def _three_attempts_one_call(api_key, url, **kwargs):
        bus, tid = kwargs.get("bus"), kwargs.get("trace_id")
        # P2's exact retry shape: the first SEND advances call_index, and
        # every retry passes that call_index back so the counter does NOT move.
        # Without the explicit pass-back this stub would inflate the counter
        # and the test would be measuring the wrong thing.
        call_index = None
        for _ in range(3):
            ev = bus.record(tid, api_key, Phase.SEND,
                            served_from=ServedFrom.NONE, call_index=call_index)
            call_index = ev.call_index
            bus.record(tid, api_key, Phase.RECV, served_from=ServedFrom.NONE,
                       call_index=call_index,
                       fault=FaultType.HTTP_500, status_code=500)
        return ResilientResponse(
            api_key=api_key, trace_id=tid, data=None,
            served_from=ServedFrom.MESSAGE, status_code=None,
            breaker_state=None, attempts=3, latency_ms=9.0, events=[],
        )

    monkeypatch.setattr("backend.main.resilient_get", _three_attempts_one_call)

    body = _fi_body()
    body["total_occurrences"] = 3
    client.post("/fi/run", json=body)

    # 3 occurrences x 3 sends = 9 rows, but only 3 distinct calls.
    # 3 occurrences x 3 attempts x (SEND + RECV) = 18 evidence rows, but only
    # 3 logical calls. If retries were being counted as occurrences this would
    # be 9 distinct call_index values instead of 3.
    from backend.models import RequestLogRow

    rows = [
        r for r in client.fake_session.added
        if isinstance(r, RequestLogRow)
    ]
    assert len(rows) == 18, f"expected 18 evidence rows, got {len(rows)}"
    assert sorted({r.call_index for r in rows}) == [1, 2, 3], (
        "retries were counted as extra drill occurrences"
    )


def test_fi_run_scores_once_after_all_occurrences(client, monkeypatch):
    """One score_run over the whole drill, never one per occurrence.

    Grading each occurrence separately would make every run a single-call run,
    and `miss` would be true for a k-of-n drill whose fault lands on call 3.
    """
    scores: list[int] = []
    import backend.main as main_mod

    real_score = main_mod.score_run

    def _counting_score(spec, events):
        scores.append(len(events))
        return real_score(spec, events)

    monkeypatch.setattr(main_mod, "score_run", _counting_score)

    _stub_resilient(monkeypatch)
    body = _fi_body()
    body["total_occurrences"] = 3
    client.post("/fi/run", json=body)

    assert len(scores) == 1, f"score_run called {len(scores)} times, expected 1"
    # 3 occurrences x (SEND + RECV) = 6 rows in one grading pass.
    assert scores[0] == 6


def test_fi_run_persists_every_occurrences_evidence(client, monkeypatch):
    calls = _stub_resilient(monkeypatch)
    body = _fi_body()
    body["total_occurrences"] = 3

    client.post("/fi/run", json=body)

    from backend.models import RequestLogRow

    rows = [r for r in client.fake_session.added if isinstance(r, RequestLogRow)]
    assert len(rows) == 6, f"expected 6 evidence rows, got {len(rows)}"
    assert {r.trace_id for r in rows} == {"run-test-1"}
    assert all(r.mode == "experiment" for r in rows)


def test_fi_run_stores_total_occurrences_on_the_row(client, monkeypatch):
    from backend.models import FiRunRow

    _stub_resilient(monkeypatch)
    body = _fi_body()
    body["total_occurrences"] = 3

    client.post("/fi/run", json=body)

    intents = [r for r in client.fake_session.added if isinstance(r, FiRunRow)]
    assert intents[0].total_occurrences == 3


def test_fi_run_multi_occurrence_can_grade_ts_true(client, monkeypatch):
    """The headline case must be reachable through the API now.

    k-of-n, k=2 of n=3: calls 1 and 3 healthy, call 2 takes the fault. Only a
    route that executes all three occurrences can produce this verdict - the
    single-call version scored miss=True.
    """
    async def _k_of_n(api_key, url, **kwargs):
        bus, tid = kwargs.get("bus"), kwargs.get("trace_id")
        from backend.faults import GuardEvaluator

        spec = kwargs["spec"]
        guard = GuardEvaluator(bus)
        send = bus.record(tid, api_key, Phase.SEND, served_from=ServedFrom.NONE)
        decision = guard.should_fire(tid, spec.target, spec.guard)

        if decision.should_fire:
            bus.record(tid, api_key, Phase.RECV, served_from=ServedFrom.NONE,
                       call_index=send.call_index, fault=spec.fault,
                       status_code=500)
            bus.record(tid, api_key, Phase.RECV, served_from=ServedFrom.CACHE,
                       call_index=send.call_index)
        else:
            bus.record(tid, api_key, Phase.RECV, served_from=ServedFrom.LIVE,
                       call_index=send.call_index, status_code=200)
        return ResilientResponse(
            api_key=api_key, trace_id=tid, data={"ok": True},
            served_from=ServedFrom.LIVE, status_code=200,
            breaker_state=BreakerState.CLOSED, attempts=1, latency_ms=1.0,
            events=[],
        )

    monkeypatch.setattr("backend.main.resilient_get", _k_of_n)

    from backend.schemas import Pattern

    body = _fi_body(
        pattern=Pattern.K_OF_N.value,
        target=dict(api_key=API, phase=Phase.RECV.value, occurrence=2),
        total_occurrences=3,
    )
    body["run_id"] = "run-k2"
    response = client.post("/fi/run", json=body).json()

    assert response["ts"] is True, (
        f"k-of-n k=2 of 3 must pass: {response['explain']} / {response['notes']}"
    )
    assert response["miss"] is False


def test_fi_run_fault_still_lands_on_the_targeted_occurrence(
    client, monkeypatch
):
    """With n occurrences the fault must land on k, not on every call."""
    fired_on: list[int] = []

    async def _k_of_n(api_key, url, **kwargs):
        bus, tid = kwargs.get("bus"), kwargs.get("trace_id")
        from backend.faults import GuardEvaluator

        spec = kwargs["spec"]
        send = bus.record(tid, api_key, Phase.SEND, served_from=ServedFrom.NONE)
        if GuardEvaluator(bus).should_fire(tid, spec.target, spec.guard).should_fire:
            fired_on.append(send.call_index)
            bus.record(tid, api_key, Phase.RECV, served_from=ServedFrom.NONE,
                       call_index=send.call_index, fault=spec.fault)
            bus.record(tid, api_key, Phase.RECV, served_from=ServedFrom.CACHE,
                       call_index=send.call_index)
        else:
            bus.record(tid, api_key, Phase.RECV, served_from=ServedFrom.LIVE,
                       call_index=send.call_index)
        return ResilientResponse(
            api_key=api_key, trace_id=tid, data=None,
            served_from=ServedFrom.LIVE, status_code=200,
            breaker_state=BreakerState.CLOSED, attempts=1, latency_ms=1.0,
            events=[],
        )

    monkeypatch.setattr("backend.main.resilient_get", _k_of_n)

    from backend.schemas import Pattern

    body = _fi_body(
        pattern=Pattern.K_OF_N.value,
        target=dict(api_key=API, phase=Phase.RECV.value, occurrence=3),
        total_occurrences=4,
    )
    body["run_id"] = "run-k3"
    client.post("/fi/run", json=body)

    assert fired_on == [3], f"fault fired on {fired_on}, expected only call 3"


def test_fi_run_detail_returns_404_for_an_unknown_run(client):
    assert client.get("/fi/runs/never-existed").status_code == 404


def test_fi_run_detail_separates_unfinished_from_unknown(client, monkeypatch):
    """P3's load_run returns None for both; the route must not hide that.

    An unfinished run is information - the FI console shows "started, never
    finished" rather than a bare 404.
    """
    from backend.models import FiRunRow

    async def _none(session, run_id):
        return None

    monkeypatch.setattr("backend.main.load_run", _none)
    monkeypatch.setattr(
        "backend.main.load_run_evidence",
        lambda session, run_id: asyncio.sleep(0, result=[]),
    )
    client.fake_session.run_rows["started-not-finished"] = FiRunRow(
        run_id="started-not-finished", pattern="k_of_n", target_api=API,
        target_k=1, target_phase="recv", fault="http_500",
    )

    body = client.get("/fi/runs/started-not-finished").json()
    assert body["found"] is False
    assert body["finished"] is False


def test_fi_run_detail_returns_a_stored_verdict(client, monkeypatch):
    from backend import k_of_n_drill
    from backend.scoring import score_run

    bus_like = [
        EvidenceEvent(trace_id="t", api_key=API, phase=Phase.SEND,
                      served_from=ServedFrom.NONE, occurrence=1, call_index=1),
        EvidenceEvent(trace_id="t", api_key=API, phase=Phase.RECV,
                      served_from=ServedFrom.LIVE, occurrence=1, call_index=1),
    ]
    spec = k_of_n_drill("stored-1", API, k=1, n=1)
    stored = score_run(spec, bus_like)

    async def _load(session, run_id):
        return stored if run_id == "stored-1" else None

    monkeypatch.setattr("backend.main.load_run", _load)

    body = client.get("/fi/runs/stored-1").json()
    assert body["found"] is True
    assert body["finished"] is True
    assert body["ts"] == stored.ts
    assert "TS=" in body["explain"]


def test_fi_run_detail_omits_the_timeline_by_default(client, monkeypatch):
    """The timeline can be large; it is opt-in."""
    from backend import k_of_n_drill
    from backend.scoring import score_run

    spec = k_of_n_drill("stored-2", API, k=1, n=1)
    stored = score_run(spec, [])

    async def _load(session, run_id):
        return stored

    monkeypatch.setattr("backend.main.load_run", _load)

    assert "timeline" not in client.get("/fi/runs/stored-2").json()
    assert "timeline" in client.get(
        "/fi/runs/stored-2", params={"include_evidence": True}
    ).json()


# ---------------------------------------------------------------------------
# Group 8 - the dashboard hub
# ---------------------------------------------------------------------------


def test_hub_starts_with_no_clients():
    hub = DashboardHub()
    assert hub.history == []


def test_hub_retains_a_bounded_history():
    hub = DashboardHub()
    hub.history_max = 3
    for i in range(6):
        hub.history.append({"n": i})
        if len(hub.history) > 3:
            hub.history.pop(0)

    assert len(hub.history) == 3
    assert hub.history[-1]["n"] == 5


def test_hub_disconnect_is_idempotent():
    hub = DashboardHub()
    asyncio.run(hub.disconnect(object()))
    asyncio.run(hub.disconnect(object()))


def test_websocket_route_accepts_a_connection():
    with TestClient(app) as test_client:
        with test_client.websocket_connect("/ws/dashboard") as socket:
            snapshot = socket.receive_json()
            assert snapshot["type"] == "snapshot"
            assert "events" in snapshot["payload"]


def test_websocket_receives_broadcasts():
    with TestClient(app) as test_client:
        with test_client.websocket_connect("/ws/dashboard") as socket:
            socket.receive_json()  # the snapshot

            hub = DashboardHub()
            asyncio.run(hub.broadcast("plan", {"trace_id": "t-9"}))

            # hub is a local instance; assert the global one is what broadcast
            from backend.main import hub as global_hub

            asyncio.run(global_hub.broadcast("plan", {"trace_id": "t-10"}))
            event = socket.receive_json()
            assert event["type"] == "plan"
            assert event["payload"]["trace_id"] == "t-10"


# ---------------------------------------------------------------------------
# Group 9 - containment
# ---------------------------------------------------------------------------


def test_main_does_not_reimplement_the_ts_conjunction():
    """The route must read P1's verdict, never rebuild the conjunction."""
    import inspect

    from backend import main as main_mod

    source = inspect.getsource(main_mod)
    assert "correct_withstand and" not in source
    assert "policy_success and" not in source


def test_main_does_not_import_axes_or_the_demo_harness():
    """Those are Pushkar's files. main.py must not reach for them."""
    import ast
    import inspect

    from backend import main as main_mod

    tree = ast.parse(inspect.getsource(main_mod))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    assert not any("axes" in m for m in modules)
    assert not any("demo" in m for m in modules)


def test_main_uses_p3_store_rather_than_raw_sql_for_writes():
    """Evidence must go through event_to_row so the scorer fields are kept."""
    import inspect

    from backend import main as main_mod

    source = inspect.getsource(main_mod)
    assert "event_to_row" in source
    assert "RequestLogRow(" not in source, "main.py must not hand-build a row"
