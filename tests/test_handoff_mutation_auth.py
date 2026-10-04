"""Handoff writes must revalidate credentials after waiting for their transaction."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import HTTPException
from test_handoffs import TRIAGE, create
from test_handoffs import backend as handoff_fixture

from factored_bck.handoff import CreateHandoffArguments


@pytest.fixture
def backend(store):
    yield from handoff_fixture.__wrapped__(store)


def test_logout_between_initial_auth_and_case_creation_denies_write(backend, monkeypatch):
    store, service, _, customers, _ = backend
    ready, proceed = Event(), Event()
    actual = store.session

    def session(token, **kwargs):
        user = actual(token, **kwargs)
        if not kwargs:
            ready.set()
            assert proceed.wait(5)
        return user

    monkeypatch.setattr(store, "session", session)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            service.create,
            customers[0],
            CreateHandoffArguments(triage=TRIAGE, idempotency_key="revoked"),
        )
        try:
            assert ready.wait(5)
            store.logout(customers[0])
        finally:
            proceed.set()
        with pytest.raises(HTTPException) as error:
            future.result(timeout=5)
    assert error.value.status_code == 401
    assert service.metrics()["total_handoffs"] == 0


def test_disabling_agent_after_initial_auth_prevents_acceptance(backend, monkeypatch):
    store, service, _, customers, agents = backend
    case = create(backend)
    ready, proceed = Event(), Event()
    actual = service.agents.session

    def session(token, **kwargs):
        identity = actual(token, **kwargs)
        if not kwargs:
            ready.set()
            assert proceed.wait(5)
        return identity

    monkeypatch.setattr(service.agents, "session", session)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.transition, agents[0], case["handoff_id"], "accept")
        try:
            assert ready.wait(5)
            with store.connect() as pg:
                pg.execute("UPDATE simulator.agent_users SET enabled=false WHERE agent_id='a1'")
        finally:
            proceed.set()
        with pytest.raises(HTTPException) as error:
            future.result(timeout=5)
    assert error.value.status_code == 401
    assert service.get(customers[0], case["handoff_id"])["status"] == "assigned"


@pytest.mark.parametrize("loss", ["language", "experience"])
def test_assignment_no_longer_meeting_routing_requirements_cannot_accept(backend, loss):
    store, service, _, customers, agents = backend
    case = create(backend)
    with store.connect() as pg:
        if loss == "language":
            pg.execute("UPDATE bank.service_agents SET languages='inglés' WHERE agent_id='a1'")
        else:
            pg.execute(
                "UPDATE bank.service_agents SET experience_level='Unknown' WHERE agent_id='a1'"
            )
    with pytest.raises(HTTPException) as error:
        service.transition(agents[0], case["handoff_id"], "accept")
    assert error.value.status_code == 409
    assert service.get(customers[0], case["handoff_id"])["status"] == "assigned"
