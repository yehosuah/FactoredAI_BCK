"""Deterministic HTTP measurements, privacy, bounded storage and fault isolation."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from factored_bck.app import create_app
from factored_bck.metrics import HttpMetrics
from factored_bck.settings import Settings


def registry_with_ticks(*ticks, **kwargs):
    clock = iter(ticks)
    return HttpMetrics(clock=lambda: next(clock), **kwargs)


def test_counts_status_classes_and_latency_percentiles():
    metrics = HttpMetrics(clock=lambda: 0)
    empty = metrics.snapshot()
    assert empty["total_requests"] == empty["total_failures"] == 0
    assert empty["error_rate"] == 0
    assert empty["latency_ms"] == {"sample_count": 0, "p50": None, "p95": None}
    for latency in range(1, 21):
        metrics.record("GET", "/health/live", 500 if latency == 20 else 200, -latency / 1000)
    metrics.record("POST", "/auth/login", 401, -0.021)
    result = metrics.snapshot()
    assert result["total_requests"] == 21
    assert result["total_failures"] == 2
    assert result["error_rate"] == pytest.approx(2 / 21)
    assert result["status_classes"]["4xx"] == result["status_classes"]["5xx"] == 1
    assert result["latency_ms"] == {"sample_count": 21, "p50": 11, "p95": 20}
    route = result["routes"][0]
    assert route["route"] == "/health/live"
    assert route["total_requests"] == 20
    assert route["latency_ms"] == {"sample_count": 20, "p50": 10, "p95": 19}


def test_success_latency_and_failures_keep_existing_error_contract():
    metrics = registry_with_ticks(1, 1.125, 2, 2.25, 3, 3.5, 4, 4.75)
    app = create_app(Settings(_env_file=None), metrics=metrics)

    @app.get("/broken")
    def broken():
        raise RuntimeError("private-error")

    @app.get("/number/{value}")
    def number(value: int):
        return value

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        error = client.get("/broken")
        assert error.status_code == 500
        assert error.json()["error"]["request_id"] == error.headers["x-request-id"]
        assert client.get("/number/invalid").status_code == 422
        assert client.post("/health/live").status_code == 405
    result = metrics.snapshot()
    assert result["total_requests"] == 4
    assert result["total_failures"] == 3
    assert result["latency_ms"] == {"sample_count": 4, "p50": 250, "p95": 750}
    health = next(
        r for r in result["routes"] if r["method"] == "GET" and r["route"] == "/health/live"
    )
    assert health["latency_ms"] == {"sample_count": 1, "p50": 125, "p95": 125}


def test_template_normalization_never_retains_private_request_or_response_data():
    metrics = HttpMetrics(clock=lambda: 0)
    app = create_app(Settings(_env_file=None), metrics=metrics)

    @app.post("/private/{product_id}")
    def private(product_id: str, body: dict):
        return {"product_id": product_id, "body": body, "token": "response-secret"}

    with TestClient(app) as client:
        for private_id in ("secret-card-one", "secret-card-two"):
            assert (
                client.post(
                    f"/private/{private_id}?customer=secret-customer",
                    headers={"Authorization": "Bearer secret-token", "X-Request-ID": "secret-id"},
                    json={"username": "secret-user", "password": "secret-password"},
                ).status_code
                == 200
            )
            assert client.get(f"/missing/{private_id}").status_code == 404
        assert client.request("PRIVATE-METHOD", "/unknown").status_code == 404
    result = metrics.snapshot()
    rendered = json.dumps(result)
    assert "secret" not in rendered
    assert "PRIVATE-METHOD" not in rendered
    assert "product_id" in rendered  # The template variable name is safe.
    rows = {(row["method"], row["route"]): row for row in result["routes"]}
    assert rows[("POST", "/private/{product_id}")]["total_requests"] == 2
    assert rows[("GET", "<unmatched>")]["total_requests"] == 2
    assert rows[("OTHER", "<unmatched>")]["total_requests"] == 1


@pytest.mark.parametrize("failure", ["start", "record"])
@pytest.mark.parametrize(
    "path,status", [("/health/live", 200), ("/missing", 404), ("/broken", 500)]
)
def test_collector_failure_never_changes_normal_response(
    monkeypatch, failure, path, status, caplog
):
    metrics = HttpMetrics()

    def fail(*args):
        raise RuntimeError("private-collector-value")

    monkeypatch.setattr(metrics, failure, fail)
    app = create_app(Settings(_env_file=None), metrics=metrics)

    @app.get("/broken")
    def broken():
        raise ValueError("private-banking-value")

    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == status
    assert response.headers["x-request-id"]
    assert "private" not in response.text
    assert "private" not in caplog.text


def test_bounded_storage_preserves_counters_and_recent_percentiles():
    metrics = HttpMetrics(sample_limit=3, max_series=2, clock=lambda: 0)
    for number in range(1, 101):
        metrics.record("GET", f"/server-template-{number}", 200, -number / 1000)
    result = metrics.snapshot()
    assert result["total_requests"] == 100
    assert result["latency_ms"] == {"sample_count": 3, "p50": 99, "p95": 100}
    assert len(result["routes"]) == 3  # Two configured slots plus a fixed overflow group.
    assert sum(r["total_requests"] for r in result["routes"]) == 100
    assert all(r["latency_ms"]["sample_count"] <= 3 for r in result["routes"])
    overflow = next(r for r in result["routes"] if r["route"] == "<overflow>")
    assert overflow["total_requests"] == 98
    assert overflow["latency_ms"] == result["latency_ms"]


def test_concurrent_collection_and_snapshots_are_consistent():
    metrics = HttpMetrics(sample_limit=8, clock=lambda: 0)

    def collect(_worker):
        for _ in range(100):
            metrics.record("GET", "/health/live", 200, 0)
            result = metrics.snapshot()
            assert result["total_requests"] == sum(r["total_requests"] for r in result["routes"])

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(collect, range(8)))
    assert metrics.snapshot()["total_requests"] == 800
    assert metrics.snapshot()["latency_ms"]["sample_count"] == 8


def test_registries_are_isolated_and_snapshots_do_not_mutate_counters():
    first, second = HttpMetrics(clock=lambda: 0), HttpMetrics()
    first.record("GET", "/health/live", 200, 0)
    result = first.snapshot()
    result["status_classes"]["2xx"] = 999
    result["routes"].clear()
    assert first.snapshot()["status_classes"]["2xx"] == 1
    assert second.snapshot()["total_requests"] == 0


@pytest.mark.parametrize("elapsed", [-1, float("nan"), float("inf")])
def test_invalid_timings_are_dropped(elapsed):
    metrics = HttpMetrics(clock=lambda: elapsed)
    metrics.record("GET", "/health/live", 200, 0)
    assert metrics.snapshot()["total_requests"] == 0


class OperationsStore:
    """Controlled adapter for testing the operations interface without PostgreSQL."""

    def __init__(self):
        self.aggregate_calls = 0

    def initialize(self):
        pass

    def session(self, token):
        if token != "secret-session":
            raise HTTPException(401)
        return {"customer_id": "secret-customer", "username": "secret-user"}

    def action_metrics(self):
        self.aggregate_calls += 1
        return {"status": "available", "scope": "all_customers", "total_committed": 3}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic secret-session"},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Bearer " + "x" * 201},
    ],
)
def test_metrics_requires_session_before_collecting_aggregates(headers):
    store = OperationsStore()
    metrics = HttpMetrics()
    app = create_app(Settings(_env_file=None), store=store, metrics=metrics)
    with TestClient(app) as client:
        response = client.get("/operations/metrics", headers=headers)
    assert response.status_code == 401
    assert store.aggregate_calls == 0
    assert metrics.snapshot()["total_requests"] == 0


def test_endpoint_separates_metrics_and_polling_does_not_count_itself():
    metrics = registry_with_ticks(0, 0.125, 1, 2, 3, 4, 5)
    app = create_app(Settings(_env_file=None), store=OperationsStore(), metrics=metrics)
    with TestClient(app) as client:
        client.get("/health/live")
        for _ in range(5):
            response = client.get(
                "/operations/metrics?private=secret-query",
                headers={"Authorization": "Bearer secret-session"},
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-request-id"]
            body = response.json()
            assert set(body) == {"http", "card_actions", "handoffs", "limitations"}
            assert body["http"]["total_requests"] == 1
            assert body["http"]["latency_ms"]["p95"] == 125
            assert body["card_actions"]["total_committed"] == 3
            assert body["card_actions"]["scope"] == "all_customers"
            assert "secret" not in response.text
    assert metrics.snapshot()["total_requests"] == 1


@pytest.mark.parametrize("broken_section", ["http", "card_actions", "both"])
def test_endpoint_reports_unavailability_without_fabricating_zeros(
    monkeypatch, broken_section, caplog
):
    metrics, store = HttpMetrics(), OperationsStore()

    def fail():
        raise RuntimeError("secret-database-or-collector-details")

    if broken_section in ("http", "both"):
        monkeypatch.setattr(metrics, "snapshot", fail)
    if broken_section in ("card_actions", "both"):
        monkeypatch.setattr(store, "action_metrics", fail)
    app = create_app(Settings(_env_file=None), store=store, metrics=metrics)
    with TestClient(app) as client:
        response = client.get(
            "/operations/metrics", headers={"Authorization": "Bearer secret-session"}
        )
        assert client.get("/health/live").status_code == 200
    assert response.status_code == 200
    body = response.json()
    for section in ("http", "card_actions"):
        if broken_section in (section, "both"):
            assert body[section]["status"] == "unavailable"
            assert "total_requests" not in body[section]
            assert "total_committed" not in body[section]
        else:
            assert body[section]["status"] == "available"
    assert "secret" not in response.text
    assert "secret" not in caplog.text


def test_metrics_route_is_absent_without_the_data_authentication_layer():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        assert client.get("/operations/metrics").status_code == 404


def test_real_card_action_template_and_readiness_failure(monkeypatch):
    metrics, store = HttpMetrics(clock=lambda: 0), OperationsStore()

    def unavailable():
        raise RuntimeError("private-database-details")

    monkeypatch.setattr(store, "ready", unavailable, raising=False)
    app = create_app(Settings(_env_file=None), store=store, metrics=metrics)
    with TestClient(app) as client:
        for card in ("private-card-one", "private-card-two"):
            response = client.post(
                f"/me/cards/{card}/actions",
                headers={"Authorization": "Bearer secret-session"},
                json={"action": "private-payload"},
            )
            assert response.status_code == 422
        assert client.get("/health/ready").status_code == 503
    result = metrics.snapshot()
    actions = next(r for r in result["routes"] if r["method"] == "POST")
    assert actions["route"] == "/me/cards/{product_id}/actions"
    assert actions["total_requests"] == actions["total_failures"] == 2
    assert result["total_failures"] == 3
    assert "private" not in json.dumps(result)


def test_redirects_are_unmatched_and_metrics_method_errors_are_excluded():
    metrics = HttpMetrics(clock=lambda: 0)
    app = create_app(Settings(_env_file=None), store=OperationsStore(), metrics=metrics)
    with TestClient(app) as client:
        assert client.post("/operations/metrics").status_code == 405
        assert client.get("/operations/metrics/", follow_redirects=False).status_code == 307
    result = metrics.snapshot()
    assert result["total_requests"] == 1
    assert result["total_failures"] == 0
    assert result["routes"][0]["route"] == "<unmatched>"
    assert result["status_classes"]["3xx"] == 1
