"""Committed transition replay remains recoverable after suitability changes, with fresh auth."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import HTTPException
from test_handoffs import backend as handoff_fixture
from test_handoffs import create


@pytest.fixture
def backend(store):
    yield from handoff_fixture.__wrapped__(store)


def committed(backend, operation):
    store, service, _, _, agents = backend
    with store.connect() as pg:
        pg.execute("UPDATE bank.service_agents SET specialty='Fraudes'")
    case = create(backend, required_specialty="Fraudes")
    result = service.transition(agents[0], case["handoff_id"], "accept")
    if operation == "resolve":
        result = service.transition(agents[0], case["handoff_id"], "resolve")
    return result


def cutover(store, *, removed=False):
    with store.connect() as pg:
        pg.execute("SELECT pg_advisory_xact_lock(7236148201)")
        pg.execute("INSERT INTO bank.releases SELECT 'next-release',manifest FROM bank.releases")
        pg.execute(
            "INSERT INTO bank.customers SELECT 'next-release',customer_id,segment,email "
            "FROM bank.customers"
        )
        pg.execute(
            "INSERT INTO bank.service_agents SELECT 'next-release',agent_id,agent_type,"
            "experience_level,'inglés',specialty,avg_csat,agent_status,email "
            "FROM bank.service_agents WHERE agent_id<>'a1' OR NOT %s",
            (removed,),
        )
        pg.execute("UPDATE bank.current_release SET release_id='next-release'")


@pytest.mark.parametrize("operation", ["accept", "resolve"])
@pytest.mark.parametrize("change", ["language", "specialty", "experience", "release"])
def test_replay_returns_identical_committed_result_after_case_suitability_loss(
    backend, operation, change
):
    store, service, _, _, agents = backend
    original = committed(backend, operation)
    if change == "release":
        cutover(store)
    else:
        with store.connect() as pg:
            if change == "language":
                pg.execute("UPDATE bank.service_agents SET languages='inglés' WHERE agent_id='a1'")
            elif change == "specialty":
                pg.execute("UPDATE bank.service_agents SET specialty=NULL WHERE agent_id='a1'")
            else:
                pg.execute(
                    "UPDATE bank.service_agents SET experience_level='Unknown' WHERE agent_id='a1'"
                )
    assert service.transition(agents[0], original["handoff_id"], operation) == original
    assert service.metrics()["total_handoffs"] == 1
    assert store.action_metrics()["total_committed"] == 0


@pytest.mark.parametrize("operation", ["accept", "resolve"])
@pytest.mark.parametrize("revocation", ["logout", "expiry", "disabled", "release_removed"])
def test_replay_requires_current_valid_enabled_session_and_release_membership(
    backend, operation, revocation
):
    store, service, _, _, agents = backend
    original = committed(backend, operation)
    if revocation == "logout":
        service.agents.logout(agents[0])
    elif revocation == "release_removed":
        cutover(store, removed=True)
    else:
        with store.connect() as pg:
            if revocation == "expiry":
                pg.execute("UPDATE simulator.agent_sessions SET expires_at='2000-01-01'")
            else:
                pg.execute("UPDATE simulator.agent_users SET enabled=false WHERE agent_id='a1'")
    with pytest.raises(HTTPException) as error:
        service.transition(agents[0], original["handoff_id"], operation)
    assert error.value.status_code == 401


def test_replay_reauthenticates_after_initial_session_check_and_before_return(backend, monkeypatch):
    store, service, _, _, agents = backend
    original = committed(backend, "resolve")
    ready, proceed = Event(), Event()
    session = service.agents.session

    def paused(token, **kwargs):
        identity = session(token, **kwargs)
        if not kwargs:
            ready.set()
            assert proceed.wait(5)
        return identity

    monkeypatch.setattr(service.agents, "session", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.transition, agents[0], original["handoff_id"], "resolve")
        try:
            assert ready.wait(5)
            with store.connect() as pg:
                pg.execute("UPDATE simulator.agent_users SET enabled=false WHERE agent_id='a1'")
        finally:
            proceed.set()
        with pytest.raises(HTTPException) as error:
            future.result(timeout=5)
    assert error.value.status_code == 401
