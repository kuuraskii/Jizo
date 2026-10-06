"""
Tests for the demo app (`backend/demo.py`) - owner: Pushkar (P6).

The demo is a *consumer* of the library, so these tests drive its HTTP surface
with a `MockTransport` upstream: the real `resilient_get` runs, only the socket
is fake. That is what proves the demo actually goes through the protector
rather than pretending to.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import demo


def _transport() -> httpx.AsyncClient:
    """Up 200s for the real hosts; a dead port for the `simulate` URLs."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in ("127.0.0.1", "localhost"):
            raise httpx.ConnectError("simulated outage", request=request)
        return httpx.Response(200, json={"ok": True, "host": request.url.host})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _clean_cache():
    """The demo caches live answers; a stale cache would change a later test."""
    demo._cache.clear()
    yield
    demo._cache.clear()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(demo, "_make_client", _transport)
    with TestClient(demo.app) as c:
        yield c


def test_index_serves_the_demo_page(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "Route Confirm" in response.text


def test_a_healthy_upstream_is_served_live(client):
    body = client.get("/api/confirm").json()

    assert body["results"]["weather"]["servedFrom"] == "live"
    assert body["results"]["geocode"]["servedFrom"] == "live"
    assert body["degraded"] is False


def test_a_simulated_outage_falls_back_to_the_seeded_default(client):
    """With the upstream dead, the ladder's default rung answers - the demo
    must still confirm a route."""
    body = client.get("/api/confirm", params={"simulate": 1}).json()

    assert body["results"]["weather"]["servedFrom"] == "default"
    assert body["degraded"] is True
    assert body["results"]["weather"]["data"]["location"] == "Delhi"


def test_a_cached_live_answer_is_reused_on_the_next_call(client):
    """Live, then simulate: the second call should be served from cache."""
    client.get("/api/confirm")                       # populates the cache
    body = client.get("/api/confirm", params={"simulate": 1}).json()

    assert body["results"]["weather"]["servedFrom"] == "cache"


def test_the_address_is_url_encoded(client):
    """A raw '&' must not inject extra upstream parameters."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, json={})

    import backend.demo as demo_mod

    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    original = demo_mod._make_client
    demo_mod._make_client = _client
    try:
        client.get("/api/confirm", params={"address": "a&limit=100"})
    finally:
        demo_mod._make_client = original

    assert all("limit=100&format" not in url for url in captured)
    assert any("%26" in url for url in captured)
