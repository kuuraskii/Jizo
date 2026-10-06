"""
Tests for JIZO structured logging (Part 2 - Pushkar's file).

On stage you cannot grep a print, so every line must be one JSON object with
the same keys. These tests prove:

  * every upstream-call line carries the core fields, so logs are greppable
  * latency is measured automatically and never has to be hand-calculated
  * a failure is logged even when the caller forgets to log it
  * the library works with OR without structlog installed, with the same
    output shape either way

The last point matters: structlog is an optional dependency, so `import jizo`
must not fail for someone who only wants the library.
"""

from __future__ import annotations

import json
import logging

import pytest

from backend import CORE_FIELDS, CallLogger, configure_logging, get_logger
from backend.logging_conf import _to_json_line


class CapturingLogger:
    """Collects log calls so a test can inspect what would have been printed."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def _record(self, level: str, event: str, fields: dict) -> None:
        self.records.append((level, event, fields))

    def info(self, event: str = "", **kwargs) -> None:
        self._record("info", event, kwargs)

    def warning(self, event: str = "", **kwargs) -> None:
        self._record("warning", event, kwargs)

    def error(self, event: str = "", **kwargs) -> None:
        self._record("error", event, kwargs)

    def debug(self, event: str = "", **kwargs) -> None:
        self._record("debug", event, kwargs)

    def bind(self, **kwargs) -> "CapturingLogger":
        return self


# ---------------------------------------------------------------------------
# The uniform line shape
# ---------------------------------------------------------------------------


def test_core_fields_are_the_documented_set() -> None:
    """These keys are what P6 greps and what P5 charts, so they are fixed."""
    assert CORE_FIELDS == (
        "event",
        "trace_id",
        "api_key",
        "attempt",
        "status_code",
        "latency_ms",
        "breaker_state",
        "served_from",
    )


def test_success_line_carries_every_core_field() -> None:
    log = CapturingLogger()
    from backend import BreakerState, ServedFrom

    with CallLogger(log, "trace-abc", "weather") as call:
        call.set_attempt(2)
        call.set_breaker_state(BreakerState.HALF_OPEN)
        call.set_served_from(ServedFrom.CACHE)
        call.success(200)

    level, event, fields = log.records[0]
    assert level == "info"
    # structlog takes the event name positionally; the rest ride along as
    # keyword fields. Every core key must be represented in the rendered line.
    assert event == "upstream_call"
    rendered = {"event": event, **fields}
    for key in CORE_FIELDS:
        assert key in rendered, f"missing {key}"
    assert fields["trace_id"] == "trace-abc"
    assert fields["api_key"] == "weather"
    assert fields["attempt"] == 2
    assert fields["status_code"] == 200
    assert fields["breaker_state"] == "HALF_OPEN"
    assert fields["served_from"] == "cache"


def test_latency_is_measured_not_passed_in() -> None:
    """Nobody should have to calculate latency by hand."""
    log = CapturingLogger()

    with CallLogger(log, "t", "weather") as call:
        call.success(200)

    assert isinstance(log.records[0][2]["latency_ms"], float)
    assert log.records[0][2]["latency_ms"] >= 0.0


def test_failure_line_logs_at_warning() -> None:
    """Failures must stand out in a log filter."""
    log = CapturingLogger()

    with CallLogger(log, "t", "weather") as call:
        call.failure("ConnectTimeout", status_code=None)

    level, event, fields = log.records[0]
    assert level == "warning"
    assert fields["error_type"] == "ConnectTimeout"
    assert "status_code" not in fields, "None status should be omitted"


def test_failure_includes_status_when_known() -> None:
    log = CapturingLogger()

    with CallLogger(log, "t", "weather") as call:
        call.failure("HTTPStatusError", status_code=503)

    assert log.records[0][2]["status_code"] == 503


def test_exception_inside_the_block_is_logged_automatically() -> None:
    """A caller must not be able to forget logging a failed call."""
    log = CapturingLogger()

    with pytest.raises(RuntimeError):
        with CallLogger(log, "t", "weather") as call:
            raise RuntimeError("boom")

    assert log.records, "the failure was swallowed silently"
    assert log.records[0][2]["error_type"] == "RuntimeError"


def test_context_manager_does_not_swallow_the_exception() -> None:
    """Logging must never mask the original error."""
    log = CapturingLogger()

    with pytest.raises(ValueError):
        with CallLogger(log, "t", "weather"):
            raise ValueError("original")

    assert log.records[0][2]["error_type"] == "ValueError"


def test_an_explicit_failure_is_not_logged_twice() -> None:
    log = CapturingLogger()

    with CallLogger(log, "t", "weather") as call:
        call.failure("Timeout")

    assert len(log.records) == 1


def test_extra_fields_are_attached() -> None:
    """Callers add their own context without changing the core shape."""
    log = CapturingLogger()

    with CallLogger(log, "t", "weather") as call:
        call.extra(city="Delhi", fixture="demo")
        call.success(200)

    fields = log.records[0][2]
    assert fields["city"] == "Delhi"
    assert fields["fixture"] == "demo"
    assert "status_code" in fields


def test_credentials_are_redacted() -> None:
    """Nothing that looks like a credential may reach a log sink.

    Log sinks are retained and searchable, so a leaked key is a real incident.
    This group did not exist before review - the old coverage map claimed a
    test proved it, and no test did.
    """
    from backend.logging_conf import redact

    # A credential in a URL's query string.
    url = redact("https://x.test/v1/now?api_key=sk-LIVE-abc&units=metric")
    assert "sk-LIVE-abc" not in url
    assert "units=metric" in url, "the rest of the URL must stay readable"

    # A credential named by its field.
    assert redact("tok_9f", "token") == "***redacted***"
    assert redact("hunter2", "secret") == "***redacted***"
    assert redact("p4ss", "password") == "***redacted***"

    # An Authorization header value.
    assert redact({"Authorization": "Bearer tok_9f"})["Authorization"] == "***redacted***"

    # A credential inside an exception message.
    assert "k9" not in redact("failed calling https://x?api_key=k9")

    # And through a real log line.
    log = CapturingLogger()
    with CallLogger(log, "t", "weather") as call:
        call.extra(url="https://x.test/v1?api_key=sk-LIVE-abc", token="tok_9f")
        call.success(200)
    import backend.logging_conf as lc
    line = lc._render("upstream_call", log.records[0][2])
    assert "sk-LIVE-abc" not in line
    assert "tok_9f" not in line


def test_ordinary_values_are_not_redacted() -> None:
    """Redaction that blanks everything is useless; only credentials go."""
    from backend.logging_conf import redact

    assert redact("Delhi") == "Delhi"
    assert redact("https://x.test/v1/now") == "https://x.test/v1/now"
    # `api_key` in this codebase names the dependency, not a credential, so
    # redacting it would blank every useful log line.
    assert redact("weather", "api_key") == "weather"
    assert redact(812.4, "latency_ms") == 812.4, "numbers keep their type"


def test_redaction_survives_a_self_referential_structure() -> None:
    """A logging helper must never be able to kill the request it describes.

    A dict that points back at itself used to recurse until the stack blew,
    and `RecursionError` escapes into the caller's `except` as a *second*
    exception - the real failure gets masked by our own logging.
    """
    from backend.logging_conf import redact

    loop: dict = {"city": "Delhi"}
    loop["self"] = loop
    loop["list"] = [loop, {"nested": loop}]

    out = redact(loop)  # must not raise

    assert "<circular>" in json.dumps(out, default=str)
    assert out["city"] == "Delhi", "the useful part still survives"


def test_redaction_stops_at_a_depth_limit() -> None:
    """A deeply nested payload is truncated, not recursed forever."""
    from backend.logging_conf import MAX_REDACT_DEPTH, redact

    payload: dict = {"leaf": "value"}
    for _ in range(40):
        payload = {"deeper": payload}

    out = redact(payload)
    blob = json.dumps(out, default=str)

    assert f"<truncated at depth {MAX_REDACT_DEPTH}>" in blob


def test_compound_credential_headers_are_redacted() -> None:
    """Real header names end in a credential word whatever precedes it.

    The first version only matched an exact `secret_key`-style pair, so
    `X-Api-Key` and `subscription_key` sailed straight through.
    """
    from backend.logging_conf import redact

    assert redact("abc123", "X-Api-Key") == "***redacted***"
    assert redact("abc123", "subscription_key") == "***redacted***"
    assert redact("abc123", "openai_api_key") == "***redacted***"

    # ...but the bare `api_key` names the dependency in this codebase, and
    # blanking it would empty the most useful column on every log line.
    assert redact("weather", "api_key") == "weather"


def test_structlog_path_redacts_too() -> None:
    """Installing structlog must not silently disable redaction."""
    from backend.logging_conf import _redact_processor

    event_dict = {"api_key": "weather", "token": "tok_9f", "url": "https://x?api_key=k9"}
    out = _redact_processor(None, "info", dict(event_dict))

    assert out["token"] == "***redacted***"
    assert "k9" not in out["url"]
    assert out["api_key"] == "weather"


def test_attempt_defaults_to_one() -> None:
    log = CapturingLogger()

    with CallLogger(log, "t", "weather") as call:
        call.success(200)

    assert log.records[0][2]["attempt"] == 1


# ---------------------------------------------------------------------------
# structlog optional
# ---------------------------------------------------------------------------


def test_get_logger_works_without_structlog(monkeypatch: pytest.MonkeyPatch) -> None:
    """The library must import and log without the optional dependency."""
    import backend.logging_conf as lc

    monkeypatch.setattr(lc, "_HAS_STRUCTLOG", False)
    logger = lc.get_logger("jizo.test")

    # The stdlib path must accept the same kwargs structlog does.
    logger.info("upstream_call", trace_id="t", api_key="weather")


def test_configure_logging_is_safe_to_call_twice() -> None:
    """Process start-up may call it more than once; it must not raise."""
    configure_logging("INFO")
    configure_logging("INFO")


def test_configure_logging_accepts_console_output() -> None:
    configure_logging("DEBUG", json_output=False)


def test_json_line_is_valid_json_with_no_none_values() -> None:
    """Azure Monitor's exporter parses these, so they must be valid JSON."""
    line = _to_json_line(
        "upstream_call",
        {"trace_id": "t", "api_key": "weather", "status_code": None},
    )
    payload = json.loads(line)

    assert payload["event"] == "upstream_call"
    assert payload["trace_id"] == "t"
    assert "status_code" not in payload, "None fields should be dropped"


def test_json_line_tolerates_unserialisable_values() -> None:
    """An exception object in a field must not break logging."""
    line = _to_json_line("upstream_call", {"error": ValueError("x")})

    assert isinstance(json.loads(line)["error"], str)


def test_breaker_transition_is_its_own_event() -> None:
    """Transitions are the numbers a judge asks about, so they get a line."""
    import backend.logging_conf as lc
    from backend import ApiPolicy, CircuitBreaker

    log = CapturingLogger()
    breaker = CircuitBreaker(ApiPolicy(api_key="weather", base_url="https://x.test"))
    for _ in range(20):
        breaker.record_failure()

    lc.log_breaker_transition(breaker, logger=log)
    level, event, fields = log.records[0]

    assert event == "breaker_transition"
    assert fields["api_key"] == "weather"
    assert fields["from_state"] == "CLOSED"
    assert fields["to_state"] == "OPEN"
    assert fields["error_pct"] > 0


def test_logging_a_transition_does_not_advance_the_breaker() -> None:
    """Reading the breaker to log it must never drive it into probing.

    Regression: the helper called snapshot(), which read the lazy `state`
    property and performed the OPEN -> HALF_OPEN transition. That made a
    dashboard polling `/breaker/state` a driver of the state machine.
    """
    import backend.logging_conf as lc
    from backend import ApiPolicy, BreakerState, CircuitBreaker

    class Clock:
        def __init__(self) -> None:
            self.t = 0.0

        def __call__(self) -> float:
            return self.t

        def advance(self, s: float) -> None:
            self.t += s

    clock = Clock()
    log = CapturingLogger()
    breaker = CircuitBreaker(
        ApiPolicy(api_key="weather", base_url="https://x.test"), clock=clock
    )
    for _ in range(20):
        breaker.record_failure()
    clock.advance(10.1)

    lc.log_breaker_transition(breaker, logger=log)

    assert breaker.state is BreakerState.OPEN, "logging mutated the state machine"


def test_repeated_transition_logging_is_quiet() -> None:
    """Polling the log helper on a settled breaker must not spam lines."""
    import backend.logging_conf as lc
    from backend import ApiPolicy, CircuitBreaker

    log = CapturingLogger()
    breaker = CircuitBreaker(ApiPolicy(api_key="weather", base_url="https://x.test"))

    for _ in range(5):
        lc.log_breaker_transition(breaker, logger=log)

    assert log.records == [], "a never-transitioned breaker must log nothing"

    for _ in range(20):
        breaker.record_failure()
    lc.log_breaker_transition(breaker, logger=log)
    first = len(log.records)
    lc.log_breaker_transition(breaker, logger=log)

    assert first == 1
    assert len(log.records) == 1, "the same transition must not be logged twice"


# ---------------------------------------------------------------------------
# The stdlib fallback adapter
# ---------------------------------------------------------------------------


def test_stdlib_adapter_renders_json_lines(caplog: pytest.LogCaptureFixture) -> None:
    """Without structlog the output shape must still be one JSON object."""
    import backend.logging_conf as lc

    monkey_logger = lc._StdlibJsonLogger(logging.getLogger("jizo.fallback"))
    with caplog.at_level(logging.INFO):
        monkey_logger.info("upstream_call", trace_id="t", api_key="weather")

    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["event"] == "upstream_call"
    # `api_key` here is the dependency's internal name, not a credential, so
    # redaction must leave it readable.
    assert payload["api_key"] == "weather"


def test_call_logger_failure_reaches_a_real_stdlib_logger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The outage line is the whole point of this file - test it for real.

    Every other test here passed a fake logger, so `_StdlibJsonLogger` was
    only ever exercised through `.info()`. `.warning()` - the exact method
    `CallLogger.failure()` calls, i.e. the row that proves the dependency went
    down - had zero coverage. A typo in that method would have shipped.
    """
    import backend.logging_conf as lc

    adapter = lc._StdlibJsonLogger(logging.getLogger("jizo.real"))
    with caplog.at_level(logging.WARNING):
        with CallLogger(adapter, "t-42", "weather") as call:
            call.failure("ConnectTimeout", status_code=503)

    record = caplog.records[-1]
    payload = json.loads(record.getMessage())

    assert record.levelname == "WARNING", "a failure must not log at INFO"
    assert payload["outcome"] == "failure"
    assert payload["trace_id"] == "t-42"
    assert payload["api_key"] == "weather"
    assert payload["status_code"] == 503
    assert payload["error_type"] == "ConnectTimeout"
    assert "latency_ms" in payload, "latency is measured even on failure"


def test_every_stdlib_adapter_level_emits_valid_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """All four levels, because P3 exports whatever level we emit at."""
    import backend.logging_conf as lc

    adapter = lc._StdlibJsonLogger(logging.getLogger("jizo.levels"))
    with caplog.at_level(logging.DEBUG):
        adapter.debug("d", n=1)
        adapter.info("i", n=2)
        adapter.warning("w", n=3)
        adapter.error("e", n=4)

    got = [json.loads(r.getMessage()) for r in caplog.records[-4:]]
    assert [g["n"] for g in got] == [1, 2, 3, 4]
    # Severity rides on the stdlib record, which is what P3's log shipping
    # filters on. It is deliberately not repeated inside the JSON payload.
    assert [r.levelname for r in caplog.records[-4:]] == [
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
    ]
    assert [g["event"] for g in got] == ["d", "i", "w", "e"]


def test_bearer_token_in_free_text_is_redacted() -> None:
    """An exception message often carries the whole request line.

    `raise_for_status()` and most client libraries put the URL in the error
    text, so a credential arrives without ever being named as a field.
    """
    from backend.logging_conf import redact

    out = redact("HTTP 401 for Authorization: Bearer abc123xyz")
    assert "abc123xyz" not in out
    assert out == "***redacted***"


def test_get_logger_returns_a_usable_logger_on_both_paths() -> None:
    """`get_logger()` is how every teammate gets a logger.

    Two implementations exist behind it - structlog's, and our stdlib adapter.
    Whichever one is installed, the returned object must accept the same calls,
    or a teammate's code breaks depending on whether they ran `pip install`.
    """
    import backend.logging_conf as lc

    log = get_logger("jizo.probe")
    for level in ("debug", "info", "warning", "error"):
        getattr(log, level)("smoke", ok=True)

    if lc._HAS_STRUCTLOG:
        # structlog's own logger, not our adapter.
        assert not isinstance(log, lc._StdlibJsonLogger)
    else:
        assert isinstance(log, lc._StdlibJsonLogger)


def test_configure_logging_runs_on_both_paths() -> None:
    """Installing structlog must not break configuration, and vice versa."""
    configure_logging("DEBUG")
    configure_logging("not-a-real-level")  # must not raise

    log = get_logger("jizo.both")
    log.info("still_works", n=1)


def test_latency_is_absent_until_the_call_starts() -> None:
    """`latency_ms` must never be a bogus 0.0.

    A `0.0` here would look like an infinitely fast upstream call on the
    dashboard, which is exactly the number a resilience demo is judged on.
    Absent is honest; zero is a lie.
    """
    import backend.logging_conf as lc
    from backend.breaker import _now

    call = CallLogger.__new__(CallLogger)
    call._start = None
    assert call._latency_ms() is None

    # And a started call reports a real, non-negative number.
    call._start = _now() - 0.25
    measured = call._latency_ms()
    assert measured is not None and measured >= 250.0, (
        f"expected >=250ms, got {measured}"
    )


def test_stdlib_adapter_bind_carries_context(caplog: pytest.LogCaptureFixture) -> None:
    """bind() must attach fields, not silently drop them.

    Regression: it returned self and discarded every kwarg, so context bound
    by a caller vanished - and the two renderers diverged, because
    structlog's bind really does attach.
    """
    import backend.logging_conf as lc

    adapter = lc._StdlibJsonLogger(logging.getLogger("jizo.fallback"))
    bound = adapter.bind(trace_id="t-123", run_id="demo")
    with caplog.at_level(logging.INFO):
        bound.info("upstream_call", api_key="weather")

    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["trace_id"] == "t-123"
    assert payload["run_id"] == "demo"


def test_caller_supplied_event_does_not_crash_or_rename(caplog: pytest.LogCaptureFixture) -> None:
    """A caller field named `event` collided with the positional parameter.

    It raised TypeError on the stdlib path and silently renamed the line on
    the structlog path. The call site's event name must always win.
    """
    import backend.logging_conf as lc

    adapter = lc._StdlibJsonLogger(logging.getLogger("jizo.fallback"))
    with caplog.at_level(logging.INFO):
        adapter.info("upstream_call", event="renamed", api_key="weather")

    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["event"] == "upstream_call"


def test_nan_is_not_emitted_as_json(caplog: pytest.LogCaptureFixture) -> None:
    """NaN is invalid JSON, so a log exporter would drop the whole line."""
    import backend.logging_conf as lc

    adapter = lc._StdlibJsonLogger(logging.getLogger("jizo.fallback"))
    with caplog.at_level(logging.INFO):
        with pytest.raises(ValueError):
            adapter.info("upstream_call", latency_ms=float("nan"))