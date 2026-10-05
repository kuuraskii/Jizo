"""
JIZO - Part 1 shared contracts.

This file is the "language" that every other part of JIZO speaks.
Think of it as the agreement everyone signs before writing code:
Aditi's proxy, Aayush's database tables, Riya's routes and Nikunj's
dashboard all describe their data using the exact shapes defined here.

Why it matters: if Pushkar changes a field name in this file, code in
5 other folders breaks at once. So treat these shapes as frozen.

Nothing in this file talks to the network. It only describes data.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Small vocabularies (enums)
# ---------------------------------------------------------------------------


class Phase(str, Enum):
    """The stage a call had reached when something was recorded.

    A normal successful call looks like: SEND -> POST_EFFECT -> RECV.
    Knowing *which* stage we were in is what lets JIZO tell a harmless
    failure apart from a dangerous one.
    """

    SEND = "send"  # we sent the request, upstream has it
    PRE_EFFECT = "pre_effect"  # upstream has NOT changed anything yet
    POST_EFFECT = "post_effect"  # upstream HAS already changed something
    RECV = "recv"  # we got (or failed to get) the response


class FaultType(str, Enum):
    """The kind of failure we deliberately cause during a drill."""

    DROP_RESPONSE = "drop_response"  # work gets done, answer never arrives
    DELAY = "delay"  # answer arrives far too late
    HTTP_500 = "http_500"  # upstream reports a server error
    HTTP_503 = "http_503"  # upstream says "temporarily unavailable"
    TIMEOUT = "timeout"  # we gave up waiting
    RIVAL_RESPONSE = "rival_response"  # a second, competing answer wins the race


class Pattern(str, Enum):
    """The three failure *timings* JIZO can reproduce and grade."""

    POST_EFFECT = "post_effect"  # answer lost AFTER the work was committed
    ORDER_SENSITIVE = "order_sensitive"  # a stale/rival answer arrives first
    K_OF_N = "k_of_n"  # only one call out of several breaks


class ServedFrom(str, Enum):
    """Where the answer the customer saw actually came from.

    This is the honest label we show on stage: did we serve real data,
    a cached copy, a built-in default, or just a clear message?
    """

    LIVE = "live"  # fresh from the upstream API
    CACHE = "cache"  # slightly stale saved copy
    DEFAULT = "default"  # built-in safe value
    MESSAGE = "message"  # no data, but a human-readable explanation
    NONE = "none"  # nothing was served at all (a failure)


class BreakerState(str, Enum):
    """Circuit breaker states.

    CLOSED   = upstream healthy, calls flow normally.
    OPEN     = upstream considered broken, stop calling it, serve fallback.
    HALF_OPEN= cautiously testing whether the upstream recovered.
    """

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


# ---------------------------------------------------------------------------
# Policy: how we plan to talk to one upstream API
# ---------------------------------------------------------------------------


class ApiPolicy(BaseModel):
    """The safety rules for one upstream dependency.

    Every number here comes from published tuning (see build doc Sec. 4)
    rather than being guessed, so the demo is defensible in Q&A.
    """

    model_config = ConfigDict(frozen=True)  # policies are read-only once loaded

    api_key: str = Field(..., description="Short internal name, e.g. 'weather'")
    base_url: str = Field(..., description="Root URL of the upstream service")
    timeout_s: float = Field(3.0, gt=0, description="Max seconds per single attempt")
    max_attempts: int = Field(3, ge=1, description="Total tries allowed (1 = no retry)")

    # Backoff maths: wait longer between tries, with a random nudge so that
    # many callers do not all retry at the same instant.
    backoff_initial_s: float = Field(0.075, gt=0)
    backoff_max_s: float = Field(1.8, gt=0)
    jitter_s: float = Field(0.05, ge=0)

    # Circuit breaker settings (5 sourced parameters).
    breaker_window: int = Field(100, gt=0, description="How many recent calls we judge by")
    breaker_error_threshold: float = Field(
        0.25, gt=0, le=1, description="Error ratio that trips the breaker"
    )
    breaker_min_volume: int = Field(
        20, gt=0, description="Ignore tripping until we have this many samples"
    )
    breaker_sleep_s: float = Field(10.0, gt=0, description="How long OPEN rests before probing")
    half_open_probes: int = Field(10, gt=0, description="Probes allowed per probe window")
    half_open_window_s: float = Field(5.0, gt=0)

    # Whether retrying this call is safe. GET is safe; a payment POST is not.
    idempotent: bool = Field(True, description="True for safe reads, False for writes")
    criticality: str = Field("medium", description="low | medium | high")
    bulkhead_max_concurrency: int = Field(
        20, gt=0, description="Cap on simultaneous calls to this dependency"
    )
    courtesy_rps: Optional[float] = Field(
        None, gt=0, description="Polite request-rate cap for public/shared APIs"
    )

    def backoff_delay_s(self, attempt: int) -> float:
        """Wait time before retry number `attempt` (0-based).

        Formula used across JIZO:
            min(initial * 2**attempt + random(0, jitter), max)

        The cap matters: without it, attempt 10 would wait ~100 seconds
        and the customer would give up long before we retry.
        """
        import random

        exponential = self.backoff_initial_s * (2**attempt)
        jitter = random.uniform(0, self.jitter_s)
        return min(exponential + jitter, self.backoff_max_s)


# ---------------------------------------------------------------------------
# Fault-injection contracts
# ---------------------------------------------------------------------------


class FaultTarget(BaseModel):
    """Exactly which call the fault should hit.

    `occurrence` is the "k" in "k-of-n": only the k-th identical call breaks.
    That precision is what separates JIZO from a blunt "break the endpoint".
    """

    api_key: str
    phase: Phase = Field(..., description="Stage at which the fault strikes")
    occurrence: int = Field(
        1, ge=1, description="Only fire on this occurrence (k-th call), 1-based"
    )

    @field_validator("api_key")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("api_key cannot be blank")
        return value


class GuardAfter(BaseModel):
    """A safety condition: "only inject *after* this evidence exists".

    This is the core idea borrowed from temporal fault-injection research
    (Tan et al. 2026). A plain "break the API" cannot express *when*.
    A guard can: it waits for proof that the risky moment has arrived.
    """

    api_key: str
    phase: Phase = Field(..., description="Evidence that must already exist")
    min_count: int = Field(
        1, ge=1, description="How many times that evidence must be present"
    )

    def describe(self) -> str:
        """Plain-English summary, handy for logs and the dashboard."""
        plural = "" if self.min_count == 1 else "s"
        return f"after >= {self.min_count} '{self.phase.value}' event(s) on '{self.api_key}'"


class EvidenceEvent(BaseModel):
    """One observation, recorded once, never edited.

    This is "event sourcing" in practice: instead of guessing what the
    upstream did, we write down every step as it happens and later replay
    those steps to decide whether the system behaved correctly.
    """

    trace_id: str = Field(..., description="Groups every event of one user request")
    api_key: str
    phase: Phase
    served_from: ServedFrom = ServedFrom.NONE
    status_code: Optional[int] = None
    effect_applied: bool = Field(
        False,
        description="True once the upstream has permanently changed something",
    )
    fault: Optional[FaultType] = Field(None, description="Fault that struck at this step")
    occurrence: int = Field(
        ...,
        ge=1,
        description="Which time this (api, phase) pair was seen (1st, 2nd, 3rd...)",
    )
    call_index: int = Field(
        1,
        ge=1,
        description=(
            "Which CALL this event belongs to (1st call, 2nd call...). "
            "Different from 'occurrence': one call can log several rows "
            "(a failed attempt plus a fallback), so counting rows would "
            "make 'the 3rd call' drift. Judges use this field."
        ),
    )
    latency_ms: Optional[float] = Field(None, ge=0)
    breaker_state: Optional[BreakerState] = None
    leaked_raw_error: bool = Field(
        False,
        description=(
            "True when the upstream's raw error body was passed to the caller "
            "instead of a fallback. Set by P2's proxy - the scorer must not "
            "have to guess this from a status code."
        ),
    )
    note: Optional[str] = None


class DrillSpec(BaseModel):
    """A complete, reproducible drill definition.

    Pattern helpers in faults.py build these, so nobody hand-writes the
    guard/target pairing and gets it subtly wrong.
    """

    run_id: str
    pattern: Pattern
    fault: FaultType
    target: FaultTarget
    guard: GuardAfter
    total_occurrences: int = Field(
        4, ge=1, description="How many repeated calls the drill watches (n in k-of-n)"
    )
    idempotent: bool = Field(
        True, description="False when a duplicate action would cause real harm"
    )

    def summary(self) -> str:
        """One-line human description used in logs and on the dashboard."""
        return (
            f"{self.pattern.value}: {self.fault.value} on occurrence "
            f"{self.target.occurrence}/{self.total_occurrences} of '{self.target.api_key}' "
            f"{self.guard.describe()}"
        )


# ---------------------------------------------------------------------------
# Scoring contracts
# ---------------------------------------------------------------------------


class DrillOutcome(BaseModel):
    """Raw findings for one drill, before we turn them into a score.

    Each flag answers a different question, so the dashboard can explain
    *why* a run failed instead of just showing a red number.
    """

    correct_withstand: bool = Field(
        ..., description="CW - did the app still answer the faulted call?"
    )
    policy_success: bool = Field(
        ..., description="PS - were the safety rules respected (no duplicates, no bad retries)?"
    )
    premature: bool = Field(..., description="Prem - did the fault fire too early?")
    missed: bool = Field(..., description="Miss - did the fault fail to fire when it should?")
    duplicate: bool = Field(..., description="Mult - did one call do its work twice?")
    notes: list[str] = Field(default_factory=list)

    @property
    def ts(self) -> bool:
        """Temporal Success - the single source of truth for the verdict.

        Kept as a property so no caller can re-implement the conjunction
        and drift from it.
        """
        return (
            self.correct_withstand
            and self.policy_success
            and not self.premature
            and not self.missed
            and not self.duplicate
        )


class ScoreResult(BaseModel):
    """The graded verdict for one drill.

    Temporal Success (TS) is the headline number and is deliberately
    strict: every condition must hold. A single duplicate action or a
    fault that fired early makes the run a failure.
    """

    run_id: str
    pattern: Pattern
    ts: bool = Field(..., description="CW AND PS AND NOT Prem AND NOT Miss AND NOT Mult")
    cw: bool
    ps: bool
    prem: bool
    miss: bool
    mult: bool
    timeline: list[EvidenceEvent] = Field(default_factory=list)
    spec: Optional[DrillSpec] = Field(
        None,
        description="The spec this was graded against, so a consumer can "
        "re-score or explain intent without keeping its own copy.",
    )
    notes: list[str] = Field(default_factory=list)

    def explain(self) -> str:
        """Plain-English verdict for the slide or the console."""
        verdict = "PASS" if self.ts else "FAIL"
        bits = [
            f"TS={verdict}",
            f"CW={'y' if self.cw else 'n'}",
            f"PS={'y' if self.ps else 'n'}",
            f"Prem={'y' if self.prem else 'n'}",
            f"Miss={'y' if self.miss else 'n'}",
            f"Mult={'y' if self.mult else 'n'}",
        ]
        return " | ".join(bits)


#: Shorter alias used in the API contract tables.
FiRunResult = ScoreResult


class FiRun(BaseModel):
    """A drill request as it arrives from the FI console (POST /fi/run).

    This is the *wire shape only*. Convert it with `to_spec()` rather than
    hand-building a DrillSpec, so the guard/target pairing and the defaults
    always match the pattern helpers.
    """

    run_id: str
    pattern: Pattern
    fault: FaultType
    target: FaultTarget
    guard: GuardAfter
    total_occurrences: int = Field(4, ge=1)
    idempotent: bool = Field(
        True,
        description="Defaults to True here, but POST_EFFECT drills are unsafe "
        "to retry - P4 should default it to False for that pattern.",
    )

    def to_spec(self) -> DrillSpec:
        """Convert this wire shape into the internal, gradeable spec."""
        return DrillSpec(
            run_id=self.run_id,
            pattern=self.pattern,
            fault=self.fault,
            target=self.target,
            guard=self.guard,
            total_occurrences=self.total_occurrences,
            idempotent=self.idempotent,
        )