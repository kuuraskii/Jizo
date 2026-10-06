"""
JIZO - Part 2 structured logging.

Why not `print()`? On stage you cannot grep a print. Every line here is one
JSON object, so when something goes wrong during the demo you can find the
exact call, see which attempt failed, and prove what happened afterwards.

One line per call, always the same keys:

    {"event": "upstream_call", "trace_id": "abc123", "api_key": "weather",
     "attempt": 1, "status_code": 503, "latency_ms": 3004.2,
     "breaker_state": "CLOSED", "served_from": "none", ...}

That is the minimum needed to answer a mentor's question: "how do you know
the breaker opened in three seconds rather than thirty?" You filter on
`api_key` and `breaker_state`, and read `latency_ms`.

`structlog` renders these. It is optional at import time so that `import jizo`
never fails for a user who only wants the library - the same rule as
`requirements-gui.txt` for pywebview.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any, Optional

#: The keys every upstream-call line carries, so logs are uniform and
#: greppable. Anything else a caller adds is merged on top.
CORE_FIELDS = (
    "event",
    "trace_id",
    "api_key",
    "attempt",
    "status_code",
    "latency_ms",
    "breaker_state",
    "served_from",
)

try:  # pragma: no cover - exercised by whichever branch is installed
    import structlog

    _HAS_STRUCTLOG = True
except ImportError:  # pragma: no cover
    structlog = None  # type: ignore[assignment]
    _HAS_STRUCTLOG = False


def _redact_processor(logger, method_name: str, event_dict: dict):
    """structlog processor: strip credentials from every event dict.

    Needed so both renderers redact identically - otherwise installing
    structlog would silently disable redaction.
    """
    return redact(event_dict)


def configure_logging(
    level: str = "INFO",
    *,
    json_output: bool = True,
) -> None:
    """Set up logging once, at process start.

    Call this from `jizo.serve()` or your own entry point. Safe to call twice.

    With structlog installed you get one JSON object per line, which is what
    Azure Monitor's log exporter expects. Without it we fall back to stdlib
    logging with a JSON formatter, so the shape of every line is the same
    either way - only the renderer differs.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    if _HAS_STRUCTLOG:
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                _redact_processor,
                (
                    structlog.processors.JSONRenderer()
                    if json_output
                    else structlog.dev.ConsoleRenderer()
                ),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
            logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
            cache_logger_on_first_use=True,
        )
    else:
        logging.basicConfig(
            format="%(message)s",
            level=numeric_level,
            stream=sys.stdout,
            force=True,
        )


def get_logger(name: str = "jizo") -> Any:
    """Get a logger.

    Returns a structlog logger when available, otherwise a stdlib one. Both
    accept keyword arguments, so call sites do not need to know which.
    """
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    logger = logging.getLogger(name)
    return _StdlibJsonLogger(logger)


class _StdlibJsonLogger:
    """Thin adapter so stdlib loggers take the same kwargs as structlog."""

    def __init__(self, logger: logging.Logger, **bound: Any) -> None:
        self._logger = logger
        self._bound = bound

    def bind(self, **kwargs: Any) -> "_StdlibJsonLogger":
        """Bind context that every later line carries, like structlog does.

        Returns a NEW adapter carrying the extra fields; the original is left
        alone so bound context cannot leak into unrelated call sites.
        """
        merged = dict(self._bound)
        merged.update(kwargs)
        return _StdlibJsonLogger(self._logger, **merged)

    # `_event` is underscore-prefixed so a caller-supplied `event=` key in
    # kwargs cannot collide with the positional parameter and raise TypeError.
    def info(self, _event: str = "", **kwargs: Any) -> None:
        self._logger.info(_render(_event, {**self._bound, **kwargs}))

    def warning(self, _event: str = "", **kwargs: Any) -> None:
        self._logger.warning(_render(_event, {**self._bound, **kwargs}))

    def error(self, _event: str = "", **kwargs: Any) -> None:
        self._logger.error(_render(_event, {**self._bound, **kwargs}))

    def debug(self, _event: str = "", **kwargs: Any) -> None:
        self._logger.debug(_render(_event, {**self._bound, **kwargs}))


def _render(event: str, fields: dict[str, Any]) -> str:
    """One JSON line, redacted, for the stdlib renderer.

    The structlog path applies redaction through its own processor chain.
    """
    import json

    safe = {k: v for k, v in fields.items() if v is not None and k != "event"}
    payload = {"event": event, **redact(safe)}
    # allow_nan=False: NaN/Infinity are not valid JSON, so a log exporter
    # would reject the whole line rather than the bad field.
    return json.dumps(payload, default=str, separators=(",", ":"), allow_nan=False)


#: Kept as the historical name; the renderer now lives above.
_to_json_line = _render


class CallLogger:
    """Logs one upstream call, so callers do not assemble log fields by hand.

    Use it from the proxy:

        with CallLogger(log, trace_id, "weather") as call:
            call.set_attempt(1)
            result = await client.get(url)
            call.success(200, latency_ms=812.0)

    Latency is measured automatically, so it is never forgotten or
    hand-calculated wrong. A failure records the exception type rather than
    the whole traceback, because the traceback belongs in a separate field.
    """

    def __init__(
        self,
        logger: Any,
        trace_id: str,
        api_key: str,
        *,
        attempt: int = 1,
    ) -> None:
        self._logger = logger
        self._fields: dict[str, Any] = {
            "trace_id": trace_id,
            "api_key": api_key,
            "attempt": attempt,
        }
        self._start: Optional[float] = None

    def __enter__(self) -> "CallLogger":
        from .breaker import _now

        self._start = _now()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Never swallow the exception; just make sure a failed call is logged
        # even if the caller forgot to call .failure().
        if exc_type is not None and "outcome" not in self._fields:
            self.failure(exc_type.__name__)
        return False

    def set_attempt(self, attempt: int) -> None:
        """Which try this is (1-based)."""
        self._fields["attempt"] = attempt

    def set_breaker_state(self, state: Any) -> None:
        """Attach the breaker state so logs show the guard in action."""
        self._fields["breaker_state"] = getattr(state, "value", str(state))

    def set_served_from(self, served_from: Any) -> None:
        """Where the caller actually got their answer from."""
        self._fields["served_from"] = getattr(served_from, "value", str(served_from))

    def extra(self, **kwargs: Any) -> None:
        """Attach anything else worth recording.

        Redacted immediately, not at render time. A caller may hand this
        logger to anything, and a raw logger that receives a credential is a
        leak we cannot take back.
        """
        self._fields.update(redact(kwargs))

    def _latency_ms(self) -> Optional[float]:
        if self._start is None:
            return None
        from .breaker import _now

        return round((_now() - self._start) * 1000.0, 2)

    def success(self, status_code: int, **kwargs: Any) -> None:
        """Log a call that worked."""
        self._fields.update(kwargs)
        self._fields["status_code"] = status_code
        self._fields["latency_ms"] = self._latency_ms()
        self._fields["outcome"] = "success"
        self._logger.info("upstream_call", **self._redacted_fields())

    def _redacted_fields(self) -> dict:
        """Fields with credentials stripped, for any logger we were handed.

        `event` is dropped here too. structlog's `meth(event, ...)` binds it
        positionally, so a caller field named `event` would raise TypeError on
        that path even though the stdlib renderer tolerates it.
        """
        return {k: v for k, v in redact(dict(self._fields)).items() if k != "event"}

    def failure(
        self, error_type: str, status_code: Optional[int] = None, **kwargs: Any
    ) -> None:
        """Log a call that failed. `error_type` is the exception class name."""
        self._fields.update(kwargs)
        self._fields["error_type"] = error_type
        if status_code is not None:
            self._fields["status_code"] = status_code
        self._fields["latency_ms"] = self._latency_ms()
        self._fields["outcome"] = "failure"
        self._logger.warning("upstream_call", **self._redacted_fields())


#: Field names whose VALUE is a credential. Matched on word boundaries.
#:
#: `api_key` is deliberately absent from this list. In JIZO it names the
#: dependency ("weather", "geocode"), and it is part of the fixed line shape -
#: redacting it would blank every useful log line. A credential in a URL is
#: still caught by SECRET_QUERY_KEYS below, which is how upstream keys
#: actually appear. Callers logging a real credential should use `token`,
#: `secret` or `authorization`.
SECRET_MARKERS = (
    "secret", "token", "password", "passwd", "authorization", "auth",
    "credential", "bearer", "access_key", "session", "apikey", "api-key",
)

#: Query-string keys whose values are never safe to log.
SECRET_QUERY_KEYS = ("api_key", "apikey", "key", "token", "access_token", "sig")

REDACTED = "***redacted***"

#: An Authorization header value anywhere in free text.
_AUTH_HEADER = re.compile(r"\b(bearer|basic)\s+\S+", re.IGNORECASE)


def _looks_secret(key: str) -> bool:
    """Does this KEY name a credential?

    Matched on word boundaries, not substrings. A plain `in` test was a
    mistake: it treats the ordinary api_key "weather" as a secret because
    "auth" appears inside it, which redacted every useful log line.
    """
    parts = [p for p in re.split(r"[^a-z0-9]+", key.lower()) if p]
    if any(part in SECRET_MARKERS for part in parts):
        return True

    # Compound names ending in a credential word. `X-Api-Key`,
    # `subscription_key` and `openai_api_key` are all real header names.
    #
    # EXCEPT the bare `api_key` / `apikey`, which in this codebase names the
    # dependency ("weather"), not a credential. Redacting it would blank the
    # api_key field on every log line. A genuine upstream key in a URL is
    # still caught by SECRET_QUERY_KEYS.
    if parts == ["api", "key"] or parts == ["apikey"]:
        return False
    return len(parts) > 1 and parts[-1] in (
        "key", "token", "secret", "password", "passwd", "apikey"
    )


#: How deep to walk nested containers before giving up. A logging helper must
#: never be able to kill the request it is trying to describe.
MAX_REDACT_DEPTH = 6


def redact(
    value: Any,
    _key: str = "",
    _depth: int = 0,
    _seen: frozenset[int] = frozenset(),
) -> Any:
    """Strip credentials out of anything on its way into a log line.

    Logging a secret is a real incident: log sinks are shipped to Azure
    Monitor, so anything written is retained and searchable. Three leaks had
    to be closed:

    * `extra(url=...)` with an `api_key` in the query string
    * `extra(headers={"Authorization": "Bearer ..."})`
    * a credential appearing inside an exception message

    The key is checked first (so `token=...` is caught even when the value
    looks harmless), then the value is scanned for the markers and for
    credential-shaped query parameters.
    """
    if _depth >= MAX_REDACT_DEPTH:
        return f"<truncated at depth {MAX_REDACT_DEPTH}>"

    # Cycle guard: a self-referential dict would otherwise recurse until the
    # stack blew, and RecursionError escaping into the request path is a far
    # worse outcome than a logged placeholder.
    if isinstance(value, (dict, list, tuple, set)):
        if id(value) in _seen:
            return "<circular>"
        _seen = _seen | {id(value)}

    if isinstance(value, dict):
        return {k: redact(v, str(k), _depth + 1, _seen) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, _key, _depth + 1, _seen) for v in value]

    if _key and _looks_secret(_key):
        return REDACTED

    # Leave numbers, booleans and None alone so the JSON keeps their types -
    # `latency_ms: 812.4` must not become `"812.4"`, and P5 charts these.
    if isinstance(value, (int, float, bool)) or value is None:
        return value

    text = str(value)
    for qkey in SECRET_QUERY_KEYS:
        text = _redact_query_param(text, qkey)
    if _AUTH_HEADER.search(text):
        return REDACTED
    return text


def _redact_query_param(text: str, param: str) -> str:
    """Blank out one query parameter's value, leaving the rest readable."""
    import re

    pattern = re.compile(
        rf"([?&]{re.escape(param)}=)([^&\s\"']+)", re.IGNORECASE
    )
    return pattern.sub(rf"\1{REDACTED}", text)


def log_breaker_transition(breaker: Any, logger: Optional[Any] = None) -> None:
    """Log each NEW breaker state change since the last call.

    Breaker transitions are the numbers a judge asks about ("you claim it
    opened in three seconds - show me"), so they get their own event rather
    than being buried in a call line.

    Reads `transitions()`, not `snapshot()`, so it cannot advance the state
    machine, and it only emits rows it has not emitted before - calling it
    repeatedly on a settled breaker logs nothing.
    """
    log = logger or get_logger("jizo.breaker")
    transitions = breaker.transitions()

    already = getattr(breaker, "_logged_transition_count", 0)
    for frm, to, at, rate in transitions[already:]:
        log.info(
            "breaker_transition",
            api_key=breaker.policy.api_key,
            from_state=frm.value,
            to_state=to.value,
            # Recorded at the transition, not now. A judge asking "what was
            # the error rate when it tripped?" must not be answered with
            # whatever the window happens to hold today.
            error_pct=round(rate, 2),
            at=round(at, 3),
        )
    # Remember the watermark on the breaker so repeated calls are quiet.
    try:
        breaker._logged_transition_count = len(transitions)
    except AttributeError:  # pragma: no cover - defensive
        pass