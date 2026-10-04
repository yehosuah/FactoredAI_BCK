"""All-customer aggregates require a distinct host-controlled operator credential."""

import pytest
from fastapi.testclient import TestClient

from factored_bck.app import create_app
from factored_bck.settings import Settings

OPERATOR = "synthetic-operator-" + "x" * 40
REPLACEMENT = "synthetic-replacement-" + "y" * 40


@pytest.fixture
def operations(tool_backend, tmp_path):
    store, _, context = tool_backend
    secret = tmp_path / "operator-secret"
    secret.write_text(OPERATOR + "\n")
    secret.chmod(0o600)
    settings = Settings(_env_file=None, metrics_token_file=secret)
    with TestClient(create_app(settings, store=store)) as client:
        yield client, store, context.session_token.get_secret_value(), secret


@pytest.mark.parametrize("customer", ["user-1", "user-2"])
def test_customer_session_cannot_collect_or_poll_other_customer_aggregates(
    operations, monkeypatch, customer
):
    client, store, _, _ = operations
    calls = []
    monkeypatch.setattr(store, "action_metrics", lambda: calls.append(True) or {})
    token = store.login(customer, "test-password", "local-test")["access_token"]
    for _ in range(2):
        response = client.get("/operations/metrics", headers={"Authorization": "Bearer " + token})
        assert response.status_code == 401
        assert "all_customers" not in response.text
    assert calls == []


def test_operator_authority_is_separate_and_rotation_revokes_old_token_immediately(
    operations, caplog
):
    client, store, customer, secret = operations
    headers = {"Authorization": "Bearer " + OPERATOR}
    assert client.get("/operations/metrics", headers=headers).status_code == 200
    store.logout(customer)
    assert client.get("/operations/metrics", headers=headers).status_code == 200
    secret.write_text(REPLACEMENT + "\n")
    assert client.get("/operations/metrics", headers=headers).status_code == 401
    response = client.get("/operations/metrics", headers={"Authorization": "Bearer " + REPLACEMENT})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert OPERATOR not in response.text + caplog.text
    assert REPLACEMENT not in response.text + caplog.text


@pytest.mark.parametrize("failure", ["missing", "empty", "short", "oversize", "nonascii"])
def test_invalid_operator_secret_fails_closed_without_collecting(operations, monkeypatch, failure):
    client, store, _, secret = operations
    calls = []
    monkeypatch.setattr(store, "action_metrics", lambda: calls.append(True) or {})
    if failure == "missing":
        secret.unlink()
    else:
        secret.write_bytes(
            {"empty": b"", "short": b"short", "oversize": b"x" * 300, "nonascii": b"\xff" * 40}[
                failure
            ]
        )
    response = client.get("/operations/metrics", headers={"Authorization": "Bearer " + OPERATOR})
    assert response.status_code == 503
    assert calls == []


def test_metrics_without_operator_configuration_denies_even_valid_customer(tool_backend):
    store, _, context = tool_backend
    with TestClient(create_app(Settings(_env_file=None), store=store)) as client:
        response = client.get(
            "/operations/metrics",
            headers={"Authorization": "Bearer " + context.session_token.get_secret_value()},
        )
    assert response.status_code == 401
