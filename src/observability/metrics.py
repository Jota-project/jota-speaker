"""Prometheus metrics for jota-speaker — single source of truth.

All metric instances and helpers live here. Call sites in session.py /
wyoming/handler.py import only the helpers; they never touch
prometheus_client directly. This keeps the door open for an OTel migration
later (only this file would change).
"""
import functools
from typing import Callable, TypeVar

from prometheus_client import Counter, Gauge, Histogram

TTFB_HISTOGRAM = Histogram(
    "jota_speaker_ttfb_ms",
    "Time-to-first-byte: from first text event to first audio byte",
    labelnames=["session_type", "engine"],
    buckets=(50, 100, 150, 200, 300, 500, 1000, 2000, 5000),
)

BARGE_IN_LATENCY_HISTOGRAM = Histogram(
    "jota_speaker_barge_in_latency_ms",
    "Barge-in latency: from interrupt message to interrupted message",
    labelnames=["session_type"],
    buckets=(10, 25, 50, 75, 100, 150, 200, 500),
)

SESSIONS_ACTIVE = Gauge(
    "jota_speaker_sessions_active",
    "Active sessions by type",
    labelnames=["session_type"],
)

SYNTHESIS_IN_FLIGHT = Gauge(
    "jota_speaker_engine_synthesis_in_flight",
    "Number of engine.synthesize() coroutines currently executing",
)

SESSIONS_TOTAL = Counter(
    "jota_speaker_sessions_total",
    "Total sessions by terminal result",
    labelnames=["session_type", "result"],
)

ERRORS_TOTAL = Counter(
    "jota_speaker_errors_total",
    "Total errors by code",
    labelnames=["code"],
)

CHUNKS_TOTAL = Counter(
    "jota_speaker_chunks_total",
    "Total chunks by result",
    labelnames=["result"],
)

INTERRUPTS_TOTAL = Counter(
    "jota_speaker_interrupts_total",
    "Total interrupts processed",
)


_F = TypeVar("_F", bound=Callable[..., None])


def _safe(func: _F) -> _F:
    """Make a metrics helper never raise.

    Instrumentation must never take down production: if the underlying
    prometheus_client registry/metric raises for any reason, swallow it
    and no-op instead of propagating to the caller. KeyboardInterrupt and
    SystemExit are intentionally not caught.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            return None

    return wrapper  # type: ignore[return-value]


def _load_enabled_at_import() -> bool:
    """Read the metrics_enabled flag once at module import time.

    Cached rather than re-read per call: cheap, and a runtime env change
    without a process restart is an accepted limitation (see README).
    """
    try:
        from src.core.config import get_settings

        return get_settings().metrics_enabled
    except Exception:
        return True


_ENABLED: bool = _load_enabled_at_import()


def _noop_if_disabled(func: _F) -> _F:
    """Skip the wrapped helper entirely when JOTA_METRICS_ENABLED=false."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not _ENABLED:
            return None
        return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


# ── Helpers (wrap prometheus_client to keep call sites clean) ──────────────

@_noop_if_disabled
@_safe
def observe_ttfb_ms(duration_ms: float, session_type: str, engine: str) -> None:
    """Observe TTFB. durations < 0 are dropped (clock skew)."""
    if duration_ms < 0:
        return
    TTFB_HISTOGRAM.labels(session_type=session_type, engine=engine).observe(
        duration_ms / 1000.0
    )


@_noop_if_disabled
@_safe
def observe_barge_in_latency_ms(duration_ms: float, session_type: str) -> None:
    """Observe barge-in latency. durations < 0 are dropped."""
    if duration_ms < 0:
        return
    BARGE_IN_LATENCY_HISTOGRAM.labels(session_type=session_type).observe(
        duration_ms / 1000.0
    )


@_noop_if_disabled
@_safe
def session_started(session_type: str) -> None:
    SESSIONS_ACTIVE.labels(session_type=session_type).inc()


@_noop_if_disabled
@_safe
def session_ended(session_type: str, result: str) -> None:
    SESSIONS_ACTIVE.labels(session_type=session_type).dec()
    SESSIONS_TOTAL.labels(session_type=session_type, result=result).inc()


@_noop_if_disabled
@_safe
def error_occurred(code: str) -> None:
    ERRORS_TOTAL.labels(code=code).inc()


@_noop_if_disabled
@_safe
def chunk_finished(aborted: bool) -> None:
    CHUNKS_TOTAL.labels(result="aborted" if aborted else "ok").inc()


@_noop_if_disabled
@_safe
def interrupt_processed(latency_ms: float, session_type: str) -> None:
    INTERRUPTS_TOTAL.inc()
    observe_barge_in_latency_ms(latency_ms, session_type)


@_noop_if_disabled
@_safe
def synthesis_started() -> None:
    SYNTHESIS_IN_FLIGHT.inc()


@_noop_if_disabled
@_safe
def synthesis_finished() -> None:
    SYNTHESIS_IN_FLIGHT.dec()
