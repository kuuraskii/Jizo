# P5 — Dashboard (native window + data layer)

Owner: Pushkar. UI: `frontend/`. Data: `backend/dashboard.py`.

The watchtower, shipped **inside the library**: one Python call opens a native
window. No server, no URL to type, no browser needed.

```python
from backend.dashboard import dashboard

dashboard(api_key="weather", name="Open-Meteo").open()
# or, from the command line:
#   python -m backend.dashboard --api-key weather
```

## The split that matters

**`snapshot()` is the data; `frontend/` is only pixels.**

`snapshot()` returns plain nested dicts, imports no GUI package, touches no
socket, and is fully unit-testable headlessly (`tests/test_dashboard.py`, 14
tests, no database, no window). Every panel renders from exactly that payload —
the frontend computes nothing. That is what lets the same numbers back a test,
a CI check, and the window on stage, and it is how the render path obeys the
PRD rule "no network I/O on the UI thread": there is no I/O to do, only dicts.

## Files

| File | Purpose |
|---|---|
| `backend/dashboard.py` | `snapshot()` (data) + `Dashboard`/`dashboard()` (pywebview host) |
| `frontend/index.html` | markup; `__JIZO_DATA__` is replaced with the payload at launch |
| `frontend/styles.css` | true-dark theme + neon accents + glass |
| `frontend/app.js` | renderer; the single API-key dropdown; charts |
| `requirements-gui.txt` | `pywebview` — **optional**, so `import backend` never needs it |
| `tests/test_dashboard.py` | headless tests |

## What the dashboard shows (per selected API key)

Hystrix-style, one screen, no scrolling:

- **Row 1** — Overall Status hero · Error Rate · Mean / 90th / 99th latency tables
- **Row 2** — Open Circuit Breakers · Command Failures · Command Successes · Command Timeouts
- **Row 3** — Fallback Successes · Duplicate Effects · Total Calls · Error Rate
- **Row 4** — TS Scorecards (by pattern) · 4-Axis radar · Protected vs Control

The **single dropdown** picks the dependency. Switching it re-renders every
panel and highlights that dependency in the latency tables. The list of keys
comes from `api_registry`, so **adding an upstream in P3 makes it appear in the
dropdown with no P5 change**.

## Where the data comes from (integration points)

`snapshot()` reads P3's rows and **reuses** the other parts rather than
restating them:

| Panel | Source |
|---|---|
| breaker state / open count | `breaker_transitions` (latest per api) |
| error rate, latency, counters | `request_logs` (`phase='recv'`) |
| drill scorecards, TS rate | `fi_runs` |
| latency tables | `request_logs.latency_ms` grouped by api_key |
| 4-axis radar | `axes.axis_scores` (unmeasurable axes stay `None`) |
| protected-vs-control bars | `compare.side_metrics` / `compare_sides` |

It never calls an upstream and never advances a breaker — a dashboard that
pokes the thing it observes is not an observer.

## Setup required to run it

1. **A populated database.** `DATABASE_URL` set, migrations applied, and
   `api_registry` seeded (`python -m backend.seed`). The dropdown is empty
   without registry rows.
2. **Data to show.** `request_logs` / `fi_runs` come from real traffic
   (`/route/plan`) or drills (`/fi/run`). With no rows the panels say "no data"
   rather than inventing a zero.
3. **`pywebview` (optional).** `pip install -r requirements-gui.txt`. If it, or
   the OS WebView2/WebKit runtime, is missing, `open()` **falls back to the
   system browser** instead of crashing.

## Theme tokens

Near-black base `#0A0A0C`; panels `#1A1D24` with a 1px `#2A2F3D` border; text
`#E2E8F0`. Accents are reserved for state and used sparingly: healthy/success
cyan `#00F0FF` (charts green `#00FF66`), warning amber `#FFB800`, critical
"NOT WELL" hot pink `#FF0055`. Only critical / active state gets a glow.

## Known limitations

- **In-process refresh, not WebSocket.** The window re-reads `snapshot()` every
  5s. The PRD's `WS /ws/dashboard` push is P4's; wiring the window to it is a
  drop-in follow-up (swap the timer for a socket).
- **No FI console on the board.** Firing a drill from the UI would need the
  FastAPI server up and would change the approved 4-row layout, so it is left
  to the `/fi/run` route for now.
- **Percentiles are computed in Python** over the fetched rows (no numpy), fine
  at demo scale, not at millions of rows.
