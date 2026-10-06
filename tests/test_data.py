"""
JIZO - Part 3 data-layer tests.

Owner: Aayush (P3).

These cover the things P1's and P2's suites cannot: the mapping from P1's
contracts onto the tables, the config/secret precedence, and one regression
test for a bug that would have crashed the seed on its first run.

**No database is required.** Every test here runs against in-memory models
and pure functions, so the suite stays fast and never flakes - the same
discipline P1's scorer follows. Tests that genuinely need Postgres live
behind `pytest.mark.db` and are skipped by default.

Run:

    .\\.venv\\Scripts\\python.exe -m pytest tests/test_data.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend import FaultType, Phase, Pattern, ServedFrom, BreakerState
from backend.models import (
    BREAKER_STATE_VALUES,
    FAULT_VALUES,
    MODE_VALUES,
    PATTERN_VALUES,
    PHASE_VALUES,
    SERVED_FROM_VALUES,
    ApiRegistryRow,
    Base,
    BreakerTransitionRow,
    FiRunRow,
    RequestLogRow,
)
from backend.seed import RUN_BUILDERS, _policies
from backend.store import (
    event_to_row,
    load_evidence,
    load_run,
    load_run_evidence,
    load_registry,
    record_transition,
    save_run,
    spec_to_row,
)

ROOT = Path(__file__).resolve().parent.parent

#: Doc Sec. 7.6 / PRD Sec. 4 table names. These are a contract: PRD and the
#: build doc both list them, and P4/P5 read them by name.
DOC_TABLES = {"api_registry", "breaker_transitions", "fi_runs", "request_logs"}

#: The five fields P1's scorer reads. Without them no stored run can be
#: re-scored, so their presence on `request_logs` is non-negotiable.
SCORER_FIELDS = (
    "effect_applied",
    "leaked_raw_error",
    "served_from",
    "fault",
    "call_index",
)


# ---------------------------------------------------------------------------
# Table shape vs the documented schema
# ---------------------------------------------------------------------------


def test_four_tables_match_doc_sec_7_6():
    """Exactly the four documented tables exist - no more, no fewer."""
    assert set(Base.metadata.tables) == DOC_TABLES


def test_documented_key_fields_are_present():
    """Key fields from Doc Sec. 7.6 on each table."""
    expected = {
        "api_registry": ["api_key", "base_url", "criticality", "owner"],
        "breaker_transitions": [
            "ts", "api_key", "from_state", "to_state", "error_pct", "reason",
        ],
        "fi_runs": [
            "run_id", "pattern", "target_api", "target_k", "target_phase",
            "ts", "cw", "ps", "prem", "miss", "mult",
        ],
        "request_logs": [
            "ts", "trace_id", "api_key", "latency_ms", "status_code",
            "attempt", "breaker_state",
        ],
    }
    for table, fields in expected.items():
        cols = {c.name for c in Base.metadata.tables[table].columns}
        missing = [f for f in fields if f not in cols]
        assert not missing, f"{table} is missing {missing}"


def test_request_logs_carries_the_five_scorer_fields():
    """A table built to Sec. 7.6's abbreviated list could not re-score a run."""
    cols = {c.name for c in Base.metadata.tables["request_logs"].columns}
    for field in SCORER_FIELDS:
        assert field in cols, f"scorer field {field} missing from request_logs"


def test_vocabulary_matches_p1_enums_exactly():
    """The CHECK vocabularies must not drift from P1's frozen enums.

    If P1 adds a phase or fault type and these are not updated, every insert
    of the new value would be rejected by the database.
    """
    assert set(PHASE_VALUES) == {p.value for p in Phase}
    assert set(FAULT_VALUES) == {f.value for f in FaultType}
    assert set(SERVED_FROM_VALUES) == {s.value for s in ServedFrom}
    assert set(BREAKER_STATE_VALUES) == {b.value for b in BreakerState}
    assert set(PATTERN_VALUES) == {p.value for p in Pattern}
    assert set(MODE_VALUES) == {"control", "experiment"}


def test_api_registry_defaults_match_doc_sec_11_4():
    """Sec. 11.4 is the designated source: 'Do not invent thresholds'."""
    expected = {
        "timeout_s": 3.0,
        "max_attempts": 3,
        "backoff_initial_s": 0.075,
        "backoff_max_s": 1.8,
        "jitter_s": 0.05,
        "breaker_window": 100,
        "breaker_error_threshold": 0.25,
        "breaker_min_volume": 20,
        "breaker_sleep_s": 10.0,
        "half_open_probes": 10,
        "half_open_window_s": 5.0,
    }
    for name, value in expected.items():
        col = Base.metadata.tables["api_registry"].columns[name]
        default = getattr(col.default, "arg", None) if col.default is not None else None
        if hasattr(default, "value"):
            default = default.value
        assert default == value, f"api_registry.{name} default {default} != {value}"


def test_fi_runs_has_ts_consistency_constraint():
    """TS must equal its own definition, enforced in the schema.

    This is what makes a stored verdict auditable: no writer can persist a
    self-inconsistent score.
    """
    sql = " ".join(str(c.sqltext) for c in Base.metadata.tables["fi_runs"].constraints
                   if hasattr(c, "sqltext"))
    assert "cw AND ps" in sql.replace("\n", " ")
    assert "NOT misses" not in sql  # sanity: no typo'd column name
    assert "prem" in sql and "miss" in sql and "mult" in sql


def test_attempt_check_is_one_based():
    """attempt=1 is the first try, matching P2's resilient_get.

    An earlier revision used 0-based and disagreed with P2's live logs for
    the same field; this pins the convention.
    """
    sql = " ".join(
        str(c.sqltext) for c in Base.metadata.tables["request_logs"].constraints
        if hasattr(c, "sqltext")
    )
    assert "attempt >= 1" in sql
    assert "attempt >= 0" not in sql


def test_breaker_transition_cannot_be_a_no_op():
    """A 'transition' between identical states is a caller bug."""
    sql = " ".join(
        str(c.sqltext) for c in Base.metadata.tables["breaker_transitions"].constraints
        if hasattr(c, "sqltext")
    )
    assert "from_state <> to_state" in sql


# ---------------------------------------------------------------------------
# Migration parity - the migration is the source of truth for deployments
# ---------------------------------------------------------------------------


def _migration_source() -> str:
    return (ROOT / "migrations" / "versions" / "0001_initial.py").read_text(
        encoding="utf-8"
    )


def test_migration_creates_every_documented_table():
    src = _migration_source()
    for table in DOC_TABLES:
        assert f'"{table}"' in src, f"migration never creates {table}"


def test_migration_matches_models_on_attempt_check():
    """Models and migration must agree, or a fresh deploy differs from dev."""
    src = _migration_source()
    assert "attempt >= 1" in src
    assert "attempt >= 0" not in src


def test_migration_downgrade_drops_everything():
    """A migration without a working downgrade is a trap."""
    src = _migration_source()
    downgrade = src.split("def downgrade")[-1]
    for table in DOC_TABLES:
        assert f'"{table}"' in downgrade, f"downgrade does not drop {table}"


# ---------------------------------------------------------------------------
# EvidenceEvent -> request_logs mapping
# ---------------------------------------------------------------------------


def _one_event(**kwargs):
    """Build a bus with one event and return its mapped row."""
    from backend import EvidenceBus

    bus = EvidenceBus()
    event = bus.record("trace-x", "weather", kwargs.pop("phase", Phase.RECV), **kwargs)
    return event_to_row(event)


def test_event_maps_onto_request_logs_field_for_field():
    row = _one_event(
        served_from=ServedFrom.CACHE,
        status_code=503,
        effect_applied=True,
        fault=FaultType.HTTP_503,
        latency_ms=12.5,
        leaked_raw_error=True,
        note="raw error leaked",
    )
    assert row.trace_id == "trace-x"
    assert row.api_key == "weather"
    assert row.phase == "recv"
    assert row.served_from == "cache"
    assert row.status_code == 503
    assert row.effect_applied is True
    assert row.fault == "http_503"
    assert row.latency_ms == 12.5
    assert row.leaked_raw_error is True
    assert row.note == "raw error leaked"


def test_event_mapping_uses_enum_values_not_enum_reprs():
    """The database stores the `.value` strings, never 'Phase.RECV'."""
    row = _one_event(served_from=ServedFrom.LIVE)
    assert row.phase in PHASE_VALUES
    assert row.served_from in SERVED_FROM_VALUES
    assert "Phase." not in row.phase


def test_event_mapping_leaves_absent_fault_as_none():
    row = _one_event(served_from=ServedFrom.LIVE)
    assert row.fault is None
    assert row.breaker_state is None


def test_event_mapping_defaults_attempt_and_mode_honestly():
    """Sec. 7.3 requires attempt and mode; the bus carries neither, so the
    The defaults are the honest "we do not know" values: the first
    attempt, and no mode - guessing `control` would corrupt P4's
    control-vs-experiment comparison."""
    row = _one_event(served_from=ServedFrom.LIVE)
    assert row.attempt == 1
    assert row.mode is None


def test_event_mapping_records_mode_guard_and_attempt_when_supplied():
    """P4 owns these three; it must be able to set them explicitly."""
    from backend import EvidenceBus

    bus = EvidenceBus()
    event = bus.record("trace-x", "weather", Phase.RECV,
                       served_from=ServedFrom.LIVE)
    row = event_to_row(
        event, mode="experiment", attempt=3, guard="After(recv_weather#1)"
    )
    assert row.mode == "experiment"
    assert row.attempt == 3
    assert row.guard == "After(recv_weather#1)"


# ---------------------------------------------------------------------------
# DrillSpec -> fi_runs, and the verdict
# ---------------------------------------------------------------------------


def test_spec_row_leaves_score_columns_null():
    """The spec is written when a run starts, before any verdict exists.

    This is what lets a run that crashes mid-flight still hold its intent.
    """
    result, _ = RUN_BUILDERS[0]()
    row = spec_to_row(result.spec)
    assert row.ts is None
    assert row.cw is None
    assert row.mult is None
    assert row.timeline is None


def test_spec_row_flattens_the_target_and_guard():
    result, _ = RUN_BUILDERS[0]()
    row = spec_to_row(result.spec)
    assert row.target_api == result.spec.target.api_key
    assert row.target_k == result.spec.target.occurrence
    assert row.target_phase == result.spec.target.phase.value
    assert row.guard_min_count == result.spec.guard.min_count


def _database_or_skip():
    """Skip the test when no database is reachable.

    Deliberately does NOT return a session factory: a factory built during
    this probe is bound to the probe's event loop, and handing it to a later
    `asyncio.run()` fails with "Event loop is closed". Each test creates its
    own factory *inside* the coroutine it actually uses, and the engine
    rebuilds itself when the loop changes (see backend/db.py).
    """
    import asyncio

    from sqlalchemy import text

    from backend.db import get_sessionmaker

    async def probe():
        try:
            async with get_sessionmaker()() as s:
                await s.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    if not asyncio.run(probe()):
        pytest.skip("no database reachable")


def _probe_run(tag: str):
    """A demo run under a unique run_id, so tests never touch seeded rows.

    An earlier version reused the seeded `run_id` and deleted it for cleanup,
    which made the tests order-dependent: whichever ran first removed the row
    the next one expected.
    """
    result, events = RUN_BUILDERS[0]()
    run_id = f"pytest-probe-{tag}"
    spec = result.spec.model_copy(update={"run_id": run_id})
    scored = result.model_copy(update={"run_id": run_id, "spec": spec})
    # Namespace the evidence too. The demo builders all reuse one fixed
    # trace_id ("seed-k-of-n"), so reusing it here would append the probe's
    # request_logs rows to the seeded run's trace and leak them on every
    # cleanup. The timeline sold to `load_run` comes from `scored.timeline`,
    # not from these rows, so re-pointing them changes nothing about scoring.
    events = [e.model_copy(update={"trace_id": run_id}) for e in events]
    return spec, scored, events


async def _drop_run(session, run_id: str) -> None:
    from sqlalchemy import delete

    from backend.models import FiRunRow, RequestLogRow

    # Evidence is keyed by the probe trace_id, which `_probe_run` sets to the
    # run_id; drop it too so repeated runs never accumulate request_logs rows.
    await session.execute(
        delete(RequestLogRow).where(RequestLogRow.trace_id == run_id)
    )
    row = await session.get(FiRunRow, run_id)
    if row is not None:
        await session.delete(row)
    await session.commit()


def test_save_run_uses_one_row_and_never_duplicates_the_key():
    """Regression: the original seed inserted the spec, then a SECOND row with
    the same run_id -> `duplicate key value violates unique constraint
    "fi_runs_pkey"` on the very first run.

    `run_id` is the primary key, so intent and verdict share one row.
    """
    import asyncio

    from sqlalchemy import func, select

    from backend.db import get_sessionmaker
    from backend.models import FiRunRow

    _database_or_skip()

    async def scenario():
        spec, scored, events = _probe_run("onerow")
        factory = get_sessionmaker()
        async with factory() as s:
            await _drop_run(s, spec.run_id)
            try:
                await save_run(s, spec, scored, events)
                await save_run(s, spec, scored, events)   # idempotent re-save
                count = (
                    await s.execute(
                        select(func.count()).select_from(FiRunRow).where(
                            FiRunRow.run_id == spec.run_id
                        )
                    )
                ).scalar_one()
            finally:
                await _drop_run(s, spec.run_id)
            return count

    assert asyncio.run(scenario()) == 1


def test_save_run_matches_the_scorer():
    """The stored verdict must equal what P1's scorer decided."""
    import asyncio

    from backend.db import get_sessionmaker

    _database_or_skip()

    async def scenario():
        spec, scored, events = _probe_run("verdict")
        factory = get_sessionmaker()
        async with factory() as s:
            await _drop_run(s, spec.run_id)
            try:
                row = await save_run(s, spec, scored, events)
                stored = (row.ts, row.cw, row.ps, row.prem, row.miss, row.mult)
            finally:
                await _drop_run(s, spec.run_id)
        expected = (scored.ts, scored.cw, scored.ps, scored.prem,
                    scored.miss, scored.mult)
        return stored, expected

    assert asyncio.run(scenario())[0] == asyncio.run(scenario())[1]


def test_save_run_stores_a_rehydratable_timeline_and_spec():
    """A stored run must be re-explainable without re-running it.

    Proven by reading it back through `load_run` and re-grading the restored
    evidence with P1's real scorer.
    """
    import asyncio

    from backend.db import get_sessionmaker
    from backend.scoring import score_run

    _database_or_skip()

    async def scenario():
        spec, scored, events = _probe_run("rehydrate")
        factory = get_sessionmaker()
        async with factory() as s:
            await _drop_run(s, spec.run_id)
            try:
                await save_run(s, spec, scored, events)
                restored = await load_run(s, spec.run_id)
                assert restored is not None
                assert len(restored.timeline) == len(scored.timeline)
                assert restored.spec is not None
                # The restored evidence must re-grade to the same verdict.
                ok = score_run(restored.spec, restored.timeline).ts == scored.ts
            finally:
                await _drop_run(s, spec.run_id)
            return ok

    assert asyncio.run(scenario()) is True


def test_save_run_repairs_an_incomplete_run():
    """Regression: the old seed committed the spec and the verdict in two
    separate transactions, so a crash between them left `ts IS NULL` forever -
    the next run saw "already present" and skipped it.
    """
    import asyncio

    from sqlalchemy import update

    from backend.db import get_sessionmaker
    from backend.models import FiRunRow

    _database_or_skip()

    async def scenario():
        spec, scored, events = _probe_run("repair")
        factory = get_sessionmaker()
        async with factory() as s:
            await _drop_run(s, spec.run_id)
            try:
                await save_run(s, spec, scored, events)

                # Simulate the crash: strip the verdict but keep the intent.
                await s.execute(
                    update(FiRunRow).where(FiRunRow.run_id == spec.run_id)
                    .values(ts=None, timeline=None)
                )
                await s.commit()
                assert await load_run(s, spec.run_id) is None

                # Re-saving must repair it, not skip it.
                await save_run(s, spec, scored, events)
                repaired = await load_run(s, spec.run_id)
                ok = repaired is not None and repaired.ts == scored.ts
            finally:
                await _drop_run(s, spec.run_id)
            return ok

    assert asyncio.run(scenario()) is True


def test_record_transition_converts_an_aware_timestamp():
    """Regression: an aware datetime into a naive column is an asyncpg
    DataError ("can't subtract offset-naive and offset-aware datetimes")."""
    import asyncio
    import datetime as dt

    from backend.db import get_sessionmaker
    from backend.models import BreakerTransitionRow

    _database_or_skip()

    async def scenario():
        factory = get_sessionmaker()
        async with factory() as s:
            row = await record_transition(
                s, api_key="weather", from_state="CLOSED", to_state="OPEN",
                error_pct=42.5, reason="pytest tz probe",
                ts=dt.datetime.now(dt.timezone.utc),
            )
            stored = row.ts
            await s.delete(row)
            await s.commit()
        return stored

    stored = asyncio.run(scenario())
    assert stored.tzinfo is None, "aware datetime reached the naive column"


def test_load_registry_round_trips_a_policy():
    """`api_registry` -> P1 `ApiPolicy` -> P2's registry, the bridge that lets
    the table actually affect a live call."""
    import asyncio

    from backend.db import get_sessionmaker

    _database_or_skip()

    async def scenario():
        factory = get_sessionmaker()
        async with factory() as s:
            return await load_registry(s)

    policies = asyncio.run(scenario())
    assert policies, "api_registry is empty - run the seed"
    for p in policies:
        assert p.api_key
        assert p.timeout_s > 0
        assert 0 < p.breaker_error_threshold <= 1


def test_every_demo_run_passes_and_records_its_intent():
    for builder in RUN_BUILDERS:
        result, events = builder()
        assert result.ts is True, f"{result.run_id} does not pass: {result.explain()}"
        assert result.spec is not None
        assert events, "a demo run with no evidence proves nothing"


# ---------------------------------------------------------------------------
# Registry policies - must resolve for P2's proxy
# ---------------------------------------------------------------------------


def test_policies_use_p2s_api_key_names():
    """P2's proxy resolves policies via load_policy(api_key).

    A registry row under any other name is unreachable, so these names are a
    cross-part contract, not a preference.
    """
    names = {p.api_key for p in _policies()}
    assert "weather" in names
    assert "geocode" in names


def test_every_p2_registry_key_has_a_p3_row():
    """The whole point of the registry table: P2's keys must be seeded."""
    from backend.config import registered_policies

    p3 = {p.api_key for p in _policies()}
    missing = set(registered_policies()) - p3
    assert not missing, f"P2 keys with no api_registry row: {sorted(missing)}"


def test_payment_policy_is_marked_not_idempotent():
    """The post-effect pattern exists because a write must not be retried
    blindly, so this flag is the point of the policy, not decoration."""
    payment = {p.api_key: p for p in _policies()}["payment"]
    assert payment.idempotent is False
    assert payment.criticality == "high"


def test_geocode_policy_keeps_nominatim_courtesy_limit():
    """Doc Sec. 10: Nominatim requires <= 1 rps."""
    geocode = {p.api_key: p for p in _policies()}["geocode"]
    assert geocode.courtesy_rps == 1


# ---------------------------------------------------------------------------
# Health - must never raise, and must never leak a secret
# ---------------------------------------------------------------------------


def test_health_returns_a_report_and_never_raises():
    """A health check that throws tells a probe nothing.

    Whether or not Postgres is up, this must return a dict describing the
    truth rather than propagating an exception.
    """
    import asyncio

    from backend.health import check_health

    report = asyncio.run(check_health())
    assert isinstance(report, dict)
    assert "ok" in report
    assert "database" in report
    assert "tables" in report
    assert report["status"] in {"healthy", "degraded", "unhealthy"}


def test_health_report_never_contains_the_password():
    """The report is served to a browser, so the DSN must be masked."""
    import asyncio

    from backend.health import check_health

    report = asyncio.run(check_health())
    url = report["database"]["url"]
    assert "postgres" in url or url == "(not configured)"
    if "://" in url and "@" in url:
        password_part = url.split("://", 1)[1].split("@", 1)[0].split(":", 1)[-1]
        assert password_part in {"***", ""} or password_part == "(not configured)"


def test_ready_is_stricter_than_health():
    """Readiness requires policy loaded, not merely a live connection."""
    import asyncio

    from backend.health import check_ready

    report = asyncio.run(check_ready())
    assert "ready" in report
    assert isinstance(report["ready"], bool)
    assert report["reason"]


# ---------------------------------------------------------------------------
# Config / secrets precedence
# ---------------------------------------------------------------------------


def test_env_parser_tolerates_a_utf8_bom():
    """PowerShell's `Set-Content -Encoding UTF8` writes a BOM.

    Without `utf-8-sig`, the FIRST key silently becomes '\\ufeffDATABASE_URL'
    and never resolves - which every Windows teammate would hit.
    """
    import tempfile

    from backend.secrets import _parse_env_file

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / ".env"
        path.write_bytes(b"\xef\xbb\xbfDATABASE_URL=postgresql+asyncpg://x/y\n")
        values = _parse_env_file(path)
        assert values.get("DATABASE_URL") == "postgresql+asyncpg://x/y"


def test_env_parser_ignores_comments_and_blank_lines():
    import tempfile

    from backend.secrets import _parse_env_file

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / ".env"
        path.write_text(
            "# a comment\n\nKEY=value\nBAD_LINE_NO_EQUALS\n", encoding="utf-8"
        )
        values = _parse_env_file(path)
        assert values == {"KEY": "value"}


def test_env_parser_keeps_equals_signs_inside_values():
    """A URL containing '=' must survive intact."""
    import tempfile

    from backend.secrets import _parse_env_file

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / ".env"
        path.write_text("TOKEN=abc=def=ghi\n", encoding="utf-8")
        assert _parse_env_file(path)["TOKEN"] == "abc=def=ghi"


def test_missing_env_file_yields_no_values_rather_than_raising():
    from backend.secrets import _parse_env_file

    assert _parse_env_file(Path("does-not-exist-anywhere.env")) == {}


def test_env_example_declares_the_documented_variable_names():
    """Appendix A.1 is paste-ready, so the template must match it."""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in (
        "UPSTREAM_TIMEOUT",
        "MAX_ATTEMPTS",
        "OPENMETEO_BASE",
        "NOMINATIM_BASE",
        "NOMINATIM_UA",
        "DATABASE_URL",
        "AZURE_KEYVAULT_URL",
    ):
        assert re.search(rf"^{key}=", text, re.M), f"{key} missing from .env.example"


def test_upstreams_are_keyless_so_no_api_key_vars_exist():
    """Doc Sec. 1: the demo upstreams need no auth.

    An invented WEATHER_API_KEY would be sent to a keyless API and ignored.
    """
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    # Match assignments, not substrings: the file explains in a comment that
    # these variables deliberately do not exist.
    assert not re.search(r"^\s*WEATHER_API_KEY=", text, re.M)
    assert not re.search(r"^\s*GEOCODE_API_KEY=", text, re.M)


def test_database_url_uses_the_async_driver():
    """A plain postgresql:// URL builds a sync engine and fails at connect."""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    match = re.search(r"^DATABASE_URL=(\S+)", text, re.M)
    assert match, "DATABASE_URL missing"
    assert "+asyncpg" in match.group(1)


def test_db_module_rejects_a_sync_url():
    """Better a clear error here than a confusing asyncpg driver failure."""
    from backend.db import get_engine
    import backend.db as db

    db._engine = None
    db._sessionmaker = None
    with pytest.raises(RuntimeError, match="asyncpg"):
        get_engine("postgresql://user:pw@localhost/db")


def test_docker_compose_matches_appendix_a1():
    """Credentials and the readiness probe must line up with Appendix A.1.

    Deliberately text-based: PyYAML is not a project dependency, and adding
    one for a single assertion is not worth it. The parse-level check is
    `docker compose config`, which CI should run.
    """
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "POSTGRES_DB: resilient" in text
    assert "POSTGRES_USER: postgres" in text

    # The readiness probe must reference the same db/user, or it reports
    # "no response" forever while the database is actually fine.
    # Match the assignment line only - the file's comment also mentions
    # `pg_isready`, and a bare substring check would match that too.
    probe = [ln.strip() for ln in text.splitlines() if "pg_isready -U postgres" in ln]
    assert probe, "no pg_isready healthcheck referencing the postgres user"
    assert "resilient" in probe[0]


def test_docker_compose_ports_is_a_list_not_a_scalar():
    """Regression: a YAML comment between `ports:` and its first item makes
    `ports` parse as the string "5432:5432" instead of a list, and
    `docker compose` rejects the whole file with 'must be a array'.
    """
    lines = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").splitlines()

    ports_index = next(
        i for i, ln in enumerate(lines) if ln.strip() == "ports:"
    )
    # The next non-comment, non-blank line must be a list item.
    for follow in lines[ports_index + 1:]:
        stripped = follow.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert stripped.startswith("- "), (
            f"ports value is not a list item: {stripped!r} - "
            "a comment sits between 'ports:' and its first item"
        )
        break
    else:  # pragma: no cover - the loop always breaks on a real file
        pytest.fail("ports: has no value")

    assert '- "5432:5432"' in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Regressions found by independent review (each one was a real failure)
# ---------------------------------------------------------------------------


def test_key_vault_value_is_not_clobbered_by_the_local_default(monkeypatch):
    """Regression: Key Vault was fetched, then silently overwritten.

    `values` is {**DEFAULTS, **env_file} and DEFAULTS always holds a
    DATABASE_URL, so the final precedence loop re-assigned it every time -
    defeating the vault entirely in cloud, and connecting to the wrong
    database.
    """
    from backend import secrets as sec

    monkeypatch.setenv("AZURE_KEYVAULT_URL", "https://fake.vault.azure.net/")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        sec, "_parse_env_file", lambda path: {}
    )
    monkeypatch.setattr(
        sec,
        "_from_key_vault",
        lambda uri: {"DATABASE_URL": "postgresql+asyncpg://vault:5432/prod"},
    )

    resolved = sec._load()
    assert resolved["DATABASE_URL"] == "postgresql+asyncpg://vault:5432/prod"


def test_real_env_var_still_beats_key_vault(monkeypatch):
    """Precedence is env > vault > .env > default; the vault fix must not
    have promoted the vault above a real environment variable."""
    from backend import secrets as sec

    monkeypatch.setenv("AZURE_KEYVAULT_URL", "https://fake.vault.azure.net/")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://env:5432/from_env")
    monkeypatch.setattr(sec, "_parse_env_file", lambda path: {})
    monkeypatch.setattr(
        sec,
        "_from_key_vault",
        lambda uri: {"DATABASE_URL": "postgresql+asyncpg://vault:5432/prod"},
    )

    assert sec._load()["DATABASE_URL"] == "postgresql+asyncpg://env:5432/from_env"


def test_health_error_field_is_scrubbed_of_the_password(monkeypatch):
    """Regression: a generic exception's text was copied into the report.

    `db.py`'s driver guard embeds the raw URL in its message, and that guard
    fires on the single most common misconfiguration - so the report leaked
    the database password to any browser that could reach /health.
    """
    import types

    from backend import health

    dsn = "postgresql://admin:SUPERSECRET@db.internal:5432/prod"
    monkeypatch.setattr(
        health, "get_config", lambda: types.SimpleNamespace(database_url=dsn)
    )

    scrubbed = health._scrub(
        f"RuntimeError: DATABASE_URL must use the async driver: {dsn!r}"
    )
    assert "SUPERSECRET" not in scrubbed
    assert "***" in scrubbed


def test_env_parser_does_not_raise_on_a_non_utf8_file():
    """Regression: a UTF-16 `.env` (Windows Notepad's "Unicode" save) raised
    UnicodeDecodeError out of check_health(), whose contract is never to
    raise."""
    import tempfile

    from backend.secrets import _parse_env_file

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / ".env"
        path.write_bytes("DATABASE_URL=x\n".encode("utf-16"))
        assert _parse_env_file(path) == {}


def test_sessionmaker_rebuilds_when_the_event_loop_changes(monkeypatch):
    """Regression: the cached engine was bound to the first event loop.

    A second `asyncio.run()` therefore found a dead pool and /health
    reported a perfectly healthy database as unreachable.
    """
    import asyncio
    import types

    from backend import db

    monkeypatch.setattr(
        db,
        "get_config",
        lambda: types.SimpleNamespace(
            database_url="postgresql+asyncpg://u:p@localhost/x"
        ),
    )
    db._engine = None
    db._sessionmaker = None
    db._engine_loop = None

    async def grab():
        return db.get_sessionmaker()

    first = asyncio.run(grab())
    second = asyncio.run(grab())
    assert first is not second, "sessionmaker survived an event-loop change"

    db._engine = None
    db._sessionmaker = None
    db._engine_loop = None


def test_seed_never_calls_create_all():
    """Regression: `Base.metadata.create_all` built a schema that lacked every
    server default and had no `alembic_version` row, so `alembic upgrade head`
    then failed with DuplicateTableError. Alembic is the only schema
    authority."""
    import inspect
    import re

    from backend import seed as seed_mod

    src = inspect.getsource(seed_mod)
    # Look for an invocation, not the word: the module docstring explains
    # why create_all is NOT used, and that explanation must stay.
    assert not re.search(r"create_all\s*\(", src), "seed still calls create_all()"


def test_seed_requires_the_migration_with_a_clear_message():
    """A missing schema must explain itself rather than raising a driver
    error."""
    import inspect

    from backend import seed as seed_mod

    src = inspect.getsource(seed_mod)
    assert "alembic upgrade head" in src


def test_health_degraded_and_unreachable_when_a_breaker_is_open():
    """Regression: `reachable` was hard-coded True, so an OPEN breaker was
    published as reachable - contradicting Sec. 7.4's own example payload.

    Requires a live database; skipped when none is reachable.
    """
    import asyncio

    from backend.db import get_sessionmaker
    from backend.health import check_health
    from backend.models import BreakerTransitionRow

    async def scenario():
        from sqlalchemy import select

        factory = get_sessionmaker()
        try:
            async with factory() as s:
                await s.execute(select(BreakerTransitionRow).limit(1))
        except Exception:
            pytest.skip("no database reachable")

        async with factory() as s:
            s.add(
                BreakerTransitionRow(
                    api_key="weather", from_state="CLOSED", to_state="OPEN",
                    error_pct=42.5, reason="pytest regression probe",
                )
            )
            await s.commit()

        report = await check_health()

        async with factory() as s:
            for row in (
                await s.execute(
                    select(BreakerTransitionRow).where(
                        BreakerTransitionRow.reason == "pytest regression probe"
                    )
                )
            ).scalars():
                await s.delete(row)
            await s.commit()

        return report

    report = asyncio.run(scenario())
    assert report["status"] == "degraded"
    assert report["ok"] is False
    assert report["dependencies"]["weather"]["breaker"] == "OPEN"
    assert report["dependencies"]["weather"]["reachable"] is False


# ---------------------------------------------------------------------------
# Regressions from the independent verification pass
# ---------------------------------------------------------------------------


def _linked_probe(tag: str):
    """A probe run whose `fi_runs.trace_id` and evidence share one trace.

    `_probe_run` re-points the events at a unique trace but leaves
    `result.timeline` holding the builder's original trace, so the stored
    timeline and the stored evidence would disagree. These tests are about
    the run<->evidence link, so they keep the two consistent.
    """
    result, events = RUN_BUILDERS[0]()
    run_id = f"pytest-linked-{tag}"
    events = [e.model_copy(update={"trace_id": run_id}) for e in events]
    spec = result.spec.model_copy(update={"run_id": run_id})
    scored = result.model_copy(
        update={"run_id": run_id, "spec": spec, "timeline": events}
    )
    return spec, scored, events


async def _drop_linked(session, run_id: str) -> None:
    from sqlalchemy import delete

    from backend.models import FiRunRow, RequestLogRow

    row = await session.get(FiRunRow, run_id)
    if row is not None:
        await session.delete(row)
    await session.execute(
        delete(RequestLogRow).where(RequestLogRow.trace_id == run_id)
    )
    await session.commit()


def test_save_run_writes_evidence_when_repairing_a_spec_only_run():
    """Regression (review finding A1): evidence was written only when the
    `fi_runs` row was new.

    A spec-only row - created via the public `spec_to_row()` at run start, a
    legitimate state - would then be repaired with a verdict but never get
    its evidence. The stored run said PASS while re-grading the evidence
    table said FAIL, because the table was empty.
    """
    import asyncio

    from sqlalchemy import func, select

    from backend.db import get_sessionmaker
    from backend.models import RequestLogRow
    from backend.scoring import score_run

    _database_or_skip()

    async def scenario():
        spec, scored, events = _linked_probe("speconly")
        factory = get_sessionmaker()
        async with factory() as s:
            await _drop_linked(s, spec.run_id)
            try:
                # Intent recorded first, no evidence yet.
                s.add(spec_to_row(spec))
                await s.commit()
                before = (
                    await s.execute(
                        select(func.count()).select_from(RequestLogRow).where(
                            RequestLogRow.trace_id == spec.run_id
                        )
                    )
                ).scalar_one()

                await save_run(s, spec, scored, events)

                stored = await load_run(s, spec.run_id)
                evidence = await load_evidence(s, spec.run_id)
                regraded = score_run(stored.spec, evidence)
            finally:
                await _drop_linked(s, spec.run_id)

            return before, len(evidence), regraded.ts == stored.ts

    before, count, regrades = asyncio.run(scenario())
    assert before == 0, "fixture was not actually spec-only"
    assert count == len(_linked_probe("speconly")[2])
    assert regrades, "evidence-based regrade disagreed with the stored verdict"


def test_save_run_does_not_duplicate_evidence_left_by_a_deleted_run():
    """Regression (review finding A2): `request_logs` has no unique key, so
    deleting a `fi_runs` row while leaving its evidence behind - then
    re-saving - silently doubled the evidence (9 rows became 18)."""
    import asyncio

    from sqlalchemy import delete, func, select

    from backend.db import get_sessionmaker
    from backend.models import FiRunRow, RequestLogRow

    _database_or_skip()

    async def scenario():
        spec, scored, events = _linked_probe("nodup")
        factory = get_sessionmaker()
        async with factory() as s:
            await _drop_linked(s, spec.run_id)
            try:
                await save_run(s, spec, scored, events)
                first = (
                    await s.execute(
                        select(func.count()).select_from(RequestLogRow).where(
                            RequestLogRow.trace_id == spec.run_id
                        )
                    )
                ).scalar_one()

                # Delete only the run, leaving orphaned evidence.
                await s.execute(
                    delete(FiRunRow).where(FiRunRow.run_id == spec.run_id)
                )
                await s.commit()

                await save_run(s, spec, scored, events)
                second = (
                    await s.execute(
                        select(func.count()).select_from(RequestLogRow).where(
                            RequestLogRow.trace_id == spec.run_id
                        )
                    )
                ).scalar_one()
            finally:
                await _drop_linked(s, spec.run_id)
            return first, second

    first, second = asyncio.run(scenario())
    assert first == second, f"evidence duplicated on re-save: {first} -> {second}"


def test_load_run_evidence_resolves_the_trace_from_the_run_id():
    """Regression (review finding A2): `fi_runs` had no `trace_id`, so a
    caller holding only a `run_id` could not find the run's evidence."""
    import asyncio

    from backend.db import get_sessionmaker
    from backend.scoring import score_run

    _database_or_skip()

    async def scenario():
        spec, scored, events = _linked_probe("link")
        factory = get_sessionmaker()
        async with factory() as s:
            await _drop_linked(s, spec.run_id)
            try:
                await save_run(s, spec, scored, events)

                run = await load_run(s, spec.run_id)
                assert run is not None
                # Resolved with no prior knowledge of the trace id.
                resolved = await load_run_evidence(s, spec.run_id)

                # Re-saving without events must not drop the link.
                await save_run(s, spec, scored)
                after = await load_run_evidence(s, spec.run_id)

                regrades = score_run(run.spec, resolved).ts == run.ts
            finally:
                await _drop_linked(s, spec.run_id)
            return len(resolved), len(after), regrades

    resolved, after, regrades = asyncio.run(scenario())
    assert resolved > 0, "no evidence resolved from the run id"
    assert after == resolved, "the link was lost on a save with no events"
    assert regrades, "evidence resolved via the run id did not re-grade"


def test_scrub_masks_the_password_without_mangling_the_scheme(monkeypatch):
    """Regression (review finding A5): `_scrub` replaced the bare password
    everywhere, so the project's own password `postgres` turned `postgresql`
    into `***ql`. That leaked nothing but made the error unreadable."""
    import types

    from backend import health

    dsn = "postgresql://postgres:postgres@localhost:5432/resilient"
    monkeypatch.setattr(
        health, "get_config", lambda: types.SimpleNamespace(database_url=dsn)
    )

    scrubbed = health._scrub(f"driver error: {dsn!r}")
    assert "postgresql" in scrubbed, "the scheme was mangled"
    assert "postgres:postgres@" not in scrubbed, "the password was not masked"
    assert ":***@" in scrubbed


def test_record_transition_does_not_rescale_error_pct():
    """`error_pct` is 0-100, but the CHECK only rejects <0 and >100, so a
    caller passing a fraction is accepted silently. We store what we are
    given rather than guessing a unit - the docstring says so, and this test
    pins the behaviour so the two cannot disagree again."""
    import asyncio

    from backend.db import get_sessionmaker
    from backend.models import BreakerTransitionRow

    _database_or_skip()

    async def scenario():
        factory = get_sessionmaker()
        async with factory() as s:
            row = await record_transition(
                s, api_key="weather", from_state="CLOSED", to_state="OPEN",
                error_pct=0.25, reason="pytest fraction probe",
            )
            stored = row.error_pct
            await s.delete(row)
            await s.commit()
        return stored

    assert asyncio.run(scenario()) == 0.25, "value was silently rescaled"
