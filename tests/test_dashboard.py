"""
Tests for the P5 dashboard data layer (`backend/dashboard.py`).

Owner: Pushkar.

These are **headless**: no window is opened, no browser is launched, and the
panel tests build their rows in memory rather than touching Postgres. That is
the whole point of the `snapshot()` / frontend split - the logic that decides
what a panel says is testable without a GUI or a database, so the expensive,
flaky part (pixels) has nothing to get wrong.

`test_snapshot_survives_a_broken_database` drives the real `snapshot()` with a
session factory that raises, which is the one path that must never propagate.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from backend import dashboard as dash
from backend.dashboard import (
    _display_name,
    _percentile,
    _rate_pct,
    _series,
    _view_for,
    snapshot,
)
from backend.models import ApiRegistryRow, FiRunRow, RequestLogRow


def _registry(**over):
    base = dict(api_key="weather", base_url="https://api.open-meteo.com")
    base.update(over)
    return ApiRegistryRow(**base)


def _log(**over):
    base = dict(
        trace_id="t-1", api_key="weather", phase="recv", occurrence=1,
        call_index=1, served_from="live", status_code=200, latency_ms=100.0,
        mode="experiment", effect_applied=False, leaked_raw_error=False,
        ts=dt.datetime(2026, 10, 6, 12, 0, 0),
    )
    base.update(over)
    return RequestLogRow(**base)


def _run(**over):
    base = dict(
        run_id="r-1", pattern="k_of_n", fault="http_500", target_api="weather",
        target_k=1, target_phase="recv", guard_api="weather",
        guard_phase="send", guard_min_count=1, idempotent=True,
        total_occurrences=4, ts=True, cw=True, ps=True, prem=False,
        miss=False, mult=False, trace_id="t-1", spec={},
    )
    base.update(over)
    return FiRunRow(**base)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def test_percentile_known_values():
    values = [10, 20, 30, 40, 50]
    assert _percentile(values, 50) == 30.0
    assert _percentile(values, 0) == 10.0
    assert _percentile(values, 100) == 50.0
    assert _percentile([42.0], 90) == 42.0
    assert _percentile([], 90) is None


def test_rate_pct_is_none_without_a_denominator():
    assert _rate_pct(0, 0) is None
    assert _rate_pct(1, 4) == 25.0


def test_series_has_a_fixed_length_even_when_empty():
    assert _series([]) == [0] * dash.TREND_BINS
    assert len(_series([1.0, 2.0, 3.0])) == dash.TREND_BINS
    # All at one instant still answers with `bins` points, never a crash.
    assert _series([5.0, 5.0]) == [0] * (dash.TREND_BINS - 1) + [2]


def test_display_name_prefers_the_known_label_then_the_host():
    assert _display_name(_registry()) == "Open-Meteo"
    assert _display_name(_registry(api_key="x", base_url="https://api.foo.dev/v1")) == "api.foo.dev"


# ---------------------------------------------------------------------------
# The per-dependency view
# ---------------------------------------------------------------------------


def test_an_open_breaker_reads_not_well():
    view = _view_for("weather", {"weather": _registry()},
                     {"weather": "OPEN"}, [_log()], [])
    assert view["overall"]["status"] == "not_well"
    assert view["overall"]["label"] == "NOT WELL"


def test_a_closed_breaker_with_healthy_calls_reads_healthy():
    view = _view_for("weather", {"weather": _registry()}, {},
                     [_log()], [])
    assert view["overall"]["status"] == "healthy"


def test_the_hero_never_invents_a_number_for_a_dependency_with_no_calls():
    view = _view_for("weather", {"weather": _registry()}, {}, [], [])
    assert view["overall"]["status"] == "unknown"
    assert view["latency"]["p90"] is None
    assert view["errorRate"] == 0.0


def test_error_rate_counts_5xx_and_unserved_calls():
    logs = [
        _log(),
        _log(status_code=500, served_from="none"),
        _log(status_code=200, served_from="live"),
        _log(status_code=200, served_from="live"),
    ]
    view = _view_for("weather", {"weather": _registry()}, {}, logs, [])
    assert view["errorRate"] == 25.0


def test_fallbacks_and_timeouts_are_counted_separately():
    logs = [
        _log(served_from="live"),
        _log(served_from="cache"),
        _log(served_from="message"),
        _log(status_code=504, served_from="none", fault="timeout"),
    ]
    view = _view_for("weather", {"weather": _registry()}, {}, logs, [])
    counters = {c["key"]: c["value"] for c in view["counters"]}
    assert counters["timeouts"] == 1
    assert counters["successes"] == 1


def test_scorecards_group_runs_by_pattern():
    runs = [
        _run(run_id="a", pattern="k_of_n", ts=True),
        _run(run_id="b", pattern="k_of_n", ts=False, prem=True),
        _run(run_id="c", pattern="post_effect", ts=True),
    ]
    view = _view_for("weather", {"weather": _registry()}, {}, [], runs)
    cards = {c["pattern"]: c for c in view["scorecards"]}
    assert cards["k_of_n"]["runs"] == 2
    assert cards["k_of_n"]["tsRate"] == 50.0
    assert cards["post_effect"]["tsRate"] == 100.0


def test_the_radar_uses_axes_and_leaves_unmeasurable_axes_null():
    view = _view_for("weather", {"weather": _registry()}, {}, [_log()], [_run()])
    by_axis = {r["axis"]: r["value"] for r in view["radar"]}
    # containment is measurable from one api in the trace; the comparative
    # axes have no control arm, so they stay None rather than a fake zero.
    assert by_axis["containment"] == 100.0
    assert by_axis["recovery"] is None


# ---------------------------------------------------------------------------
# Rendering + the never-raise guarantee
# ---------------------------------------------------------------------------


def test_render_html_embeds_the_payload_and_leaves_no_placeholder():
    html = dash.Dashboard().render_html(
        {"focus": "weather", "apiKeys": [], "views": {}, "database": {"ok": True}}
    )
    assert "__JIZO_DATA__" not in html
    assert '"focus": "weather"' in html
    assert "apiKeySelect" in html


def test_snapshot_survives_a_broken_database(monkeypatch):
    """A dashboard that dies when the data is bad cannot show that it is bad."""
    def _explode():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(dash, "get_sessionmaker", _explode)
    payload = asyncio.run(snapshot())

    assert payload["database"]["ok"] is False
    assert payload["database"]["error"] == "RuntimeError"
    assert payload["views"] == {}


def test_launch_window_falls_back_to_the_browser(monkeypatch, tmp_path):
    """No pywebview on the venue laptop must still show the dashboard."""
    import sys

    opened: dict = {}
    # `None` in sys.modules makes `import webview` raise ImportError.
    monkeypatch.setitem(sys.modules, "webview", None)
    monkeypatch.setattr(dash.webbrowser, "open",
                        lambda uri: opened.setdefault("uri", uri))

    ok = dash._launch_window(tmp_path / "board.html", "JIZO", None)

    assert ok is False
    assert "board.html" in opened["uri"]


def test_the_factory_binds_the_key_and_name():
    """`dashboard('weather').open()` is the documented entry point."""
    handle = dash.dashboard("weather", "Open-Meteo")

    assert isinstance(handle, dash.Dashboard)
    assert handle.api_key == "weather"
    assert handle.name == "Open-Meteo"
