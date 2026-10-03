"""Bounded, process-local HTTP aggregates; never retain request objects or identifiers."""

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import ceil, isfinite
from threading import Lock
from time import perf_counter

METRICS_ROUTE = "/operations/metrics"
HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)
STATUS_CLASSES = ("1xx", "2xx", "3xx", "4xx", "5xx", "other")


@dataclass
class _Series:
    latencies: deque
    requests: int = 0
    statuses: dict = field(default_factory=lambda: dict.fromkeys(STATUS_CLASSES, 0))

    def record(self, status_class, latency_ms):
        self.requests += 1
        self.statuses[status_class] += 1
        self.latencies.append(latency_ms)

    def copy(self):
        return self.requests, self.statuses.copy(), tuple(self.latencies)


def _summary(data):
    requests, statuses, latencies = data
    ordered = sorted(latencies)
    failures = statuses["4xx"] + statuses["5xx"]
    return {
        "total_requests": requests,
        "total_failures": failures,
        "error_rate": failures / requests if requests else 0.0,
        "status_classes": statuses,
        "latency_ms": {
            "sample_count": len(ordered),
            "p50": ordered[ceil(len(ordered) * 0.50) - 1] if ordered else None,
            "p95": ordered[ceil(len(ordered) * 0.95) - 1] if ordered else None,
        },
    }


class HttpMetrics:
    """A registry per app instance. Callers isolate failures at the HTTP seam.

    Only pass server-owned route templates, never paths supplied by clients.
    Counters span the registry lifetime; percentiles use bounded recent samples.
    """

    def __init__(self, *, sample_limit=1024, max_series=128, clock=perf_counter):
        if sample_limit < 1 or max_series < 1:
            raise ValueError("metrics_limits_must_be_positive")
        self._clock = clock
        self._sample_limit = sample_limit
        self._max_series = max_series
        self._started_at = datetime.now(UTC).isoformat()
        self._lock = Lock()
        self._total = _Series(deque(maxlen=sample_limit))
        self._series = {}

    def start(self):
        return self._clock()

    def record(self, method, route_template, status, started):
        if route_template == METRICS_ROUTE:
            return
        latency_ms = (self._clock() - started) * 1000
        if not isfinite(latency_ms) or latency_ms < 0:
            return
        method = method if method in HTTP_METHODS else "OTHER"
        route = route_template or "<unmatched>"
        # Templates are server-owned; cap their length as well as series cardinality.
        if len(route) > 256:
            route = "<unmatched>"
        status_class = f"{status // 100}xx" if 100 <= status < 600 else "other"
        with self._lock:
            key = (method, route)
            if key not in self._series and len(self._series) >= self._max_series:
                key = ("OTHER", "<overflow>")
            if key not in self._series:
                self._series[key] = _Series(deque(maxlen=self._sample_limit))
            self._series[key].record(status_class, latency_ms)
            self._total.record(status_class, latency_ms)

    def snapshot(self):
        with self._lock:
            total = self._total.copy()
            series = [(key, value.copy()) for key, value in self._series.items()]
            observed_at = datetime.now(UTC).isoformat()
        # Sorting happens outside the collection lock.
        return {
            "status": "available",
            "scope": "application_instance",
            "window": {
                "started_at": self._started_at,
                "observed_at": observed_at,
                "counters": "since_registry_creation",
                "latency": "latest_samples_per_series_and_total",
                "sample_limit": self._sample_limit,
                "max_route_series": self._max_series,
                "overflow_series": 1,
            },
            "latency_measurement": "middleware_entry_to_response_headers",
            "percentile_method": "nearest_rank",
            "failure_definition": "http_4xx_or_5xx",
            "excluded_routes": [METRICS_ROUTE],
            **_summary(total),
            "routes": [
                {"method": method, "route": route, **_summary(values)}
                for (method, route), values in sorted(series)
            ],
        }
