"""Unit tests for src/observability/metrics.py helpers.

Each test verifies the helper updates the underlying prometheus_client
metric correctly. We read the registry via generate_latest() to keep
tests independent of internal API.
"""
from prometheus_client import REGISTRY, generate_latest

from src.observability import metrics
from src.observability.metrics import (
    chunk_finished,
    error_occurred,
    interrupt_processed,
    observe_barge_in_latency_ms,
    observe_ttfb_ms,
    session_ended,
    session_started,
    synthesis_finished,
    synthesis_started,
)


def _counter_value(name: str, **labels) -> float:
    """Read a counter's current value via the registry text output."""
    output = generate_latest(REGISTRY).decode()
    # Sort labels alphabetically to match Prometheus output format
    sorted_labels = ",".join(f"{k}=\"{v}\"" for k, v in sorted(labels.items()))
    if sorted_labels:
        needle = f'{name}{{{sorted_labels}}} '
    else:
        # No labels: metric format is "name value"
        needle = f'{name} '
    for line in output.splitlines():
        if line.startswith(needle):
            return float(line.rsplit(" ", 1)[-1])
    return 0.0


def _gauge_value(name: str, **labels) -> float:
    """Read a gauge's current value via the registry text output."""
    return _counter_value(name, **labels)


def _histogram_count(name: str, **labels) -> float:
    """Read a histogram's _count via the registry text output."""
    suffix = "_count"
    full = name + suffix
    return _counter_value(full, **labels)


def test_observe_ttfb_ms_increments_histogram():
    metrics.TTFB_HISTOGRAM.clear()  # ensure clean state for this label combo
    observe_ttfb_ms(150.0, session_type="ws", engine="mock")
    assert _histogram_count("jota_speaker_ttfb_ms", session_type="ws", engine="mock") == 1.0


def test_observe_ttfb_ms_negative_is_dropped():
    before = _histogram_count("jota_speaker_ttfb_ms", session_type="ws", engine="mock")
    observe_ttfb_ms(-5.0, session_type="ws", engine="mock")
    after = _histogram_count("jota_speaker_ttfb_ms", session_type="ws", engine="mock")
    assert after == before


def test_observe_barge_in_latency_ms_increments_histogram():
    metrics.BARGE_IN_LATENCY_HISTOGRAM.clear()
    observe_barge_in_latency_ms(45.0, session_type="ws")
    assert _histogram_count("jota_speaker_barge_in_latency_ms", session_type="ws") == 1.0


def test_session_started_and_ended_updates_gauge_and_counter():
    metrics.SESSIONS_ACTIVE.clear()
    metrics.SESSIONS_TOTAL.clear()
    session_started("ws")
    assert _gauge_value("jota_speaker_sessions_active", session_type="ws") == 1.0
    session_ended("ws", "ok")
    assert _gauge_value("jota_speaker_sessions_active", session_type="ws") == 0.0
    assert _counter_value("jota_speaker_sessions_total", session_type="ws", result="ok") == 1.0


def test_error_occurred_increments_counter():
    metrics.ERRORS_TOTAL.clear()
    error_occurred("synthesis_error")
    assert _counter_value("jota_speaker_errors_total", code="synthesis_error") == 1.0
    error_occurred("synthesis_error")
    assert _counter_value("jota_speaker_errors_total", code="synthesis_error") == 2.0


def test_chunk_finished_ok_and_aborted():
    metrics.CHUNKS_TOTAL.clear()
    chunk_finished(aborted=False)
    chunk_finished(aborted=True)
    assert _counter_value("jota_speaker_chunks_total", result="ok") == 1.0
    assert _counter_value("jota_speaker_chunks_total", result="aborted") == 1.0


def test_interrupt_processed_increments_counter_and_histogram():
    # INTERRUPTS_TOTAL has no labels, so its own .clear() isn't safe to call
    # (see _MetricWrapper removal); read a before/after delta instead, same
    # pattern as test_observe_ttfb_ms_negative_is_dropped.
    metrics.BARGE_IN_LATENCY_HISTOGRAM.clear()
    before = _counter_value("jota_speaker_interrupts_total")
    interrupt_processed(60.0, session_type="ws")
    after = _counter_value("jota_speaker_interrupts_total")
    assert after == before + 1.0
    assert _histogram_count("jota_speaker_barge_in_latency_ms", session_type="ws") == 1.0


def test_synthesis_in_flight_gauge():
    # SYNTHESIS_IN_FLIGHT has no labels, so its own .clear() isn't safe to
    # call; read a before/after delta instead of resetting global state.
    before = _gauge_value("jota_speaker_engine_synthesis_in_flight")
    synthesis_started()
    assert _gauge_value("jota_speaker_engine_synthesis_in_flight") == before + 1.0
    synthesis_finished()
    assert _gauge_value("jota_speaker_engine_synthesis_in_flight") == before


def test_helper_swallows_exception_from_prometheus_client(monkeypatch):
    """Instrumentation must never take down production: if the underlying
    prometheus_client metric raises, the helper must no-op instead of
    propagating."""

    def _boom(*args, **kwargs):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(metrics.ERRORS_TOTAL, "labels", _boom)

    # Must not raise.
    error_occurred("some_code")
