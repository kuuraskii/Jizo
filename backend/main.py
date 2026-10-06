"""
JIZO - Part 4 FastAPI application.

This is where the parts meet. P1 decides whether a drill behaved correctly,
P2 protects a live call, P3 remembers what happened, and this file wires them
to HTTP and WebSockets so a judge can watch it happen.

    POST /route/plan      fan out weather + geocode, protected or unprotected
    POST /fi/run          run one drill through the real protector, grade it
    GET  /fi/runs/{id}    read a stored verdict back
    GET  /breaker/state   read breaker state, WITHOUT acting on it
    GET  /health          P3's health report
    GET  /ready           P3's readiness report (503 when not ready)
    WS   /ws/dashboard    minimal broadcast for P5

## The one design rule this file is careful about

**Nothing here re-implements another part.**

* Verdicts come from ``backend.scoring.score_run`` and nowhere else. This file
  never writes the CW/PS/Prem/Miss/Mult conjunction itself, so the dashboard
  and the grader cannot disagree about what TS means.
* Protection comes from ``backend.proxy.resilient_get``. This file does not
  retry, does not sleep, and does not inspect a breaker to decide anything.
* Persistence comes from ``backend.store`` and ``backend.models``. This file
  writes evidence rows with the *existing* ``event_to_row`` mapper and reads
  with the *existing* ``row_to_event``. No private P3 helper is used and no
  P3 file is modified.
* The comparison comes from ``backend.compare``.

## Two decisions worth defending

**1. ``/route/plan`` persists evidence without inventing a drill.**

Ordinary traffic is not a drill: there is no ``DrillSpec``, so there is no
``fi_runs`` row to write. Fabricating one would pollute the proof log with
rows no judge asked for. So ``/route/plan`` writes its ``EvidenceEvent`` rows
straight into ``request_logs`` via the existing mapper, each tagged with the
``mode`` that produced it. ``fi_runs`` stays reserved for ``/fi/run``.

That is also what makes the control-vs-experiment comparison possible at all:
``request_logs.mode`` is the only place the two arms are distinguishable, and
P3 built that column and index specifically for this.

**2. The value cache is in-process and owned here, deliberately.**

``proxy.py``'s ``FallbackLadder`` docstring assigns the ``cache`` rung to P3 and
``default`` to P4. P3 delivered four tables - registry, breaker timeline, drill
proofs, evidence logs - and none of them is a value cache, so there is nothing
to call. Rather than add a table to another team's slice or silently drop the
rung, this file keeps a small bounded dict populated from successful LIVE
responses.

The honest limitation, which the demo must not hide: **an in-process cache is
empty after a restart and does not survive a second worker.** Nominatim is also
a shared community service whose usage policy asks for cached responses, so a
shared cache is the right long-term answer - but that is P3's table or P5's
call, not something to smuggle in here.

## Lifespan: a database outage must not take down /health

Registry warming is best-effort by design. If Postgres is unreachable at
startup the app still starts, the failure is logged, and P3's existing contract
does the rest: ``/health`` reports ``unhealthy`` and ``/ready`` returns 503
with a reason. A health endpoint that dies because the database is down cannot
report that the database is down - which is the single most useful thing it has
to say.

Similarly ``/breaker/state`` is strictly a *read*. It uses
``BreakerRegistry.states()``, which reports the effective state **without**
performing the OPEN -> HALF_OPEN transition, so a dashboard polling once a
second cannot arm a probe budget no caller requested (edgecases BR-30, BR-40).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote

import httpx
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .breaker import BreakerRegistry
from .compare import CompareResult, compare_sides, resolve_mode
from .config import load_policy
from .db import dispose_engine, get_session, get_sessionmaker
from .faults import EvidenceBus
from .health import check_health, check_ready
from .logging_conf import configure_logging, get_logger
from .models import FiRunRow, RequestLogRow
from .proxy import FallbackLadder, resilient_get
from .schemas import DrillSpec, EvidenceEvent, FiRun, ScoreResult, ServedFrom
from .scoring import score_run
from .secrets import get_config
from .store import (
    event_to_row,
    load_run,
    load_run_evidence,
    load_registry,
    register_registry,
    save_run,
    spec_to_row,
)

logger = logging.getLogger("jizo.main")


# ---------------------------------------------------------------------------
# Application state
#
# Held on `app.state` rather than as module globals. A module-level dict would
# be exactly the global-singleton leak P1 avoided with EvidenceBus, and it
# would make the app impossible to instantiate twice in one test process.
# ---------------------------------------------------------------------------


#: Cap on the in-process value cache. Small on purpose: this is a demo aid, not
#: a tier, and an unbounded dict in a long-lived server is a slow leak.
VALUE_CACHE_MAX = 256

#: Upper bound on one drill's logical occurrence count. ``total_occurrences`` is
#: caller-supplied and every occurrence is a real protected upstream call, so an
#: unbounded value lets a single request tie up the worker with millions of
#: calls. The demo never needs more than a handful; this only stops abuse.
MAX_DRILL_OCCURRENCES = 100


class AppState:
    """Everything the lifespan creates and the routes read.

    Attributes:
        registry: P2's per-api breaker registry, shared by every request.
        client: one ``httpx.AsyncClient`` for the whole process. Opening a
            client per request loses connection pooling, and P2's proxy accepts
            ``client=`` precisely so a caller can supply a long-lived one.
        cache: the in-process fallback value cache. See the module docstring.
        registry_warmed: whether P3's policies reached P2. A ``False`` here is
            what makes ``/ready`` honest when the database was down at boot.
        registry_error: why warming failed, for the log and for diagnostics.
    """

    def __init__(self) -> None:
        self.registry: BreakerRegistry
        self.client: Optional[httpx.AsyncClient] = None
        self.cache: dict[str, Any] = {}
        self.registry_warmed: bool = False
        self.registry_error: Optional[str] = None

    # -- the in-process cache -------------------------------------------

    def remember(self, key: str, value: Any) -> None:
        """Store a successful live answer for the fallback ladder to reuse."""
        if key in self.cache:
            return
        if len(self.cache) >= VALUE_CACHE_MAX:
            # Evict the oldest insertion. dicts keep insertion order, so this
            # is a cheap FIFO rather than a real LRU - adequate for a bounded
            # demo cache and honest about what it is.
            self.cache.pop(next(iter(self.cache)), None)
        self.cache[key] = value

    def recall(self, key: str) -> Any:
        """Return a remembered answer, or ``None`` meaning "try the next rung"."""
        return self.cache.get(key)

    def ladder_for(self, cache_key: str, default: Any = None) -> FallbackLadder:
        """Build the ladder for one dependency: CACHE -> DEFAULT -> MESSAGE.

        ``LIVE`` is not a rung. P2 already tries the live upstream first and
        only walks this ladder once the live call can no longer be served, so
        the documented order LIVE -> CACHE -> DEFAULT -> MESSAGE is preserved
        by construction.
        """
        return FallbackLadder(
            cache=lambda: self.recall(cache_key),
            default=(lambda: default),
            message="upstream unavailable",
        )

    async def aclose(self) -> None:
        """Release the HTTP client. Safe to call twice."""
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.aclose()
            self.client = None

    def http(self) -> httpx.AsyncClient:
        """The shared client, created on first use.

        P2's proxy opens and closes its own client when it is not handed one,
        so passing this keeps pooling alive across requests.
        """
        if self.client is None:
            self.client = httpx.AsyncClient(follow_redirects=True)
        return self.client


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the shared resources.

    Startup order matters. Logging first, so a failure below is actually
    visible. Then the registry, so the first request finds P2's ``
    load_policy()`` answering from database rows rather than the built-in dict.
    Then the HTTP client.
    """
    configure_logging()
    log = get_logger("jizo.main")

    state = AppState()
    app.state.jizo = state

    # -- registry warm-up: best effort, never fatal --------------------
    try:
        factory = get_sessionmaker()
        async with factory() as session:
            policies = await load_registry(session)
        # Deliberately synchronous and deliberately AFTER the await: P3's note
        # is explicit that register_policy mutates a module-level dict, so
        # awaiting around it could interleave two tasks' registrations.
        keys = register_registry(policies)
        state.registry_warmed = True
        log.info("registry_warmed", apis=len(keys), keys=keys)
    except Exception as exc:  # noqa: BLE001 - the app must still start
        # Not fatal by design. /health and /ready report the degraded truth.
        state.registry_warmed = False
        state.registry_error = f"{type(exc).__name__}: {exc}"
        log.warning(
            "registry_warm_failed",
            error_type=type(exc).__name__,
            note="starting anyway; /ready will report not-ready",
        )

    state.registry = _registry()
    state.http()

    try:
        yield
    finally:
        await state.aclose()
        await dispose_engine()


def _registry() -> BreakerRegistry:
    """P2's process-wide breaker registry."""
    from .proxy import get_breaker_registry

    return get_breaker_registry()


app = FastAPI(
    title="JIZO",
    version="0.4.0",
    description=(
        "Resilience layer for code that depends on APIs you do not control. "
        "Protects every call, then proves the protection works."
    ),
    lifespan=lifespan,
)


def state_of(request: Request) -> AppState:
    """Pull the shared state off the request.

    Typed as ``fastapi.Request``, not ``Any``: FastAPI resolves an annotated
    parameter by type, so a bare ``Any`` would be treated as a *query
    parameter* and every call would 422 on a missing field.
    """
    state = getattr(request.app.state, "jizo", None)
    if state is None:  # pragma: no cover - only before lifespan runs
        state = AppState()
    return state


# ---------------------------------------------------------------------------
# Evidence persistence
# ---------------------------------------------------------------------------


def _attempts_by_api(results: dict) -> dict[str, int]:
    """Per-dependency attempt totals from one arm's results.

    ``/route/plan`` calls two upstreams and they routinely take different
    numbers of attempts. Collapsing them with ``max()`` would stamp weather's
    three tries onto geocode's single successful call, so each api_key keeps its
    own count. A dependency that failed carries ``attempts=None`` and falls back
    to the honest default of 1.
    """
    found: dict[str, int] = {}
    for api_key, payload in (results or {}).items():
        attempts = payload.get("attempts") if isinstance(payload, dict) else None
        if isinstance(attempts, int) and attempts >= 1:
            found[api_key] = attempts
    return found


def _verdict_payload(verdict: ScoreResult, spec: DrillSpec) -> dict:
    """The JSON body ``/fi/run`` returns for one graded drill.

    Factored out so the fresh path and the idempotent "this run already
    finished" path return byte-identical shapes - a client must not need to
    knowing which path answered.
    """
    return {
        "run_id": verdict.run_id,
        "pattern": verdict.pattern.value,
        "ts": verdict.ts,
        "cw": verdict.cw,
        "ps": verdict.ps,
        "prem": verdict.prem,
        "miss": verdict.miss,
        "mult": verdict.mult,
        "explain": verdict.explain(),
        "notes": verdict.notes,
        "spec": spec.summary(),
    }


async def _persist_events(
    session: AsyncSession,
    events: list[EvidenceEvent],
    *,
    mode: str,
    guard: Optional[str] = None,
    attempt: int = 1,
    attempt_by_api: Optional[dict[str, int]] = None,
) -> int:
    """Write one arm's evidence into ``request_logs``.

    Uses P3's own ``event_to_row`` mapper, so the five scorer fields
    (``effect_applied``, ``leaked_raw_error``, ``served_from``, ``fault``,
    ``call_index``) are populated identically to every other writer. A row that
    could not be re-scored would defeat the point of the table.

    ``mode`` and ``guard`` are supplied here from request context, exactly as
    ``store.event_to_row`` documents. ``mode`` is the whole point of this
    function being per-arm: it must be the arm that *produced* these rows, not
    the arm the request happened to serve.

    **``attempt`` is the TOTAL attempt count for the logical call, not a
    per-event attempt identifier.** ``EvidenceEvent`` has no ``attempt`` field -
    P1's schema is frozen and the bus has no concept of an HTTP attempt - and
    ``proxy.py`` passes the same ``call_index`` back on every retry, so no
    field on a row distinguishes attempt 1 from attempt 3. The only figure
    available is ``ResilientResponse.attempts``, which is the total for the
    whole call, so every row of that call carries it. That is more accurate
    than a hardcoded 1 (a call that took three tries no longer claims one) but
    it is still call-level, not row-level. Fixing it properly needs P2 to
    attach the attempt number to each event, or an optional field on the frozen
    schema - a cross-team change this file must not make unilaterally.

    ``attempt_by_api`` overrides ``attempt`` per ``api_key`` for a fan-out where
    each dependency used a different number of tries; anything absent falls back
    to ``attempt``.

    Returns the number of rows written.
    """
    if not events:
        return 0
    lookup = attempt_by_api or {}
    for event in events:
        session.add(
            event_to_row(
                event,
                mode=mode,
                attempt=lookup.get(event.api_key, attempt),
                guard=guard,
            )
        )
    await session.commit()
    return len(events)


async def _load_arm(
    session: AsyncSession,
    trace_id: str,
    api_key: str,
    mode: str,
) -> list[EvidenceEvent]:
    """Read one arm's evidence back: ``trace_id`` + ``api_key`` + ``mode``.

    This is the query the comparison needs and it needs no new P3 function: the
    three columns all exist on ``request_logs``, and ``row_to_event`` is
    already public in ``store.__all__``. Ordering is by ``id``, matching
    ``store.load_evidence``, because rows written in one flush share a ``ts``
    and timestamp order would be arbitrary.
    """
    from .store import row_to_event

    stmt = (
        select(RequestLogRow)
        .where(RequestLogRow.trace_id == trace_id)
        .where(RequestLogRow.api_key == api_key)
        .where(RequestLogRow.mode == mode)
        .order_by(RequestLogRow.id)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [row_to_event(row) for row in rows]


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------


class DashboardHub:
    """Minimal fan-out for P5. Not dashboard logic.

    Deliberately tiny and in-process: a set of sockets, a lock, one broadcast
    method. Every connected client receives every event, because there is
    exactly one demo client and no topic system is needed yet.

    A send failure removes that socket rather than propagating - one closed
    browser tab must never take the server down, and must never stop the
    others receiving.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self.history: list[dict] = []
        self.history_max = 50

    async def connect(self, socket: WebSocket) -> None:
        await socket.accept()
        async with self._lock:
            self._clients.add(socket)

    async def disconnect(self, socket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(socket)

    async def broadcast(self, event_type: str, payload: dict) -> None:
        """Send one event to every connected socket. Never raises."""
        message = {"type": event_type, "payload": payload}
        async with self._lock:
            self.history.append(message)
            if len(self.history) > self.history_max:
                self.history.pop(0)
            targets = list(self._clients)

        dead = []
        for socket in targets:
            try:
                await socket.send_json(message)
            except Exception:  # noqa: BLE001 - a dead client is not an error
                dead.append(socket)
        if dead:
            async with self._lock:
                for socket in dead:
                    self._clients.discard(socket)


hub = DashboardHub()


# ---------------------------------------------------------------------------
# Routes: health and readiness (P3 owns the data, P4 owns the HTTP surface)
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> JSONResponse:
    """P3's health report, served verbatim.

    P3's contract is that ``check_health`` never raises, and that is the
    primary guarantee. The guard below is belt-and-braces for the same
    requirement at the HTTP edge: an orchestrator probe must never receive a
    500 from a health endpoint, whatever the cause. The fallback body carries
    no exception text - an error string served to a browser is how a DSN ends
    up in someone's logs.
    """
    try:
        report = await check_health()
    except Exception as exc:  # noqa: BLE001 - a health check must answer
        logger.warning(
            "health_check_failed",
            extra={"error_type": type(exc).__name__},
        )
        report = {
            "status": "unhealthy",
            "ok": False,
            "service": "jizo-data",
            "database": {"ok": False, "error": type(exc).__name__},
        }

    # 200 even when unhealthy: liveness is "this process answers", and the
    # body carries the truth. Returning 503 here would make an orchestrator
    # restart a process that is correctly reporting an outage.
    return JSONResponse(report, status_code=200)


@app.get("/ready")
async def ready() -> JSONResponse:
    """P3's readiness report. 503 when the service should not take traffic.

    Readiness is deliberately stricter than liveness: it also requires
    ``api_registry`` to be non-empty, because a process with no policy cannot
    protect anything.
    """
    report = await check_ready()
    return JSONResponse(report, status_code=200 if report.get("ready") else 503)


# ---------------------------------------------------------------------------
# Routes: breaker state
# ---------------------------------------------------------------------------


@app.get("/breaker/state")
async def breaker_state(request: Request) -> JSONResponse:
    """Per-api breaker state. **Read-only, and must stay that way.**

    Uses ``BreakerRegistry.states()``, which reports the effective state
    without performing the OPEN -> HALF_OPEN step, plus ``snapshot()`` for the
    dashboard gauges. A dashboard polling this route once a second must not be
    able to arm a probe budget nobody requested - edgecases BR-30 and BR-40,
    and the reason ``state`` is pure while ``effective_state`` acts.
    """
    state = state_of(request)
    states = state.registry.states()
    return JSONResponse(
        {
            "states": states,
            "snapshot": state.registry.snapshot(),
            "registry_warmed": state.registry_warmed,
        }
    )


# ---------------------------------------------------------------------------
# Routes: the control-vs-experiment fan-out
# ---------------------------------------------------------------------------


def _plan_shape(address: str) -> dict[str, str]:
    """Build the two upstream URLs for one address.

    One function so the control arm and the experiment arm request *identical*
    URLs. A comparison where the two arms asked different questions would prove
    nothing, and this is the only place that difference could creep in.

    ``address`` arrives as a user query parameter and is percent-encoded before
    it goes into a URL. Without that, ``a&limit=100`` injects an extra upstream
    parameter and ``New Delhi`` puts a raw space in the query - neither of which
    is the question the caller actually asked.
    """
    encoded = quote(address, safe="")
    return {
        "weather": (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude=28.6139&longitude=77.2090&current=temperature_2m"
            f"&timezone=auto&label={encoded}"
        ),
        "geocode": (
            "https://nominatim.openstreetmap.org/search"
            f"?q={encoded}&format=json&limit=1"
        ),
    }


@app.post("/route/plan")
async def route_plan(
    request: Request,
    mode: str = Query("experiment", description="control | experiment"),
    address: str = Query("Delhi", description="address to plan a route for"),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Fan out to weather and geocode, protected or unprotected, and compare.

    The control arm is not a separate code path - it is the *same* call to
    P2's ``resilient_get`` with ``breaker=None``, which is how P2 documents an
    unprotected call. That keeps the two arms identical except for the thing
    under test.

    Evidence is written to ``request_logs`` tagged with the mode, and no
    ``fi_runs`` row is created: this is ordinary traffic, not a drill.
    """
    try:
        arm = resolve_mode(mode)
    except ValueError as exc:
        # A typo should be a 422, not an unhelpful 500 (the gap P1 recorded
        # as L-05 for build_spec).
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    state = state_of(request)
    # One trace for the whole HTTP request, so every row it writes correlates -
    # both arms, both dependencies. `id(request)` was the previous source and
    # was wrong twice over: a CPython address is not unique across requests
    # (a freed Request's address can be reused, merging two requests into one
    # trace), and it leaks an implementation detail into stored evidence.
    # The format mirrors P2's own `proxy._new_trace_id`.
    trace_id = f"t-plan-{uuid.uuid4().hex[:12]}"
    urls = _plan_shape(address)
    registry = state.registry

    # ONE EvidenceBus per arm, both keyed by the SAME trace_id.
    #
    # The bus keys its timeline by trace_id alone and `EvidenceEvent` has no
    # `mode` field - P3 made mode an `event_to_row` argument precisely because
    # the event cannot carry it. So with a single shared bus there is no way to
    # tell which arm produced a row, and persisting them under one mode labels
    # the protected arm's evidence as control. Two buses under one trace id
    # keeps the correlation benefit while making attribution structural rather
    # than a positional slice that would break silently if the arms ever ran
    # concurrently.
    experiment_bus = EvidenceBus()
    control_bus = EvidenceBus()

    async def one_arm(arm_name: str, bus: EvidenceBus) -> dict:
        """Run both dependencies for one arm. Registered breakers = experiment."""
        protected = arm_name == "experiment"

        async def call_one(api_key: str) -> Any:
            policy = load_policy(api_key)
            breaker = registry.get(policy) if protected else None
            # Nominatim is a shared community service whose usage policy asks
            # every client to identify itself with a contactable User-Agent.
            # P3 already resolves NOMINATIM_UA (with its own default in the
            # env > vault > .env > default chain), so the value comes from
            # config rather than being invented here.
            headers = (
                {"User-Agent": get_config().nominatim_ua}
                if api_key == "geocode"
                else None
            )
            if protected:
                fallback = state.ladder_for(f"{api_key}:{address}")
            else:
                # The control arm must be genuinely unprotected. It previously
                # reused the experiment arm's ladder (same cache key
                # ``{api}:{address}``) and P2's default retry budget, so a
                # control call that failed could be served from the value the
                # protected arm had just cached, and retried exactly like the
                # protected path. That measures protection against something
                # that is itself protected. No cache/default rung and a single
                # attempt make control the raw upstream call this module
                # documents it as.
                policy = policy.model_copy(update={"max_attempts": 1})
                fallback = FallbackLadder()
            return await resilient_get(
                api_key,
                urls[api_key],
                headers=headers,
                policy=policy,
                trace_id=trace_id,
                bus=bus,
                breaker=breaker,
                fallback=fallback,
                client=state.http(),
            )

        # Fan out concurrently. Both legs share one trace_id and one bus; the
        # bus keys its counters on (trace_id, api_key), so weather and geocode
        # keep independent occurrence and call_index values.
        responses = await asyncio.gather(
            call_one("weather"), call_one("geocode"),
            return_exceptions=True,
        )

        results = {}
        for api_key, response in zip(("weather", "geocode"), responses):
            if isinstance(response, BaseException):
                # One dead dependency must not lose the other's answer, and the
                # shape must stay IDENTICAL to a success: a dashboard reading
                # `result["weather"]["attempts"]` would otherwise get a KeyError
                # exactly when the upstream is down - the moment the panel
                # matters most. Unavailable values are None; `error` is an
                # optional extra, never a replacement for the core keys.
                results[api_key] = {
                    "api_key": api_key,
                    "served_from": ServedFrom.NONE.value,
                    "status_code": None,
                    "attempts": None,
                    "latency_ms": None,
                    "breaker_state": None,
                    "data": None,
                    "note": None,
                    "error": f"{type(response).__name__}",
                }
                continue
            # A successful live answer becomes a future fallback value - but
            # only on the protected arm. The cache *is* the protection, so the
            # unprotected control arm must neither write it nor (above) read it.
            if (
                protected
                and response.served_from is ServedFrom.LIVE
                and response.data is not None
            ):
                state.remember(f"{api_key}:{address}", response.data)
            results[api_key] = {
                "api_key": api_key,
                "served_from": response.served_from.value,
                "status_code": response.status_code,
                "attempts": response.attempts,
                "latency_ms": round(response.latency_ms, 2),
                "breaker_state": (
                    response.breaker_state.value if response.breaker_state else None
                ),
                "data": response.data,
                "note": response.note,
            }
        return results, bus

    # Initialised before the `try` so the `finally` below can never reference an
    # unbound name. Previously the `finally` unpacked `experiment`,
    # `control` and two `*_bus_used` locals assigned *inside* the try, so an
    # unexpected failure in the second arm raised UnboundLocalError there,
    # replacing the real error and skipping the response entirely.
    experiment: dict = {}
    control: dict = {}

    try:
        experiment, _ = await one_arm("experiment", experiment_bus)
        control, _ = await one_arm("control", control_bus)
    finally:
        # Each arm is persisted under ITS OWN mode. The bus objects are the ones
        # created at the top of the handler, so persistence cannot be skipped by
        # an exception mid-arm - both arms are always written.
        written = 0
        written += await _persist_events(
            session,
            experiment_bus.events(trace_id),
            mode="experiment",
            attempt_by_api=_attempts_by_api(experiment),
        )
        written += await _persist_events(
            session,
            control_bus.events(trace_id),
            mode="control",
            attempt_by_api=_attempts_by_api(control),
        )

    # The comparator reads the evidence we just persisted, so the report is
    # built from the same rows a judge can query later. `mode=arm` is passed
    # explicitly: without it the field silently reported DEFAULT_MODE even when
    # the request was serving the control arm.
    per_api = {}
    for api_key in ("weather", "geocode"):
        control_events = await _load_arm(session, trace_id, api_key, "control")
        experiment_events = await _load_arm(session, trace_id, api_key, "experiment")
        report: CompareResult = compare_sides(
            control_events, experiment_events, mode=arm
        )
        per_api[api_key] = report.as_dict()

    await hub.broadcast(
        "plan",
        {"trace_id": trace_id, "mode": arm, "apis": list(per_api)},
    )

    return JSONResponse(
        {
            "trace_id": trace_id,
            "mode": arm,
            "address": address,
            "result": experiment if arm == "experiment" else control,
            "comparison": per_api,
            "evidence_rows": written,
        }
    )


# ---------------------------------------------------------------------------
# Routes: the fault-injection console
# ---------------------------------------------------------------------------


@app.post("/fi/run")
async def fi_run(
    body: FiRun,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Run one drill through the real protector and grade it.

    The spec is graded by P1's ``score_run`` on P2's evidence, and both the
    intent and the verdict are stored with P3's ``save_run`` in one
    transaction. This route adds no scoring logic of its own - it supplies the
    spec, the bus and the guard, and reports what came back.

    Intent is recorded first (``spec_to_row``), so a run that dies mid-flight
    still leaves a row saying what it was trying to do.

    ## Occurrences vs retries - the distinction that matters here

    ``spec.total_occurrences`` is the number of **logical calls** the drill
    watches, and this route owns executing them. A retry *inside* one
    occurrence is not another occurrence: ``proxy.py`` passes the same
    ``call_index`` back on every retry, so one ``resilient_get`` call advances
    ``bus.call_count`` exactly once no matter how many HTTP attempts it makes.
    The loop below therefore calls ``resilient_get`` once per occurrence and
    never once per attempt - which is what keeps "call 3" meaning call 3.

    All occurrences share one ``EvidenceBus`` and one ``trace_id``, so the
    scorer sees ``call_index`` running 1..N and can grade a k-of-n drill. With
    ``total_occurrences == 1`` this is exactly one call, which is what the
    single-call version of this route did.

    ## Re-firing a finished ``run_id`` is idempotent, not fatal

    ``run_id`` is the primary key of ``fi_runs``. P3's ``save_run`` treats a run
    that already carries a verdict as complete and idempotent, so this route
    looks it up *first* and returns the stored verdict rather than re-inserting
    the plan row. A retried HTTP request - or a second click on the same run -
    used to hit ``fi_runs_pkey`` and 500; it now gets the first verdict back.
    The FI console should still mint a unique ``run_id`` per click, or the panel
    will keep showing the first drill's numbers.
    """
    state = state_of(request)
    spec = body.to_spec()
    trace_id = spec.run_id

    if spec.total_occurrences > MAX_DRILL_OCCURRENCES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"total_occurrences {spec.total_occurrences} exceeds the "
                f"maximum of {MAX_DRILL_OCCURRENCES}"
            ),
        )

    # Column widths: fi_runs.run_id is VARCHAR(128); the *_api columns are
    # VARCHAR(64). An over-long value used to reach asyncpg and raise
    # "value too long", which FastAPI served as a 500 - a bad request must be
    # a 422.
    if len(spec.run_id) > 128:
        raise HTTPException(
            status_code=422,
            detail=f"run_id exceeds 128 characters ({len(spec.run_id)})",
        )
    for label, value in (("target.api_key", spec.target.api_key),
                         ("guard.api_key", spec.guard.api_key)):
        if len(value) > 64:
            raise HTTPException(
                status_code=422,
                detail=f"{label} exceeds 64 characters ({len(value)})",
            )

    try:
        policy = load_policy(spec.target.api_key)
    except KeyError as exc:
        # Same class of mistake edgecases L-05 flags for build_spec: a bad name
        # must be a readable 4xx, not an unhandled KeyError turned into a 500.
        raise HTTPException(
            status_code=422,
            detail=f"unknown api_key {spec.target.api_key!r}",
        ) from exc

    # A finished run is idempotent in P3. Return its stored verdict instead of
    # colliding on fi_runs' primary key - a retried request previously 500'd.
    existing = await session.get(FiRunRow, spec.run_id)
    if existing is not None and existing.ts is not None:
        stored = await load_run(session, spec.run_id)
        if stored is not None:
            return JSONResponse(_verdict_payload(stored, spec))

    # One bus for the whole drill. No GuardEvaluator is constructed here:
    # `resilient_get` builds its own internally when both `bus` and `spec` are
    # supplied (proxy.py), and only when `spec.target.api_key` matches the
    # call's api_key. A second instance was dead code.
    bus = EvidenceBus()

    # Record the plan before anything can fail. A row may already exist from an
    # earlier attempt that never finished; leave it for save_run to repair.
    if existing is None:
        session.add(spec_to_row(spec))
        try:
            await session.commit()
        except IntegrityError:
            # Two requests raced the same run_id between our SELECT and INSERT.
            # Roll back and treat it as the idempotent case rather than a 500.
            await session.rollback()
            stored = await load_run(session, spec.run_id)
            if stored is not None:
                return JSONResponse(_verdict_payload(stored, spec))

    fault_events: list[EvidenceEvent] = []
    attempts_for_events: list[int] = []
    try:
        # One iteration per LOGICAL occurrence. Retries happen inside a single
        # `resilient_get` call and stay inside it, so `call_index` advances
        # exactly once per iteration no matter how many attempts were made.
        for _occurrence in range(1, spec.total_occurrences + 1):
            before = len(bus.events(trace_id))
            response = await resilient_get(
                spec.target.api_key,
                policy.base_url,
                policy=policy,
                trace_id=trace_id,
                bus=bus,
                breaker=state.registry.get(policy),
                fallback=state.ladder_for(spec.target.api_key),
                spec=spec,
                client=state.http(),
            )
            # `attempts` is this occurrence's TOTAL HTTP attempts. The bus may
            # have appended several rows for it, and each carries that total -
            # a call that took three tries must not be stored as one.
            raw_attempts = getattr(response, "attempts", None)
            total = (
                raw_attempts
                if isinstance(raw_attempts, int) and raw_attempts >= 1
                else 1
            )
            attempts_for_events.extend(
                [total] * (len(bus.events(trace_id)) - before)
            )
        # The bus is the single authority for evidence. Using
        # `result.events` instead would only ever be a subset of it, and
        # accumulating it per iteration would double-count the rows.
        fault_events = bus.events(trace_id)
    except Exception as exc:  # noqa: BLE001 - the run must still be graded
        # A crash here is exactly the case `ts IS NULL` exists for: intent is
        # already committed, so /fi/runs/{id} shows an unfinished run rather
        # than losing the fact that it was attempted.
        await hub.broadcast(
            "fi_run",
            {"run_id": spec.run_id, "error": type(exc).__name__},
        )
        raise HTTPException(
            status_code=502,
            detail=f"drill call failed: {type(exc).__name__}",
        ) from exc

    # Grade with P1 exactly ONCE, over every occurrence's evidence. Not one
    # line of scoring logic here.
    verdict = score_run(spec, fault_events)
    await save_run(
        session,
        spec,
        verdict,
        events=fault_events,
        mode="experiment",
        guard=spec.guard.describe(),
        # One attempt number per event, so a retried call is no longer stored
        # as a single attempt. Guarded on the length save_run requires.
        attempts=(
            attempts_for_events
            if len(attempts_for_events) == len(fault_events)
            else None
        ),
    )

    await hub.broadcast(
        "fi_run",
        {"run_id": verdict.run_id, "ts": verdict.ts, "explain": verdict.explain()},
    )

    return JSONResponse(_verdict_payload(verdict, spec))


@app.get("/fi/runs/{run_id}")
async def fi_run_detail(
    run_id: str,
    include_evidence: bool = Query(
        False, description="include the stored timeline"
    ),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Read a stored verdict back.

    P3's ``load_run`` returns ``None`` both for an unknown run and for one that
    started and never finished. That distinction is preserved rather than
    papered over: the response says which case it is, so the FI console can show
    "crashed mid-flight" instead of a bare 404. No new store API is invented to
    work around it.
    """
    stored = await load_run(session, run_id)
    evidence = await load_run_evidence(session, run_id)

    if stored is None:
        # Distinguish "never heard of it" from "began and did not finish" by
        # checking for the intent row P3 left behind. This reads a public model
        # directly; it adds no store API and modifies no P3 file.
        exists = await session.get(FiRunRow, run_id)
        if exists is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
        return JSONResponse(
            {
                "run_id": run_id,
                "found": False,
                "finished": False,
                "detail": "run started but never produced a verdict",
            },
            status_code=200,
        )

    payload: dict[str, Any] = {
        "run_id": stored.run_id,
        "found": True,
        "finished": True,
        "pattern": stored.pattern.value,
        "ts": stored.ts,
        "cw": stored.cw,
        "ps": stored.ps,
        "prem": stored.prem,
        "miss": stored.miss,
        "mult": stored.mult,
        "explain": stored.explain(),
        "notes": stored.notes,
        "spec": stored.spec.summary() if stored.spec else None,
    }
    if include_evidence:
        payload["evidence_rows"] = len(evidence)
        payload["timeline"] = [
            e.model_dump(mode="json") for e in stored.timeline
        ]
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Routes: dashboard WebSocket
# ---------------------------------------------------------------------------


@app.websocket("/ws/dashboard")
async def ws_dashboard(socket: WebSocket) -> None:
    """Push drill and plan events to P5. No dashboard logic lives here.

    On connect the client receives a snapshot of the most recent events, so a
    page that opens mid-demo is not blank, then it receives each new event as
    it happens.
    """
    await hub.connect(socket)
    try:
        await socket.send_json(
            {"type": "snapshot", "payload": {"events": list(hub.history)}}
        )
        while True:
            # The protocol is fire-and-forget: the server pushes, and an
            # inbound message is only used as a liveness signal.
            await socket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - a broken client is not a server error
        pass
    finally:
        await hub.disconnect(socket)


__all__ = ["app", "hub", "lifespan", "AppState"]
