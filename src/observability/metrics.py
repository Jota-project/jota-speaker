"""Prometheus metrics for jota-speaker — single source of truth.

All metric instances and helpers live here. Call sites in session.py /
wyoming/handler.py import only the helpers; they never touch
prometheus_client directly. This keeps the door open for an OTel migration
later (only this file would change).
"""
from prometheus_client import Counter, Gauge, Histogram


class _MetricWrapper:
    """Wrapper for prometheus_client metrics to provide safe .clear() method.

    Handles the case where prometheus_client metrics may not have _lock initialized.
    """

    def __init__(self, wrapped_metric):
        self._wrapped = wrapped_metric

    def __getattr__(self, name):
        """Delegate attribute access to wrapped metric."""
        return getattr(self._wrapped, name)

    def clear(self):
        """Clear the metric, handling missing _lock attribute gracefully."""
        try:
            self._wrapped.clear()
        except AttributeError:
            # prometheus_client may not initialize _lock for all metric types.
            # Manually clear via internal structure.
            if hasattr(self._wrapped, '_metrics'):
                # Metric has child metrics (with labels), clear them
                self._wrapped._metrics.clear()
            if hasattr(self._wrapped, '_value'):
                # Direct value reset for simple metrics (e.g., Gauge without labels)
                import threading
                if hasattr(self._wrapped, '_lock'):
                    with self._wrapped._lock:
                        self._wrapped._value.set(0)
                else:
                    # Initialize _lock manually if needed
                    self._wrapped._lock = threading.Lock()
                    with self._wrapped._lock:
                        self._wrapped._value.set(0)

TTFB_HISTOGRAM = _MetricWrapper(Histogram(
    "jota_speaker_ttfb_ms",
    "Time-to-first-byte: from first text event to first audio byte",
    labelnames=["session_type", "engine"],
    buckets=(50, 100, 150, 200, 300, 500, 1000, 2000, 5000),
))

BARGE_IN_LATENCY_HISTOGRAM = _MetricWrapper(Histogram(
    "jota_speaker_barge_in_latency_ms",
    "Barge-in latency: from interrupt message to interrupted message",
    labelnames=["session_type"],
    buckets=(10, 25, 50, 75, 100, 150, 200, 500),
))

SESSIONS_ACTIVE = _MetricWrapper(Gauge(
    "jota_speaker_sessions_active",
    "Active sessions by type",
    labelnames=["session_type"],
))

SYNTHESIS_IN_FLIGHT = _MetricWrapper(Gauge(
    "jota_speaker_engine_synthesis_in_flight",
    "Number of engine.synthesize() coroutines currently executing",
))

SESSIONS_TOTAL = _MetricWrapper(Counter(
    "jota_speaker_sessions_total",
    "Total sessions by terminal result",
    labelnames=["session_type", "result"],
))

ERRORS_TOTAL = _MetricWrapper(Counter(
    "jota_speaker_errors_total",
    "Total errors by code",
    labelnames=["code"],
))

CHUNKS_TOTAL = _MetricWrapper(Counter(
    "jota_speaker_chunks_total",
    "Total chunks by result",
    labelnames=["result"],
))

INTERRUPTS_TOTAL = _MetricWrapper(Counter(
    "jota_speaker_interrupts_total",
    "Total interrupts processed",
))


# ── Helpers (wrap prometheus_client to keep call sites clean) ──────────────

def observe_ttfb_ms(duration_ms: float, session_type: str, engine: str) -> None:
    """Observe TTFB. durations < 0 are dropped (clock skew)."""
    if duration_ms < 0:
        return
    TTFB_HISTOGRAM.labels(session_type=session_type, engine=engine).observe(
        duration_ms / 1000.0
    )


def observe_barge_in_latency_ms(duration_ms: float, session_type: str) -> None:
    """Observe barge-in latency. durations < 0 are dropped."""
    if duration_ms < 0:
        return
    BARGE_IN_LATENCY_HISTOGRAM.labels(session_type=session_type).observe(
        duration_ms / 1000.0
    )


def session_started(session_type: str) -> None:
    SESSIONS_ACTIVE.labels(session_type=session_type).inc()


def session_ended(session_type: str, result: str) -> None:
    SESSIONS_ACTIVE.labels(session_type=session_type).dec()
    SESSIONS_TOTAL.labels(session_type=session_type, result=result).inc()


def error_occurred(code: str) -> None:
    ERRORS_TOTAL.labels(code=code).inc()


def chunk_finished(aborted: bool) -> None:
    CHUNKS_TOTAL.labels(result="aborted" if aborted else "ok").inc()


def interrupt_processed(latency_ms: float, session_type: str) -> None:
    INTERRUPTS_TOTAL.inc()
    observe_barge_in_latency_ms(latency_ms, session_type)


def synthesis_started() -> None:
    SYNTHESIS_IN_FLIGHT.inc()


def synthesis_finished() -> None:
    SYNTHESIS_IN_FLIGHT.dec()
