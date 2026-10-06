"""
Tests for the JIZO control-vs-experiment comparator.

Owner: Riya (P4). Implementation: `backend/compare.py`.

Scope: this file tests `compare.py` and nothing else. It deliberately does not
touch `main.py`, the scorer's verdicts, or the database, because the comparator
is pure by design - `compare_sides()` takes evidence and returns a report, so
every case here runs in milliseconds on stub evidence.

The organising question is always the same one a judge will ask:

    did protection actually change anything?

A comparator that cannot show a difference is as useless as one that shows a
false one, so most groups here assert the DELTA, not just the two sides.

Run:
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_compare.py -v
"""

from __future__ import annotations

import pytest

from backend import (
    EvidenceBus,
    EvidenceEvent,
    FaultType,
    Phase,
    ScoreResult,
    ServedFrom,
    k_of_n_drill,
    score_run,
)
from backend.compare import (
    DEFAULT_MODE,
    MODES,
    CompareResult,
    SideMetrics,
    compare_sides,
    resolve_mode,
    side_delta,
    side_metrics,
)


WEATHER = "weather"
GEOCODE = "geocode"


# ---------------------------------------------------------------------------
# Fixtures: evidence recorded through the real bus, so the comparator is fed
# the same shapes P2's proxy would emit.
# ---------------------------------------------------------------------------


def served_call(bus: EvidenceBus, trace: str, api: str, served: ServedFrom) -> None:
    """One call that got an answer, of the given kind."""
    bus.record(trace, api, Phase.SEND)
    bus.record(trace, api, Phase.RECV, served_from=served, status_code=200)


def dead_call(bus: EvidenceBus, trace: str, api: str) -> None:
    """One call that was sent and never answered at all."""
    bus.record(trace, api, Phase.SEND)


def duplicated_call(bus: EvidenceBus, trace: str, api: str) -> None:
    """One call that applied its side effect twice - the double charge."""
    bus.record(trace, api, Phase.SEND)
    bus.record(trace, api, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, api, Phase.RECV, served_from=ServedFrom.NONE)
    bus.record(trace, api, Phase.POST_EFFECT, effect_applied=True)
    bus.record(trace, api, Phase.RECV, served_from=ServedFrom.LIVE)


def scored(trace: str, api: str, *, ts: bool) -> ScoreResult:
    """A minimal real `ScoreResult`, so TS tests exercise the actual type.

    Built through `score_run` rather than hand-constructed, so the comparator is
    never handed a verdict shape P1 could not produce.
    """
    bus = EvidenceBus()
    bus.record(trace, api, Phase.SEND)
    bus.record(trace, api, Phase.RECV, served_from=ServedFrom.LIVE)
    spec = k_of_n_drill(f"{trace}-spec", api, k=1, n=1)
    result = score_run(spec, bus.events(trace))
    return result.model_copy(update={"ts": ts})


# ---------------------------------------------------------------------------
# Group 1 - the splitter
# ---------------------------------------------------------------------------


def test_resolve_mode_accepts_both_arms() -> None:
    assert resolve_mode("control") == "control"
    assert resolve_mode("experiment") == "experiment"


def test_resolve_mode_defaults_to_the_protected_arm() -> None:
    """The safe default matters: `control` serves real traffic unprotected."""
    assert resolve_mode(None) == DEFAULT_MODE == "experiment"
    assert resolve_mode("") == DEFAULT_MODE
    assert resolve_mode("   ") == DEFAULT_MODE


def test_resolve_mode_normalises_case_and_padding() -> None:
    assert resolve_mode("  Control ") == "control"


def test_resolve_mode_rejects_a_typo_instead_of_guessing() -> None:
    """A near-miss must fail loudly, not silently route live traffic."""
    with pytest.raises(ValueError, match="unknown mode"):
        resolve_mode("experimentation")
    with pytest.raises(ValueError):
        resolve_mode("protected")


def test_modes_match_the_database_vocabulary() -> None:
    """`request_logs.mode` has a CHECK on exactly these two values."""
    from backend.models import MODE_VALUES

    assert set(MODES) == set(MODE_VALUES)


# ---------------------------------------------------------------------------
# Group 2 - what one arm did
# ---------------------------------------------------------------------------


def test_live_only_arm_is_all_success_and_no_fallback() -> None:
    bus = EvidenceBus()
    for _ in range(4):
        served_call(bus, "t", WEATHER, ServedFrom.LIVE)

    metrics = side_metrics(bus.events("t"))

    assert metrics.calls == 4
    assert metrics.success == 4
    assert metrics.fallback == 0
    assert metrics.duplicates == 0
    assert metrics.success_rate == 1.0
    assert metrics.fallback_rate == 0.0


def test_degraded_answers_count_as_both_success_and_fallback() -> None:
    """Cache is a working answer, and also a fallback. Both are true."""
    bus = EvidenceBus()
    served_call(bus, "t", WEATHER, ServedFrom.LIVE)
    served_call(bus, "t", WEATHER, ServedFrom.CACHE)
    served_call(bus, "t", WEATHER, ServedFrom.MESSAGE)

    metrics = side_metrics(bus.events("t"))

    assert metrics.calls == 3
    assert metrics.success == 3, "a clear message is still an answer"
    assert metrics.fallback == 2, "live is not a fallback"
    assert metrics.fallback_rate == pytest.approx(2 / 3)


def test_a_call_that_never_answered_counts_against_success() -> None:
    """Fail safe: a call that gave up has not succeeded."""
    bus = EvidenceBus()
    served_call(bus, "t", WEATHER, ServedFrom.LIVE)
    dead_call(bus, "t", WEATHER)

    metrics = side_metrics(bus.events("t"))

    assert metrics.calls == 2
    assert metrics.success == 1
    assert metrics.success_rate == 0.5
    assert metrics.fallback == 0


def test_the_last_response_row_of_a_call_decides() -> None:
    """Served live, then died on the same call - not a success.

    Same rule as the scorer's `_served_live`. Without it, `any()` would call
    this call healthy.
    """
    bus = EvidenceBus()
    bus.record("t", WEATHER, Phase.SEND)
    bus.record("t", WEATHER, Phase.RECV, served_from=ServedFrom.LIVE)
    bus.record("t", WEATHER, Phase.RECV, served_from=ServedFrom.NONE)

    metrics = side_metrics(bus.events("t"))

    assert metrics.calls == 1
    assert metrics.success == 0


def test_a_duplicate_effect_is_counted_once_per_call() -> None:
    bus = EvidenceBus()
    duplicated_call(bus, "t", WEATHER)

    metrics = side_metrics(bus.events("t"))

    assert metrics.calls == 1
    assert metrics.duplicates == 1
    assert metrics.duplicate_rate == 1.0


def test_two_calls_each_committing_once_is_not_a_duplicate() -> None:
    """Normal traffic. The mistake `Mult` was once wrong about (B7)."""
    bus = EvidenceBus()
    for _ in range(2):
        bus.record("t", WEATHER, Phase.SEND)
        bus.record("t", WEATHER, Phase.POST_EFFECT, effect_applied=True)
        bus.record("t", WEATHER, Phase.RECV, served_from=ServedFrom.LIVE)

    metrics = side_metrics(bus.events("t"))

    assert metrics.duplicates == 0


def test_two_apis_in_one_trace_are_two_separate_calls() -> None:
    """The fan-out case: weather call 1 and geocode call 1 are different calls.

    `EvidenceBus` keys `call_index` on `(trace_id, api_key)`, so keying only on
    `call_index` would collapse these two into one.
    """
    bus = EvidenceBus()
    served_call(bus, "t", WEATHER, ServedFrom.LIVE)
    served_call(bus, "t", GEOCODE, ServedFrom.CACHE)

    metrics = side_metrics(bus.events("t"))

    assert metrics.calls == 2
    assert metrics.success == 2
    assert metrics.fallback == 1


def test_the_caller_filters_by_api_which_is_where_the_split_belongs() -> None:
    """`side_metrics` does not filter - `main.py` hands it one arm already.

    Scoping to an API is the caller's job, exactly as `store.load_evidence`
    takes an optional `api_key`. Pinned so nobody "helpfully" adds a hidden
    filter that would silently drop the other half of a fan-out.
    """
    bus = EvidenceBus()
    served_call(bus, "t", WEATHER, ServedFrom.LIVE)
    served_call(bus, "t", GEOCODE, ServedFrom.NONE)

    everything = side_metrics(bus.events("t"))
    weather_only = side_metrics(
        [e for e in bus.events("t") if e.api_key == WEATHER]
    )

    assert everything.calls == 2
    assert weather_only.calls == 1
    assert weather_only.success == 1


def test_empty_evidence_yields_zero_rates_not_an_exception() -> None:
    metrics = side_metrics([])

    assert metrics.calls == 0
    assert metrics.success_rate == 0.0
    assert metrics.fallback_rate == 0.0
    assert metrics.duplicate_rate == 0.0
    assert metrics.ts_rate is None


def test_ts_rate_is_none_when_no_verdicts_are_supplied() -> None:
    """The honest unknown. Guessing 0.0 would read as a measured failure."""
    bus = EvidenceBus()
    served_call(bus, "t", WEATHER, ServedFrom.LIVE)

    assert side_metrics(bus.events("t")).ts_rate is None


def test_ts_rate_averages_the_supplied_verdicts() -> None:
    results = [scored("a", WEATHER, ts=True), scored("b", WEATHER, ts=False)]

    assert side_metrics([], results=results).ts_rate == 0.5
    assert side_metrics([], results=results).runs == 2


def test_ts_rate_is_read_from_the_result_not_recomputed() -> None:
    """`ScoreResult.ts` is P1's frozen verdict; this module never re-derives it."""
    result = scored("a", WEATHER, ts=True)

    assert side_metrics([], results=[result]).ts_rate == 1.0


# ---------------------------------------------------------------------------
# Group 3 - the delta
# ---------------------------------------------------------------------------


def test_delta_is_experiment_minus_control() -> None:
    control = side_metrics([])
    experiment = side_metrics([])

    assert side_delta(control, experiment).success == 0.0


def test_protection_shows_up_as_a_positive_ts_delta() -> None:
    """The headline claim: protection converted a failing run into a passing one."""
    control_bus = EvidenceBus()
    served_call(control_bus, "c", WEATHER, ServedFrom.NONE)
    experiment_bus = EvidenceBus()
    served_call(experiment_bus, "e", WEATHER, ServedFrom.CACHE)

    report = compare_sides(
        control_bus.events("c"),
        experiment_bus.events("e"),
        control_results=[scored("c", WEATHER, ts=False)],
        experiment_results=[scored("e", WEATHER, ts=True)],
    )

    assert report.delta.ts == 1.0
    assert report.delta.success == 1.0, "control served nothing; protection did"


def test_a_worse_experiment_arm_shows_a_negative_ts_delta() -> None:
    """A comparator that cannot show a regression is not a comparator."""
    report = compare_sides(
        [],
        [],
        control_results=[scored("c", WEATHER, ts=True)],
        experiment_results=[scored("e", WEATHER, ts=False)],
    )

    assert report.delta.ts == -1.0


def test_delta_ts_is_zero_when_either_arm_lacks_a_verdict() -> None:
    """An absent number is not a change of zero."""
    report = compare_sides([], [], experiment_results=[scored("e", WEATHER, ts=True)])

    assert report.delta.ts == 0.0
    assert report.experiment.ts_rate == 1.0
    assert report.control.ts_rate is None


def test_more_fallbacks_under_protection_read_as_a_positive_delta() -> None:
    """Sign convention: on fallback and duplicates, positive = worse."""
    control_bus = EvidenceBus()
    served_call(control_bus, "c", WEATHER, ServedFrom.LIVE)
    experiment_bus = EvidenceBus()
    served_call(experiment_bus, "e", WEATHER, ServedFrom.CACHE)

    report = compare_sides(control_bus.events("c"), experiment_bus.events("e"))

    assert report.delta.fallback == 1.0
    assert report.delta.success == 0.0


def test_protection_removing_a_double_charge_shows_a_negative_delta() -> None:
    control_bus = EvidenceBus()
    duplicated_call(control_bus, "c", WEATHER)
    experiment_bus = EvidenceBus()
    bus_serve = EvidenceBus()
    experiment_bus.record("e", WEATHER, Phase.SEND)
    experiment_bus.record("e", WEATHER, Phase.POST_EFFECT, effect_applied=True)
    experiment_bus.record("e", WEATHER, Phase.RECV, served_from=ServedFrom.CACHE)

    report = compare_sides(control_bus.events("c"), experiment_bus.events("e"))

    assert report.control.duplicates == 1
    assert report.experiment.duplicates == 0
    assert report.delta.duplicates == -1.0


def test_both_arms_identical_give_a_zero_delta() -> None:
    bus_a = EvidenceBus()
    bus_b = EvidenceBus()
    for bus in (bus_a, bus_b):
        served_call(bus, "t", WEATHER, ServedFrom.LIVE)
        served_call(bus, "t", WEATHER, ServedFrom.LIVE)

    report = compare_sides(bus_a.events("t"), bus_b.events("t"))

    assert report.delta.as_dict() == {
        "success": 0.0,
        "fallback": 0.0,
        "duplicates": 0.0,
        "ts": 0.0,
    }


# ---------------------------------------------------------------------------
# Group 4 - the report shape
# ---------------------------------------------------------------------------


def test_compare_sides_returns_both_arms_and_the_delta() -> None:
    control_bus = EvidenceBus()
    experiment_bus = EvidenceBus()
    served_call(control_bus, "c", WEATHER, ServedFrom.NONE)
    served_call(experiment_bus, "e", WEATHER, ServedFrom.LIVE)

    report = compare_sides(control_bus.events("c"), experiment_bus.events("e"))

    assert isinstance(report, CompareResult)
    assert isinstance(report.control, SideMetrics)
    assert isinstance(report.experiment, SideMetrics)
    assert report.control.success == 0
    assert report.experiment.success == 1
    assert report.delta.success == 1.0


def test_report_echoes_the_mode_it_was_given() -> None:
    report = compare_sides([], [], mode=resolve_mode("control"))
    assert report.mode == "control"


def test_report_is_plain_json_serialisable_data() -> None:
    """The dashboard and the API both need this without any conversion."""
    report = compare_sides([], [])

    assert set(report.as_dict()) == {"mode", "control", "experiment", "delta"}
    assert set(report.control.as_dict()) == {
        "calls", "success", "fallback", "duplicates", "runs",
        "success_rate", "fallback_rate", "duplicate_rate", "ts_rate",
    }
    assert set(report.delta.as_dict()) == {
        "success", "fallback", "duplicates", "ts",
    }


def test_the_report_carries_no_four_axis_numbers() -> None:
    """Containment / recovery / detection / stability are `axes.py`'s job."""
    report = compare_sides([], [])

    for banned in ("containment", "recovery", "detection", "stability", "mttr"):
        assert banned not in str(report.as_dict())


# ---------------------------------------------------------------------------
# Group 5 - agreement with P1, and purity
# ---------------------------------------------------------------------------


def test_duplicate_agreement_with_the_scorer_on_the_same_trace() -> None:
    """Comparator and grader must not disagree about a double charge.

    P1's `Mult` and this module's `duplicates` are separate implementations of
    the same rule, so pin them together. If someone changes one and not the
    other, this fails.
    """
    bus = EvidenceBus()
    spec = k_of_n_drill("t-spec", WEATHER, k=1, n=1, idempotent=False)
    duplicated_call(bus, "t", WEATHER)

    verdict = score_run(spec, bus.events("t"))
    metrics = side_metrics(bus.events("t"))

    assert verdict.mult is True
    assert metrics.duplicates == 1


def test_clean_agreement_with_the_scorer_on_a_healthy_trace() -> None:
    """A faultless trace: no duplicate, and TS still False because Miss.

    Worth pinning explicitly. The comparator is happy to report a healthy arm
    (success 1.0) while the grader fails the run, and that is not a
    contradiction - `Miss` is exactly "the fault never fired, so nothing was
    proven". The two answer different questions.
    """
    bus = EvidenceBus()
    served_call(bus, "t", WEATHER, ServedFrom.LIVE)
    spec = k_of_n_drill("t-spec", WEATHER, k=1, n=1)

    verdict = score_run(spec, bus.events("t"))
    metrics = side_metrics(bus.events("t"))

    assert verdict.mult is False
    assert verdict.miss is True
    assert verdict.ts is False
    assert metrics.duplicates == 0
    assert metrics.success == 1


def test_servable_comes_from_the_scorer_not_a_local_copy() -> None:
    """If P1 changes what counts as servable, this module follows for free."""
    from backend.scoring import SERVABLE_SOURCES

    bus = EvidenceBus()
    for source in SERVABLE_SOURCES:
        served_call(bus, "t", WEATHER, source)

    assert side_metrics(bus.events("t")).success == len(SERVABLE_SOURCES)


def _imported_modules(module) -> set[str]:
    """Every module `module` imports, parsed from the AST.

    Prose in a docstring is not an import - the B1 caveat names EvidenceBus in
    a sentence, and checking raw source text would fail on that. Parsing is the
    honest version of "does this module reach for X".
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_comparator_does_not_import_fastapi_or_sqlalchemy() -> None:
    """`main.py` owns the HTTP layer; `axes.py` owns the four axes."""
    from backend import compare

    imported = _imported_modules(compare)
    for banned in ("fastapi", "starlette", "sqlalchemy"):
        assert not any(banned in name for name in imported), (
            f"compare.py imported {banned}"
        )


def test_comparator_does_not_import_the_evidence_bus_or_the_database() -> None:
    """It reads evidence; it never gathers it, and it never persists it.

    This is what keeps `compare.py` importable and testable with no P2 stack
    and no database present.
    """
    from backend import compare

    imported = _imported_modules(compare)
    for banned in ("faults", "store", "db", "models", "proxy"):
        assert not any(name.endswith(banned) for name in imported), (
            f"compare.py imported {banned}"
        )


def test_accepts_a_generator_of_events() -> None:
    """Callers may stream; the module must not require a list."""
    bus = EvidenceBus()
    served_call(bus, "t", WEATHER, ServedFrom.LIVE)

    metrics = side_metrics(e for e in bus.events("t"))

    assert metrics.calls == 1
