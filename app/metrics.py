"""Lightweight Prometheus metrics for AgentTrafficCop.

Design principles:
  * Zero external dependencies — the Prometheus text format is simple
    enough to implement directly.
  * Bounded cardinality — we never label by session_id, API key, or
    anything user-controlled. All metrics are aggregate.
  * Thread-safe — every mutation goes through a single RLock.

Exposition endpoint: GET /metrics (registered in routes.py)
Prometheus text format spec: https://prometheus.io/docs/instrumenting/exposition_formats/
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from typing import Dict, List, Tuple

logger = logging.getLogger("agenttrafficcop.metrics")


# ---------------------------------------------------------------------------
# In-memory metric primitives
# ---------------------------------------------------------------------------


class _Counters:
    """Thread-safe labeled counters.

    Values are monotonically increasing integers. Keyed by a tuple of
    (name, label_dict_as_frozen_tuple).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = defaultdict(float)

    def inc(self, name: str, labels: Dict[str, str], amount: float = 1.0) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._data[key] += amount

    def get(self, name: str, labels: Dict[str, str]) -> float:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            return self._data.get(key, 0.0)

    def snapshot(self) -> List[Tuple[str, Dict[str, str], float]]:
        with self._lock:
            return [
                (name, dict(labels), value)
                for (name, labels), value in self._data.items()
            ]


class _Gauges:
    """Thread-safe labeled gauges. Last-value-wins semantics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}

    def set(self, name: str, labels: Dict[str, str], value: float) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._data[key] = float(value)

    def snapshot(self) -> List[Tuple[str, Dict[str, str], float]]:
        with self._lock:
            return [
                (name, dict(labels), value)
                for (name, labels), value in self._data.items()
            ]


class _Histograms:
    """Simple cumulative-bucket histograms for latency.

    Buckets are in seconds. Each histogram tracks:
      - one counter per bucket (how many observations <= bucket)
      - sum of all observations
      - count of all observations

    Only a single global set of buckets is supported per metric name.
    """

    DEFAULT_BUCKETS = (
        0.001,   # 1ms
        0.005,   # 5ms
        0.01,    # 10ms
        0.025,   # 25ms
        0.05,    # 50ms
        0.1,     # 100ms
        0.25,    # 250ms
        0.5,     # 500ms
        1.0,     # 1s
        2.5,     # 2.5s
        5.0,     # 5s
        10.0,    # 10s
    )

    def __init__(self, buckets: Tuple[float, ...] | None = None) -> None:
        self.buckets = buckets or self.DEFAULT_BUCKETS
        self._lock = threading.Lock()
        self._data: Dict[str, List[List[float]]] = {}

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            state = self._data.get(name)
            if state is None:
                state = [[0.0] * len(self.buckets), 0.0, 0.0]
                self._data[name] = state

            bucket_counts, total_sum, total_count = state
            for i, b in enumerate(self.buckets):
                if value <= b:
                    bucket_counts[i] += 1.0
            state[1] += value
            state[2] += 1.0

    def snapshot(self) -> Dict[str, Tuple[List[float], float, float]]:
        with self._lock:
            return {
                name: (list(state[0]), state[1], state[2])
                for name, state in self._data.items()
            }


# ---------------------------------------------------------------------------
# Module-level metric registry
# ---------------------------------------------------------------------------


_counters = _Counters()
_gauges = _Gauges()
_histograms = _Histograms()

_START_TIME = time.time()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def inc_request(endpoint: str, method: str, status: int) -> None:
    """Record one HTTP request."""
    _counters.inc(
        "atc_requests_total",
        {"endpoint": endpoint, "method": method, "status": str(status)},
    )


def observe_latency(endpoint: str, seconds: float) -> None:
    """Record the duration of one request."""
    _histograms.observe(f"atc_request_duration_seconds:{endpoint}", seconds)


def inc_approved() -> None:
    """Record one approved message."""
    _counters.inc("atc_messages_approved_total", {})


def inc_trip(reason: str) -> None:
    """Record one circuit breaker trip by reason.

    Valid reasons: repeated_payload, cyclic_payload, step_limit.
    """
    _counters.inc("atc_governance_trips_total", {"reason": reason})


def set_active_sessions(count: int) -> None:
    """Set the current active session gauge."""
    _gauges.set("atc_sessions_active", {}, float(count))


# ---------------------------------------------------------------------------
# Prometheus text exposition
# ---------------------------------------------------------------------------


def _fmt_labels(labels: Dict[str, str]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


def _escape(value: str) -> str:
    """Escape a label value per Prometheus text format rules."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def render() -> str:
    """Produce the Prometheus text format for all current metrics."""
    lines: List[str] = []

    # --- Counters -----------------------------------------------------------
    counter_snapshot = _counters.snapshot()
    counter_families: Dict[str, List[Tuple[Dict[str, str], float]]] = defaultdict(list)
    for name, labels, value in counter_snapshot:
        counter_families[name].append((labels, value))

    for name in sorted(counter_families.keys()):
        lines.append(f"# TYPE {name} counter")
        for labels, value in counter_families[name]:
            lines.append(f"{name}{_fmt_labels(labels)} {value}")

    # --- Gauges -------------------------------------------------------------
    gauge_snapshot = _gauges.snapshot()
    gauge_families: Dict[str, List[Tuple[Dict[str, str], float]]] = defaultdict(list)
    for name, labels, value in gauge_snapshot:
        gauge_families[name].append((labels, value))

    for name in sorted(gauge_families.keys()):
        lines.append(f"# TYPE {name} gauge")
        for labels, value in gauge_families[name]:
            lines.append(f"{name}{_fmt_labels(labels)} {value}")

    # --- Histograms ---------------------------------------------------------
    histogram_snapshot = _histograms.snapshot()
    by_metric: Dict[str, List[Tuple[str, List[float], float, float]]] = defaultdict(list)
    for full_name, (bucket_counts, total_sum, total_count) in histogram_snapshot.items():
        if ":" in full_name:
            metric_name, endpoint = full_name.split(":", 1)
        else:
            metric_name, endpoint = full_name, ""
        by_metric[metric_name].append((endpoint, bucket_counts, total_sum, total_count))

    for metric_name in sorted(by_metric.keys()):
        lines.append(f"# TYPE {metric_name} histogram")
        for endpoint, bucket_counts, total_sum, total_count in by_metric[metric_name]:
            labels_base = {"endpoint": endpoint} if endpoint else {}

            for i, bucket in enumerate(_histograms.buckets):
                labels = dict(labels_base)
                labels["le"] = _fmt_float(bucket)
                lines.append(
                    f"{metric_name}_bucket{_fmt_labels(labels)} {bucket_counts[i]}"
                )

            labels = dict(labels_base)
            labels["le"] = "+Inf"
            lines.append(f"{metric_name}_bucket{_fmt_labels(labels)} {total_count}")

            lines.append(
                f"{metric_name}_sum{_fmt_labels(labels_base)} {total_sum}"
            )
            lines.append(
                f"{metric_name}_count{_fmt_labels(labels_base)} {total_count}"
            )

    # --- Process info -------------------------------------------------------
    lines.append("# TYPE atc_uptime_seconds gauge")
    lines.append(f"atc_uptime_seconds {time.time() - _START_TIME:.3f}")

    return "\n".join(lines) + "\n"


def _fmt_float(value: float) -> str:
    """Format a float the way Prometheus expects (no scientific notation)."""
    if value == int(value):
        return str(int(value))
    return repr(value)