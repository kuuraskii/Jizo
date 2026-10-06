"""
JIZO demo app - the logistics dispatcher (owner: Pushkar, P6).

This is the **consumer**. It imports the library's public call -
``resilient_get`` - and nothing from inside the protection stack. That is the
point: if the library is usable, this file proves it by using it the way any
other team would.

    uvicorn backend.demo:app --port 8001
    # then open http://localhost:8001

It confirms a delivery route by fanning out to two real upstreams (weather +
geocode) through the protector, and shows - per call - which rung of the
fallback ladder answered (``servedFrom``) and the breaker state.

``?simulate=1`` points both calls at a dead port, so the fallback ladder is
visible on demand. Without it, dead Wi-Fi still shows the seeded defaults.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from .config import load_policy
from .proxy import FallbackLadder, resilient_get
from .schemas import ServedFrom

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

#: Seeded demo defaults - the "default" rung of the ladder. Delhi, per the
#: demo script, so the dispatcher always has an answer for its home city.
_DEFAULTS: dict[str, dict] = {
    "weather": {
        "source": "seeded default",
        "location": "Delhi",
        "temperature_2m": 31.0,
        "wind_speed_10m": 8.4,
    },
    "geocode": {
        "source": "seeded default",
        "display_name": "New Delhi, Delhi, India",
        "lat": 28.6139,
        "lon": 77.2090,
    },
}

#: In-process value cache - the "cache" rung. Populated from live successes.
_cache: dict[str, Any] = {}

app = FastAPI(title="JIZO Demo - Route Confirm")


def _make_client() -> httpx.AsyncClient:
    """The HTTP client the demo hands to the library.

    A seam for tests: swap this for one backed by `httpx.MockTransport` and the
    real protector still runs, with no socket opened.
    """
    return httpx.AsyncClient(follow_redirects=True)


def _urls(address: str, simulate: bool) -> dict[str, str]:
    if simulate:
        # Nothing listens on port 9 - a guaranteed transport failure, so the
        # ladder is what answers.
        return {"weather": "http://127.0.0.1:9/weather",
                "geocode": "http://127.0.0.1:9/geocode"}
    encoded = quote(address, safe="")
    return {
        "weather": (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=28.6139&longitude=77.2090&current=temperature_2m,"
            f"wind_speed_10m&timezone=auto&label={encoded}"
        ),
        "geocode": (
            "https://nominatim.openstreetmap.org/search"
            f"?q={encoded}&format=json&limit=1"
        ),
    }


def _shape(api_key: str, response) -> dict:
    """One dependency's result, flattened for the UI."""
    return {
        "apiKey": api_key,
        "servedFrom": response.served_from.value,
        "status": response.status_code,
        "attempts": response.attempts,
        "latencyMs": round(response.latency_ms, 1),
        "breakerState": (
            response.breaker_state.value if response.breaker_state else None
        ),
        "note": response.note,
        "data": response.data,
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the (single-file, dark) demo page."""
    return HTMLResponse((FRONTEND / "demo.html").read_text(encoding="utf-8"))


@app.get("/api/confirm")
async def confirm(address: str = "Delhi", simulate: int = 0) -> JSONResponse:
    """Confirm a delivery route through the library, one call per dependency.

    Every upstream answer is labelled with the rung that served it, so a judge
    can see the fallback happen instead of being told it did.
    """
    urls = _urls(address, bool(simulate))
    weather_policy = load_policy("weather")
    geocode_policy = load_policy("geocode")

    # A ladder per dependency: live (tried by the library) -> cache -> default
    # -> message. This is the demo's own value cache and seeded defaults; the
    # library only calls them when the live call cannot be served.
    ladders = {
        "weather": FallbackLadder(cache=lambda: _cache.get("weather"),
                                  default=lambda: _DEFAULTS["weather"]),
        "geocode": FallbackLadder(cache=lambda: _cache.get("geocode"),
                                  default=lambda: _DEFAULTS["geocode"]),
    }
    headers = {
        "geocode": {"User-Agent": "jizo-demo/1.0 (hackathon route confirm)"},
        "weather": None,
    }
    policies = {"weather": weather_policy, "geocode": geocode_policy}

    client = _make_client()
    results: dict[str, dict] = {}
    try:
        for api_key in ("weather", "geocode"):
            response = await resilient_get(
                api_key,
                urls[api_key],
                policy=policies[api_key],
                headers=headers[api_key],
                fallback=ladders[api_key],
                client=client,
                trace_id=f"demo-{uuid.uuid4().hex[:12]}",
            )
            # A live answer becomes the next call's cache rung.
            if response.served_from is ServedFrom.LIVE and response.data is not None:
                _cache[api_key] = response.data
            results[api_key] = _shape(api_key, response)
    finally:
        await client.aclose()

    degraded = any(r["servedFrom"] != "live" for r in results.values())
    return JSONResponse(
        {
            "address": address,
            "simulated": bool(simulate),
            "degraded": degraded,
            "results": results,
            "libraryCall": "backend.proxy.resilient_get",
        }
    )
