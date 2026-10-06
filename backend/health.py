"""
JIZO - Part 3 data health (owner: Aayush).

P4 owns the FastAPI routes; this module owns the *data* behind
`GET /health` and `GET /ready`. Kept separate so Riya wraps it in a route
without importing our engine internals.

Payload shape follows AI-Build Documentation Sec. 7.4:

    GET /health -> {"status": "degraded", "uptimeSec": 1234,
                    "dependencies": {"weather": {"reachable": false,
                                                 "breaker": "OPEN"},
                                     "geocode": {"reachable": true,
                                                 "breaker": "CLOSED"}}}
    GET /ready  -> 200 if DB + policy loaded

Note the dependency keys are the *registered* api_keys (`weather`,
`geocode`, `payment`), because Part 3's registry is aligned to Part 2's
`config.py`. Sec. 7.4's own example prints `open-meteo`/`nominatim`, which is
the build doc disagreeing with the merged code.

Three rules:

1. **Never raise.** `/health` must always answer, even when the database is
   down. A health check that 500s tells you nothing; one returning
   `{"status": "degraded", ...}` tells you exactly what is broken. This also
   matches PRD acceptance "Zero secrets in git; /health reflects dependency
   truthfully".

2. **Never leak a secret.** The database URL is rendered with
   `hide_password=True`, and SQLAlchemy exceptions are reported by TYPE
   only, because a connection error can embed the DSN.

3. **Cheap.** Only a `SELECT 1` plus COUNT(*) on small tables, so this is
   safe to poll from the dashboard.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from .secrets import get_config
from .db import get_sessionmaker
from .models import (
    ApiRegistryRow,
    BreakerTransitionRow,
    FiRunRow,
    RequestLogRow,
)

#: How long we will wait before calling it unhealthy. Short on purpose: this
#: backs a UI indicator, and a health check that hangs is worse than one
#: that reports a timeout.
HEALTH_TIMEOUT_S = 3.0

#: Process start, for `uptimeSec` (Sec. 7.4).
_STARTED_AT = time.monotonic()

_TABLES = (
    ("api_registry", ApiRegistryRow),
    ("breaker_transitions", BreakerTransitionRow),
    ("request_logs", RequestLogRow),
    ("fi_runs", FiRunRow),
)


def _sanitised_url() -> str:
    """Connection URL with any password removed."""
    url = get_config().database_url
    if not url:
        return "(not configured)"

    try:
        from sqlalchemy.engine import make_url

        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - health must never raise
        return "(unparseable)"


async def _snapshot(session) -> dict[str, Any]:
    """Row counts plus, where possible, each upstream's latest breaker state.

    The breaker state comes from `breaker_transitions` rather than the live
    protector, because this function must not call an upstream - Sec. 5 of
    the PRD forbids network I/O on the UI/render path.
    """
    counts: dict[str, int] = {}
    for name, model in _TABLES:
        result = await session.execute(select(func.count()).select_from(model))
        counts[name] = int(result.scalar_one())

    # Newest transition per api is the current state. Ordered by (ts, id):
    # rows of one run are usually inserted in a single flush, so their `ts`
    # values tie - ordering by timestamp alone would make the "current"
    # breaker flip arbitrarily between polls.
    states: dict[str, str] = {}
    rows = await session.execute(
        select(
            BreakerTransitionRow.api_key,
            BreakerTransitionRow.to_state,
        ).order_by(BreakerTransitionRow.ts.desc(), BreakerTransitionRow.id.desc())
    )
    for api_key, to_state in rows:
        states.setdefault(api_key, to_state)

    # Every registered api, so an api with no transition row is still
    # reported (as CLOSED, the documented startup state per Sec. 7.2) rather
    # than silently missing from the payload.
    registered = (
        await session.execute(select(ApiRegistryRow.api_key))
    ).scalars().all()

    return {"counts": counts, "breaker_states": states, "api_keys": list(registered)}


def _scrub(text: str) -> str:
    """Remove any credential a message might carry.

    The generic exception branch copies an exception's text into a report
    that is served to a browser, and some exceptions embed the DSN - notably
    `backend/db.py`'s "must use the async driver: '<url>'" guard, which
    fires on the single most common misconfiguration. So the raw URL is
    replaced with its masked form, and the bare password with `***` in case
    it appears on its own.
    """
    try:
        raw = get_config().database_url
    except Exception:  # noqa: BLE001 - scrubbing must never raise
        return text

    if not raw:
        return text

    text = text.replace(raw, _sanitised_url())
    try:
        from sqlalchemy.engine import make_url

        password = make_url(raw).password
        if password:
            # Replace the password ONLY in a credential position
            # (`scheme://user:PASSWORD@host`). A bare `str.replace` also
            # rewrote the password when it happened to be a substring of the
            # URL itself - the project's own password `postgres` turned
            # `postgresql` into `***ql`, mangling the message for no benefit.
            text = re.sub(
                r"(://[^:/@]*:)" + re.escape(password) + r"(@)",
                r"\1***\2",
                text,
            )
    except Exception:  # noqa: BLE001
        pass
    return text


def _any_breaker_open(states: dict[str, str]) -> bool:
    """Is any dependency's latest known breaker state OPEN?

    Sec. 7.4's "degraded" is exactly this case: the service itself is up and
    answering, but something it depends on is failing. Reporting that as
    "healthy" would hide a real outage, and reporting it as "unhealthy"
    would make a running service look dead.
    """
    return any(state == "OPEN" for state in states.values())


async def check_health() -> dict[str, Any]:
    """Report dependency status, row counts, and database reachability.

    Returns a plain dict so P4 can serialise it with no glue, and so the
    test suite can assert on it without a database.
    """
    config = get_config()
    started = time.perf_counter()

    report: dict[str, Any] = {
        # Sec. 7.4's headline field. Computed at the end from the database
        # result and the stored breaker states:
        #   unhealthy - the database is unreachable, so nothing can be served
        #   degraded  - reachable, but at least one breaker is OPEN
        #   healthy   - reachable and every breaker is closed
        "status": "unhealthy",
        # Convenience boolean for probes that only want a yes/no. Derived
        # from `status` so the two can never disagree.
        "ok": False,
        "uptimeSec": int(time.monotonic() - _STARTED_AT),
        "service": "jizo-data",
        # Sec. 7.6 dependency status. Populated from stored breaker
        # transitions; "reachable" is deliberately conservative - we report
        # what we know from the database, never a live probe, so /health
        # cannot itself become an upstream call.
        "dependencies": {},
        "database": {
            "ok": False,
            "url": _sanitised_url(),
            "driver": "asyncpg",
            "latency_ms": None,
            "error": None,
        },
        "tables": {},
        "config": config.describe(),
    }

    if not config.database_url:
        report["database"]["error"] = "DATABASE_URL is not set"
        report["status"] = "unhealthy"
        return report

    try:
        factory = get_sessionmaker()

        async def _probe() -> dict[str, Any]:
            async with factory() as session:
                # A trivial round trip confirms the connection is genuinely
                # usable, not merely that the pool handed us a socket.
                await session.execute(text("SELECT 1"))
                return await _snapshot(session)

        data = await asyncio.wait_for(_probe(), timeout=HEALTH_TIMEOUT_S)

        report["tables"] = data["counts"]
        report["database"]["ok"] = True

        # Dependents: every REGISTERED api, with its latest known state.
        # Sourced from api_registry, not from the transition table, so an api
        # with no transitions yet is still reported - as CLOSED, the
        # documented startup state (Sec. 7.2).
        #
        # `reachable` is derived from the breaker state, because we never
        # probe an upstream here (the render path must do no network I/O).
        # Hard-coding `reachable: True` published an OPEN breaker as
        # reachable, which contradicts Sec. 7.4's own example payload:
        #   {"open-meteo": {"reachable": false, "breaker": "OPEN"}}
        for api_key in data["api_keys"]:
            state = data["breaker_states"].get(api_key, "CLOSED")
            report["dependencies"][api_key] = {
                "reachable": state != "OPEN",
                "breaker": state,
            }

        report["status"] = "degraded" if _any_breaker_open(data["breaker_states"]) else "healthy"
        report["ok"] = report["status"] == "healthy"

    except TimeoutError:
        report["database"]["error"] = f"no response within {HEALTH_TIMEOUT_S:g}s"
        report["status"] = "unhealthy"
    except SQLAlchemyError as exc:
        # Report the exception TYPE, not its message: a connection error can
        # embed the DSN, and that must not reach a browser or a log.
        report["database"]["error"] = f"SQLAlchemyError: {type(exc).__name__}"
        report["status"] = "unhealthy"
    except Exception as exc:  # noqa: BLE001 - health must never raise
        # Scrubbed, not raw. `db.py`'s driver guard embeds the URL in its
        # message, and this field is served to a browser.
        report["database"]["error"] = _scrub(f"{type(exc).__name__}: {exc}")
        report["status"] = "unhealthy"

    finally:
        report["database"]["latency_ms"] = round(
            (time.perf_counter() - started) * 1000, 2
        )

    return report


async def check_ready() -> dict[str, Any]:
    """Sec. 7.4: 200 only if the DB is reachable AND policy is loaded.

    Readiness is deliberately stricter than liveness. A pod that is running
    but has no policy cannot protect anything, so it should not receive
    traffic.
    """
    report = await check_health()

    policies_loaded = report["tables"].get("api_registry", 0) > 0
    report["ready"] = bool(report["database"]["ok"] and policies_loaded)
    report["reason"] = (
        "ok"
        if report["ready"]
        else (
            "database unreachable"
            if not report["database"]["ok"]
            else "no api_registry rows - policy not loaded"
        )
    )
    return report