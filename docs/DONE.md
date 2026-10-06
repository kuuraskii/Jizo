# JIZO — per-part DONE checklists

Owner: P6. Each part signs off against its own acceptance line from
`WorkDivision.docx`. "Green" means a test or a command proves it, not that it
was tried once.

---

## P1 — Contracts, evidence bus, guards, scorer

- [x] `backend/schemas.py` frozen; `tests/test_schemas_frozen.py` fails if a
      field is added/renamed without telling the team.
- [x] TS = `CW ∧ PS ∧ ¬Prem ∧ ¬Miss ∧ ¬Mult`, computed in exactly one place.
- [x] Guard synthesis (Eqs. 1–6) fires on the intended `k`, not merely once.

## P2 — Breaker, logging, proxy

- [x] Trips at the sourced threshold (25% over a 20-sample window), fast-fails
      while OPEN, and does not trip on a thin sample.
- [x] OPEN → HALF_OPEN → CLOSED with a *limited* probe budget.
- [x] Storm guard: while OPEN, zero upstream calls and zero retries.
- [x] Retries stay one logical call (`call_index` does not drift).

## P3 — Persistence

- [x] `docker compose up -d` → healthy Postgres; `alembic upgrade head` clean.
- [x] Seed is idempotent; a re-run does not duplicate rows.
- [x] `store.py` is the public API P4 codes against; no P4 code touches
      `seed.py` internals.
- [x] `/health` reflects dependency truth and never leaks a secret.

## P4 — Integration + comparison

- [x] `/route/plan` fans out and returns live data all-CLOSED.
- [x] Control vs experiment is a real split (`request_logs.mode`).
- [x] Verdicts come from `score_run` only — never re-implemented in a route.
- [x] `/fi/run` is idempotent on a finished `run_id` (no 500).

## P5 — Dashboard

- [x] `dashboard().open()` opens a native window; falls back to a browser if
      WebView2/pywebview is missing.
- [x] `snapshot()` is headless-testable and does no I/O on the render path.
- [x] One dropdown switches dependency and re-renders every panel.
- [x] Whole board fits one screen — no scrolling.

## P6 — QA + acceptance + rehearsal

- [x] Full suite green against a real Postgres (`419 passed`).
- [x] `tests/test_integration.py` covers the three end-to-end moments:
      k-of-n, the breaker cycle, the storm guard.
- [x] Secret scan clean: `detect-secrets` against the reviewed
      `.secrets.baseline`; `gitleaks` in pre-commit and CI.
- [x] CI (`.github/workflows/ci.yml`) runs the suite against a Postgres
      service and the secret scan.
- [x] `docs/demo-script.md` with click-by-click cues and a 30-second fallback.
- [ ] **Two timed rehearsals on the demo laptop** — the only box that needs a
      human. Tick it after rehearsal 1 and 2.

---

## The one open cross-team item

`store.record_transition()` is called only by the seed. Nothing in the live
call path writes a breaker transition, so a breaker that flips during a real
drill does not land in `breaker_transitions` — and the dashboard, which reads
that table, will not show the flip live. Wiring it belongs to P2/P4; flagged
here so it is not mistaken for a dashboard bug.
