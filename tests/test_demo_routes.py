"""
Tests for the demo front door and the dashboard-as-a-page (owner: Pushkar).

The demo app is served by the same process that owns the protection stack, so
these check the wiring that makes the demo *real*: the page, the dashboard
page, and the activity feed that reads back what the protector wrote.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.main import app


def test_demo_page_is_served_at_the_root():
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Demo App" in response.text
    # it must call the real route, not a fake endpoint
    assert "/route/plan" in response.text


def test_dashboard_page_is_served():
    with TestClient(app) as client:
        response = client.get("/dashboard")

    assert response.status_code == 200
    assert "JIZO Dashboard" in response.text


def test_the_dashboard_shell_assets_are_served():
    """The /dashboard page loads styles.css and app.js by relative path."""
    with TestClient(app) as client:
        assert client.get("/styles.css").status_code == 200
        assert client.get("/app.js").status_code == 200


def test_activity_feed_returns_real_rows():
    """The demo's 'not hardcoded' proof reads request_logs back."""
    from tests.test_data import _database_or_skip
    _database_or_skip()

    with TestClient(app) as client:
        response = client.get("/demo/activity", params={"limit": 5})

    assert response.status_code == 200
    rows = response.json()["rows"]
    assert rows, "expected at least one seeded request_logs row"
    assert set(rows[0]) >= {"apiKey", "phase", "servedFrom", "breakerState"}


def test_simulate_points_the_upstreams_at_a_dead_port():
    """The 'break the upstreams' switch must not touch the call path."""
    from backend.main import _plan_shape

    real = _plan_shape("Delhi")
    broken = _plan_shape("Delhi", simulate=True)

    assert real["weather"].startswith("https://api.open-meteo.com")
    assert broken["weather"].startswith("http://127.0.0.1:9")
    assert broken["geocode"].startswith("http://127.0.0.1:9")
