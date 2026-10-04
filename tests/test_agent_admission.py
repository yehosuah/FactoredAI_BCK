"""Agent login must retain the merged main password-work admission boundary."""

import pytest
from fastapi import HTTPException

from factored_bck.agent_auth import AgentAuth
from factored_bck.store import LOGIN_KDF_LOCK


def test_rotating_agent_names_cannot_bypass_peer_budget(store):
    auth = AgentAuth(store)
    for number in range(30):
        with pytest.raises(HTTPException) as error:
            auth.login(f"missing-{number}", "wrong", "one-peer")
        assert error.value.status_code == 401
    with pytest.raises(HTTPException) as error:
        auth.login("another-name", "wrong", "one-peer")
    assert error.value.status_code == 429


def test_agent_kdf_is_rejected_while_customer_slot_is_occupied(store, monkeypatch):
    def must_not_verify(*_args):
        pytest.fail("Agent password work bypassed cross-worker admission")

    monkeypatch.setattr("factored_bck.agent_auth.password_matches", must_not_verify)
    with store.connect() as pg:
        pg.execute("SELECT pg_advisory_xact_lock(%s)", (LOGIN_KDF_LOCK,))
        with pytest.raises(HTTPException) as error:
            AgentAuth(store).login("missing", "wrong", "peer")
        assert error.value.status_code == 429
        assert error.value.headers == {"Retry-After": "1"}
