"""Real PostgreSQL aggregate checks against an isolated, disposable local cluster.

Skipped when initdb/pg_ctl are unavailable. No existing database is contacted.
"""

import json
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from factored_bck.app import create_app
from factored_bck.security import password_hash
from factored_bck.settings import Settings


def insert_evidence(pg, number, action, outcome, status="succeeded"):
    private = f"private-{number}"
    pg.execute(
        "INSERT INTO simulator.actions(action_id,customer_id,idempotency_key,payload,result) "
        "VALUES(%s,%s,%s,%s,%s)",
        (
            private,
            private,
            private,
            Jsonb({"product_id": private, "token": private}),
            Jsonb(
                {
                    "action": action,
                    "outcome": outcome,
                    "status": status,
                    "product_id": private,
                    "request_id": private,
                }
            ),
        ),
    )


def test_empty_action_history_is_not_a_zero_failure_claim(store):
    result = store.action_metrics()
    assert result["total_committed"] == result["total_succeeded"] == 0
    assert result["actions"] == []
    assert result["total_failures"] is None
    assert result["window"]["kind"] == "all_retained_committed_rows"
    assert result["scope"] == "all_customers"


def test_sql_aggregates_all_customers_and_allowlists_labels(store):
    with store.connect() as pg:
        insert_evidence(pg, 1, "pause", "state_change_verified")
        insert_evidence(pg, 2, "pause", "state_change_verified")
        insert_evidence(pg, 3, "replacement", "replacement_request_registered")
        insert_evidence(pg, 4, "unrecognized-charge", "request_registered_for_human_review")
        insert_evidence(pg, 5, "private-action", "private-outcome", "private-status")
    result = store.action_metrics()
    assert result["total_committed"] == 5
    assert result["total_succeeded"] == 4
    assert result["total_failures"] is None
    assert result["actions"] == [
        {"action": "other", "outcome": "other", "committed_count": 1, "succeeded_count": 0},
        {
            "action": "pause",
            "outcome": "state_change_verified",
            "committed_count": 2,
            "succeeded_count": 2,
        },
        {
            "action": "replacement",
            "outcome": "replacement_request_registered",
            "committed_count": 1,
            "succeeded_count": 1,
        },
        {
            "action": "unrecognized-charge",
            "outcome": "request_registered_for_human_review",
            "committed_count": 1,
            "succeeded_count": 1,
        },
    ]
    assert "private" not in json.dumps(result)


def test_rollback_and_uncommitted_actions_are_not_counted(store):
    with store.connect() as pg:
        insert_evidence(pg, 1, "pause", "state_change_verified")
        assert store.action_metrics()["total_committed"] == 0
        pg.rollback()
    assert store.action_metrics()["total_committed"] == 0


def test_real_action_replays_and_rejections_do_not_inflate_committed_counts(store):
    user = {"customer_id": "private-customer"}
    with store.connect() as pg:
        pg.execute(
            "INSERT INTO simulator.fixture_cards VALUES "
            "('private-card','private-customer','Tarjeta Crédito','TEAM-1234',"
            "'USD',0,100,'ACTIVE','team_synthetic')"
        )
        pg.execute(
            "INSERT INTO simulator.card_states(product_id,customer_id,state) "
            "VALUES ('private-card','private-customer','ACTIVE')"
        )
    first = store.action(user, "private-card", "pause", "private-idempotency-key")
    assert store.action(user, "private-card", "pause", "private-idempotency-key") == first
    with pytest.raises(HTTPException) as conflict:
        store.action(user, "private-card", "block", "private-idempotency-key")
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException) as ineligible:
        store.action(user, "private-card", "pause", "private-new-key")
    assert ineligible.value.status_code == 409
    result = store.action_metrics()
    assert result["total_committed"] == result["total_succeeded"] == 1
    assert result["total_failures"] is None
    assert "private" not in json.dumps(result)


def test_endpoint_with_real_session_expiry_and_revocation(store):
    with store.connect() as pg:
        pg.execute(
            "INSERT INTO simulator.users VALUES(%s,%s,%s,%s)",
            ("test-user", password_hash("test-password"), "test-customer", "team_synthetic"),
        )
    with TestClient(create_app(Settings(_env_file=None), store=store)) as client:
        login = client.post(
            "/auth/login", json={"username": "test-user", "password": "test-password"}
        )
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        result = client.get("/operations/metrics", headers=headers)
        assert result.status_code == 200
        assert result.json()["card_actions"]["total_committed"] == 0
        assert token not in result.text
        assert "test-user" not in result.text
        assert client.post("/auth/logout", headers=headers).status_code == 200
        assert client.get("/operations/metrics", headers=headers).status_code == 401
        login = client.post(
            "/auth/login", json={"username": "test-user", "password": "test-password"}
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        with store.connect() as pg:
            pg.execute("UPDATE simulator.sessions SET expires_at=now()-interval '1 second'")
        assert client.get("/operations/metrics", headers=headers).status_code == 401


def test_action_query_timeout_produces_unavailable_metrics(store, monkeypatch):
    # Lock the disposable table: prove the query timeout, not a wall-clock assertion.
    with store.connect() as blocker:
        blocker.execute("LOCK TABLE simulator.actions IN ACCESS EXCLUSIVE MODE")
        app = create_app(Settings(_env_file=None), store=store)
        monkeypatch.setattr(store, "session", lambda _token: {"customer_id": "test-customer"})
        # Initialization needs table locks too; the schema was already initialized above.
        monkeypatch.setattr(store, "initialize", lambda: None)
        with TestClient(app) as client:
            result = client.get("/operations/metrics", headers={"Authorization": "Bearer fixture"})
        assert result.status_code == 200
        assert result.json()["card_actions"] == {
            "status": "unavailable",
            "reason": "aggregation_unavailable",
        }
        assert result.json()["http"]["status"] == "available"


def test_aggregation_transaction_is_read_only(store, monkeypatch):
    connect = store.connect

    class CheckedConnection:
        def __init__(self, pg):
            self.pg = pg

        def execute(self, sql, params=None):
            result = self.pg.execute(sql, params)
            if sql.startswith("SELECT CASE"):
                assert self.pg.execute("SHOW transaction_read_only").fetchone() == {
                    "transaction_read_only": "on"
                }
            return result

    @contextmanager
    def checked_connect():
        with connect() as pg:
            yield CheckedConnection(pg)

    monkeypatch.setattr(store, "connect", checked_connect)
    assert store.action_metrics()["total_committed"] == 0
