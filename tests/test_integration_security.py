"""Additional security regressions for the cumulative teammate integration."""

import pytest
from fastapi import HTTPException

from factored_bck.agent_auth import AgentAuth
from factored_bck.security import password_hash


def test_agent_unknown_disabled_and_wrong_password_all_verify(store, monkeypatch):
    from factored_bck import agent_auth as module

    with store.connect() as pg:
        for username, enabled in [("active-test", True), ("disabled-test", False)]:
            pg.execute(
                "INSERT INTO simulator.agent_users(username,password_hash,agent_id,enabled) "
                "VALUES(%s,%s,%s,%s)",
                (username, password_hash("team-agent-password"), username, enabled),
            )
    original = module.password_matches
    calls = []

    def verify(password, stored):
        calls.append(stored)
        return original(password, stored)

    monkeypatch.setattr(module, "password_matches", verify)
    auth = AgentAuth(store)
    for username in ["missing-test", "active-test", "disabled-test"]:
        with pytest.raises(HTTPException) as error:
            auth.login(username, "wrong", "private-test")
        assert error.value.status_code == 401
    assert len(calls) == 3
