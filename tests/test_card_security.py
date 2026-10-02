"""Simulator boundaries, API validation and session enforcement with controlled fixtures."""

import pytest
from fastapi.testclient import TestClient

from factored_bck.app import create_app
from factored_bck.security import next_state, password_hash, password_matches
from factored_bck.settings import Settings


def test_salted_credentials_and_wrong_password():
    first = password_hash("team-test-password")
    second = password_hash("team-test-password")
    assert first != second
    assert password_matches("team-test-password", first)
    assert not password_matches("wrong", first)
    assert not password_matches("team-test-password", "broken")


@pytest.mark.parametrize(
    "state,action,result",
    [
        ("ACTIVE", "pause", "PAUSED"),
        ("PAUSED", "reactivate", "ACTIVE"),
        ("PENDING_ACTIVATION", "activate", "ACTIVE"),
        ("PAUSED", "block", "BLOCKED"),
    ],
)
def test_eligible_state_transitions(state, action, result):
    assert next_state(state, action) == result


@pytest.mark.parametrize(
    "state,action",
    [
        ("BLOCKED", "reactivate"),
        ("INELIGIBLE", "reactivate"),
        ("ACTIVE", "activate"),
        ("CLOSED", "block"),
    ],
)
def test_ineligible_transitions(state, action):
    with pytest.raises(ValueError, match="ineligible_card_state"):
        next_state(state, action)


class FixtureStore:
    def initialize(self):
        pass

    def ready(self):
        return {"release_id": "team-fixture"}

    def session(self, token):
        from fastapi import HTTPException

        if token != "team-test-token":
            raise HTTPException(401)
        return {"customer_id": "team-customer", "source_kind": "team_synthetic"}

    def cards(self, user):
        return {"customer_id": user["customer_id"], "cards": []}


def test_api_enforces_session_and_rejects_customer_override():
    with TestClient(create_app(Settings(_env_file=None), store=FixtureStore())) as client:
        assert client.get("/me/cards").status_code == 401
        assert client.get("/me/cards", headers={"Authorization": "Bearer wrong"}).status_code == 401
        auth = {"Authorization": "Bearer team-test-token"}
        assert client.get("/me/cards", headers=auth).json()["customer_id"] == "team-customer"
        response = client.post(
            "/me/cards/team-card/actions",
            headers={**auth, "Idempotency-Key": "key"},
            json={"action": "pause", "customer_id": "other"},
        )
        assert response.status_code == 422
        assert "other" not in response.text
