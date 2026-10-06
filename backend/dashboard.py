"""
JIZO - Part 5 dashboard: the data layer and the native-window host.

Owner: Pushkar (host + snapshot). UI: `frontend/`.

The dashboard is delivered *inside the library*, not as a server route:

    from backend import dashboard
    dashboard(api_key="weather").open()

`open()` pops a native OS window (WebView2 on Windows, WebKit elsewhere) with
no browser and no URL to type.

## The one split that matters

**`snapshot()` is the data; the frontend is only pixels.** `snapshot()` returns
plain nested dicts, imports no GUI package, touches no socket, and is fully
unit-testable headlessly. Every panel in `frontend/` renders from exactly this
payload - it computes nothing. That is what lets the same numbers back a test,
a CI check, and the window on stage, and it is why the render path can obey the
PRD's "no network I/O on the UI thread" rule: there is no I/O to do, only dicts.

## What it reads, and what it must never do

Reads P3's rows (`api_registry`, `request_logs`, `fi_runs`,
`breaker_transitions`) and reuses P1/P2/P4 logic rather than restating it:
`axes.axis_scores` for the radar and `compare.side_metrics` for the
protected-vs-control bars. It never calls an upstream and never advances a
breaker - a dashboard that pokes the thing it is observing is not an observer.

Like `health.py`, it **never raises**: a broken database still produces a
payload with `database.ok = false`, because a dashboard that dies when the data
is bad cannot show you that the data is bad.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import math
import statistics
import webbrowser
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select

from .axes import AXIS_LABELS, AXIS_ORDER, AxisRun, axis_scores
from .compare import compare_sides
from .db import get_sessionmaker
from .models import (
    ApiRegistryRow,
    BreakerTransitionRow,
    FiRunRow,
    RequestLogRow,
)
from .store import load_run, row_to_event

#: Where the static bundle lives.
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

#: How many slots the sparkline/trend charts are divided into.
TREND_BINS = 24

#: Recency window for the "live" panels, in minutes. Older rows still count in
#: the drill proof panels; they just fall outside the trend sparklines.
TREND_WINDOW_MIN = 60

#: An error rate above this (percent) makes the hero "degraded".
DEGRADED_ERROR_PCT = 5.0

_NAME_BY_KEY = {
    "weather": "Open-Meteo",
    "geocode": "Nominatim",
    "payment": "Payment API",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _percentile(values: list[float], pct: float) -> Optional[float]:
    """Linear-interpolated percentile, or None for an empty list.

    Hand-rolled rather than pulling in numpy: the project keeps its runtime
    dependency list short, and this is ten lines.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    rank = (len(ordered) - 1) * (pct / 100.0)
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return round(ordered[int(rank)], 2)
    blended = ordered[low] + (ordered[high] - ordered[low]) * (rank - low)
    return round(blended, 2)


def _rate_pct(numerator: int, denominator: int) -> Optional[float]:
    """A 0-100 percentage, or None when there is nothing to divide by."""
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def _finite(value: object) -> bool:
    """A real number we can plot: not None, not NaN, not an infinity.

    Postgres `double precision` accepts NaN and Infinity, and `json.dumps`
    writes them literally - which is invalid JSON, so the browser's
    `JSON.parse` failed and the ENTIRE board went blank. Filtering here keeps
    one bad latency from taking down every panel.
    """
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _display_name(row: ApiRegistryRow) -> str:
    """A human label for an api_key, for the dropdown and the panels."""
    if row.api_key in _NAME_BY_KEY:
        return _NAME_BY_KEY[row.api_key]
    host = (row.base_url or "").split("//")[-1].split("/")[0]
    return host or row.api_key.title()


def _series(stamps: list[float], bins: int = TREND_BINS) -> list[int]:
    """Bucket timestamps into a fixed number of even time slots.

    A flat series (one distinct time, or none) still returns `bins` points so
    the chart always has something to draw and never has to special-case.
    """
    if not stamps:
        return [0] * bins
    low, high = min(stamps), max(stamps)
    if high == low:
        out = [0] * bins
        out[-1] = len(stamps)
        return out
    width = (high - low) / bins
    out = [0] * bins
    for stamp in stamps:
        index = min(int((stamp - low) / width), bins - 1)
        out[index] += 1
    return out


def _epoch(value: Optional[dt.datetime]) -> Optional[float]:
    if value is None:
        return None
    return value.timestamp()


# ---------------------------------------------------------------------------
# The per-dependency view
# ---------------------------------------------------------------------------


def _view_for(
    api_key: str,
    registry: dict[str, ApiRegistryRow],
    states: dict[str, str],
    logs: list[RequestLogRow],
    runs: list[FiRunRow],
) -> dict[str, Any]:
    """Build one dependency's panel data.

    `logs` and `runs` are the full row sets; this filters. Passing the full set
    once and slicing per key is cheaper than a query per panel and keeps every
    view consistent with the same instant in time.
    """
    row = registry.get(api_key)
    mine = [r for r in logs if r.api_key == api_key]
    recv = [r for r in mine if r.phase == "recv"]

    # -- health headline ------------------------------------------------
    breaker = states.get(api_key, "CLOSED")
    error_rate = _rate_pct(
        sum(1 for r in recv if (r.status_code or 200) >= 500
            or r.served_from == "none"),
        len(recv),
    ) or 0.0

    if breaker == "OPEN":
        status, label, reason = (
            "not_well", "NOT WELL", f"{api_key} breaker is OPEN"
        )
    elif breaker == "HALF_OPEN":
        status, label, reason = (
            "degraded", "DEGRADED", f"{api_key} breaker is probing (HALF_OPEN)"
        )
    elif error_rate > DEGRADED_ERROR_PCT:
        status, label, reason = (
            "degraded", "DEGRADED", f"{error_rate:.0f}% of calls are failing"
        )
    elif not recv:
        status, label, reason = "unknown", "NO DATA", "no calls recorded yet"
    else:
        status, label, reason = (
            "healthy", "HEALTHY", "breaker CLOSED, calls succeeding"
        )

    # -- latency --------------------------------------------------------
    latencies = [r.latency_ms for r in recv if _finite(r.latency_ms)]
    latency = {
        "mean": round(statistics.fmean(latencies), 2) if latencies else None,
        "p50": _percentile(latencies, 50),
        "p90": _percentile(latencies, 90),
        "p99": _percentile(latencies, 99),
    }
    # Per-call rows for the latency cards. Calls, not rows: a retried call is
    # one call (P1's `call_index` rule), so a call's latency is its last recv.
    by_call: dict[int, RequestLogRow] = {}
    for r in recv:
        by_call[r.call_index] = r
    latency_rows = [
        {
            "label": f"call #{index}",
            "name": _display_name(row) if row else api_key,
            "current": round(r.latency_ms, 2) if _finite(r.latency_ms) else None,
            "status": r.status_code,
            "servedFrom": r.served_from,
        }
        for index, r in sorted(by_call.items())
    ]

    # -- the eight trend charts (Hystrix rows 2 and 3) ------------------
    fallbacks = [r for r in recv if r.served_from in ("cache", "default", "message")]
    timeouts = [
        r for r in recv
        if r.fault == "timeout" or r.status_code in (408, 504)
    ]
    failures = [r for r in recv if (r.status_code or 200) >= 500
                or r.served_from == "none"]
    successes = [r for r in recv if r.served_from == "live"]

    # A duplicate is one call whose side effect was applied twice - P1's rule,
    # keyed by (trace, call_index).
    effect_counts: dict[tuple[str, int], int] = {}
    for r in recv:
        if r.effect_applied:
            key = (r.trace_id, r.call_index)
            effect_counts[key] = effect_counts.get(key, 0) + 1
    duplicates = sum(1 for n in effect_counts.values() if n > 1)

    stamps_all = [_epoch(r.ts) for r in recv if r.ts is not None]
    counters = [
        {"key": "breakers", "label": "Open Circuit Breakers Count",
         "value": sum(1 for s in states.values() if s == "OPEN")},
        {"key": "failures", "label": "Command Failures", "value": len(failures)},
        {"key": "successes", "label": "Command Successes", "value": len(successes)},
        {"key": "timeouts", "label": "Command Timeouts", "value": len(timeouts)},
        {"key": "fallbacks", "label": "Fallback Successes", "value": len(fallbacks)},
        {"key": "duplicates", "label": "Duplicate Effects", "value": duplicates},
        {"key": "calls", "label": "Total Calls", "value": len(recv)},
        {"key": "errorrate", "label": "Error Rate (per bucket)", "value": error_rate},
    ]
    trends = {
        "breakers": _series(stamps_all) if breaker == "OPEN" else [0] * TREND_BINS,
        "failures": _series([_epoch(r.ts) for r in failures if r.ts is not None]),
        "successes": _series([_epoch(r.ts) for r in successes if r.ts is not None]),
        "timeouts": _series([_epoch(r.ts) for r in timeouts if r.ts is not None]),
        "fallbacks": _series([_epoch(r.ts) for r in fallbacks if r.ts is not None]),
        "duplicates": _series([_epoch(r.ts) for r in recv
                               if r.effect_applied and r.ts is not None]),
        "calls": _series(stamps_all),
        "errorrate": _series([_epoch(r.ts) for r in failures if r.ts is not None]),
    }

    # The x-axis span, in epoch ms, so the charts can label real times.
    if stamps_all:
        window = {"start": min(stamps_all) * 1000, "end": max(stamps_all) * 1000}
    else:
        now_ms = dt.datetime.now(dt.timezone.utc).timestamp() * 1000
        window = {"start": now_ms - TREND_WINDOW_MIN * 60 * 1000, "end": now_ms}

    # -- proof panels ---------------------------------------------------
    my_runs = [r for r in runs if r.target_api == api_key]
    finished = [r for r in my_runs if r.ts is not None]
    ts_rate = _rate_pct(sum(1 for r in finished if r.ts), len(finished))

    patterns: dict[str, list[FiRunRow]] = {}
    for r in finished:
        patterns.setdefault(r.pattern, []).append(r)
    scorecards = [
        {
            "pattern": pattern,
            "runs": len(group),
            "pass": sum(1 for r in group if r.ts),
            "tsRate": _rate_pct(sum(1 for r in group if r.ts), len(group)),
        }
        for pattern, group in sorted(patterns.items())
    ]

    delta = _mode_delta(mine)

    scans = _radar(my_runs, logs)

    return {
        "apiKey": api_key,
        "name": _display_name(row) if row else api_key,
        "criticality": row.criticality if row else None,
        "baseUrl": row.base_url if row else None,
        "owner": row.owner if row else None,
        "overall": {"status": status, "label": label, "reason": reason},
        "errorRate": error_rate,
        "tsRate": ts_rate,
        "latency": latency,
        "latencyRows": latency_rows,
        "breakers": {
            "open": sum(1 for s in states.values() if s == "OPEN"),
            "total": len(registry),
            "state": breaker,
        },
        "counters": counters,
        "trends": trends,
        "trendWindow": window,
        "scorecards": scorecards,
        "radar": scans,
        "compare": delta,
    }


def _mode_delta(mine: list[RequestLogRow]) -> dict[str, Any]:
    """Protected-vs-control bars for one dependency, via P4's comparator.

    Reuses `compare.side_metrics` rather than counting here, so the dashboard
    and the comparison endpoint cannot disagree about what "fallback" means.
    """
    control = [row_to_event(r) for r in mine if r.mode == "control"]
    experiment = [row_to_event(r) for r in mine if r.mode == "experiment"]
    report = compare_sides(control, experiment)
    return {
        "control": report.control.as_dict(),
        "experiment": report.experiment.as_dict(),
        "delta": report.delta.as_dict(),
        "hasData": bool(control or experiment),
    }


def _radar(my_runs: list[FiRunRow], logs: list[RequestLogRow]) -> list[dict]:
    """The 4-axis radar, computed by `axes.axis_scores`.

    Blast radius comes from the run's own evidence, which is the only place it
    exists. An axis with nothing to measure is returned as None by `axes` and
    is rendered as "no data" - never as a zero.
    """
    by_trace: dict[str, set[str]] = {}
    for r in logs:
        if r.trace_id:
            by_trace.setdefault(r.trace_id, set()).add(r.api_key)

    return None if not my_runs else _radar_from(my_runs, by_trace)


def _radar_from(my_runs: list[FiRunRow], by_trace: dict[str, set[str]]) -> list[dict]:
    from .schemas import EvidenceEvent, Pattern, ScoreResult

    runs: list[AxisRun] = []
    for r in my_runs:
        if r.ts is None:
            continue
        try:
            timeline = [
                EvidenceEvent.model_validate(e) for e in (r.timeline or [])
            ]
            pattern = Pattern(r.pattern)
        except Exception:  # noqa: BLE001 - one bad row must not wipe the radar
            continue
        result = ScoreResult(
            run_id=r.run_id,
            pattern=pattern,
            ts=r.ts, cw=bool(r.cw), ps=bool(r.ps),
            prem=bool(r.prem), miss=bool(r.miss), mult=bool(r.mult),
            # The STORED timeline, not []. `axes.detection` reads it to decide
            # whether the breaker tripped; an empty timeline made every failing
            # run a false negative, so detection always scored 0 - a false
            # reliability claim, the exact thing the honesty rule forbids.
            timeline=timeline, spec=None, notes=[],
        )
        runs.append(
            AxisRun(
                result=result,
                affected_apis=frozenset(by_trace.get(r.trace_id or "", set())),
            )
        )
    score = axis_scores(runs)
    return [
        {
            "axis": axis.value,
            "label": AXIS_LABELS[axis],
            "value": score.values.get(axis),
        }
        for axis in AXIS_ORDER
    ]


# ---------------------------------------------------------------------------
# Public data API
# ---------------------------------------------------------------------------


def _latency_by_api(
    registry: dict[str, ApiRegistryRow], logs: list[RequestLogRow]
) -> list[dict[str, Any]]:
    """Mean/p90/p99 per dependency, for the three latency cards.

    The latency cards list every dependency rather than only the focused one,
    because that is how the Hystrix dashboard reads - and it lets the focus
    highlight move down the table when the dropdown changes, showing where the
    selected dependency sits against the others.
    """
    rows: list[dict[str, Any]] = []
    for api_key, row in registry.items():
        values = [
            r.latency_ms
            for r in logs
            if r.api_key == api_key and r.phase == "recv"
            and _finite(r.latency_ms)
        ]
        rows.append(
            {
                "apiKey": api_key,
                "name": _display_name(row),
                "calls": sum(1 for r in logs if r.api_key == api_key
                             and r.phase == "recv"),
                "mean": round(statistics.fmean(values), 2) if values else None,
                "p90": _percentile(values, 90),
                "p99": _percentile(values, 99),
            }
        )
    return rows


async def snapshot(api_key: Optional[str] = None) -> dict[str, Any]:
    """Everything the dashboard renders, as plain dicts. Never raises.

    Returns a `views` map - one entry per registered api_key - so the frontend
    can switch dependency instantly without a round trip, plus the metadata the
    dropdown needs. `focus` names the dependency to open on.
    """
    generated = dt.datetime.now(dt.timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "generatedAt": generated,
        "focus": api_key,
        "apiKeys": [],
        "latencyByApi": [],
        "database": {"ok": False, "error": None},
        "views": {},
    }

    try:
        factory = get_sessionmaker()
        async with factory() as session:
            registry_rows = (
                await session.execute(select(ApiRegistryRow))
            ).scalars().all()
            logs = (await session.execute(
                # ORDER BY id: the comparator's rule is "the LAST response row
                # of a call decides", which needs a stable order. Without it,
                # the same rows could yield opposite verdicts between reads.
                select(RequestLogRow).order_by(RequestLogRow.id)
            )).scalars().all()
            runs = (await session.execute(
                select(FiRunRow).order_by(FiRunRow.created_at)
            )).scalars().all()

            states: dict[str, str] = {}
            transitions = await session.execute(
                select(BreakerTransitionRow.api_key,
                       BreakerTransitionRow.to_state)
                .order_by(BreakerTransitionRow.ts.desc(),
                          BreakerTransitionRow.id.desc())
            )
            for key, state in transitions:
                states.setdefault(key, state)

        registry = {row.api_key: row for row in registry_rows}
        payload["apiKeys"] = [
            {"apiKey": row.api_key, "name": _display_name(row)}
            for row in registry_rows
        ]
        payload["database"]["ok"] = True
        payload["latencyByApi"] = _latency_by_api(registry, logs)
        payload["views"] = {
            key: _view_for(key, registry, states, logs, runs)
            for key in registry
        }
        # Coerce an unknown focus to a real one. `snapshot("typo")` used to keep
        # `focus="typo"`, build no view for it, and blank the whole board while
        # the dropdown still displayed a valid entry.
        keys = [row.api_key for row in registry_rows]
        if payload["focus"] not in keys:
            payload["focus"] = keys[0] if keys else None
    except Exception as exc:  # noqa: BLE001 - a dashboard must never die
        # The message is scrubbed of any DSN by P3's own health helper, but a
        # type name is enough here and cannot leak. `ok` must be False too:
        # leaving it True showed "db: ok" over an empty board.
        payload["database"]["ok"] = False
        payload["database"]["error"] = type(exc).__name__

    return payload


# ---------------------------------------------------------------------------
# The native-window host
# ---------------------------------------------------------------------------


class Dashboard:
    """A handle for one dashboard window.

    `snapshot_fn` is injectable so a test (or a demo replay) can drive the
    window from a fixed payload instead of the database.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        name: Optional[str] = None,
        snapshot_fn=None,
    ) -> None:
        self.api_key = api_key
        self.name = name
        self._snapshot_fn = snapshot_fn or snapshot

    def render_html(self, payload: dict[str, Any]) -> str:
        """The self-contained page, with the payload embedded.

        Embedding rather than fetching means the window works with no server,
        no port, and no CORS - and a plain browser can open the same file,
        which is how the design is reviewed without launching a window.
        """
        template = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        import json

        boot = json.dumps(payload, default=str).replace("</", "<\\/")
        return template.replace("__JIZO_DATA__", boot)

    def build(self) -> dict[str, Any]:
        """Run the snapshot synchronously (pywebview callbacks are sync)."""
        return asyncio.run(self._snapshot_fn(self.api_key))

    def open(self) -> bool:
        """Open the native window. Returns True if one actually opened.

        Falls back to the system browser when pywebview (or the OS webview
        runtime) is unavailable, which is the documented stage safety net: a
        dashboard in a browser tab beats no dashboard.
        """
        payload = self.build()
        out = FRONTEND_DIR / "_dashboard.html"
        out.write_text(self.render_html(payload), encoding="utf-8")
        return _launch_window(
            out,
            f"JIZO Dashboard — {self.name or self.api_key or 'all'}",
            _JsApi(self),
        )


class _JsApi:
    """The bridge the page calls for a live refresh.

    Only `get_snapshot` is exposed. The render path must never let the page
    write anything (no breaker reset, no drill fire from the UI thread), so
    this surface is deliberately read-only.
    """

    def __init__(self, dash: "Dashboard") -> None:
        self._dash = dash

    async def get_snapshot(self, api_key: Optional[str] = None) -> dict:
        return await self._dash._snapshot_fn(api_key)


def _launch_window(path: Path, title: str, js_api) -> bool:
    """Open `path` in a native window, or the browser if that is not possible.

    The `try` is deliberately broad. On the venue laptop the failure could be a
    missing pywebview, a missing WebView2 runtime, or a locked-down OS policy -
    three different exceptions, one required behaviour: still show the
    dashboard. A dashboard that dies because the window host is missing is a
    self-inflicted demo failure.
    """
    try:
        import webview

        webview.create_window(
            title,
            str(path),
            width=1280,
            height=860,
            background_color="#0B0F17",
            js_api=js_api,
        )
        webview.start()
        return True
    except Exception:  # noqa: BLE001 - fall back rather than crash on stage
        webbrowser.open(path.as_uri())
        return False


def dashboard(api_key: Optional[str] = None, name: Optional[str] = None) -> Dashboard:
    """The library entry point: `dashboard("weather").open()`."""
    return Dashboard(api_key=api_key, name=name)


__all__ = ["Dashboard", "dashboard", "snapshot"]


if __name__ == "__main__":  # pragma: no cover - manual launcher
    import argparse

    parser = argparse.ArgumentParser(
        description="Open the JIZO dashboard in a native window."
    )
    parser.add_argument(
        "--api-key", default=None,
        help="dependency to focus on open (default: the first registered one)",
    )
    parser.add_argument(
        "--name", default=None, help="display name for the window title",
    )
    args = parser.parse_args()
    dashboard(api_key=args.api_key, name=args.name).open()
