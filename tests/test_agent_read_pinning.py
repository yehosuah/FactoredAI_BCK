"""Agent reads and committed replays retain accepted-source authorization until commit."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import HTTPException
from test_handoffs import backend as handoff_fixture
from test_handoffs import create


@pytest.fixture
def backend(store):
    yield from handoff_fixture.__wrapped__(store)


def read(service, token, case, operation):
    if operation == "session":
        return service.agents.session(token)
    if operation == "get":
        return service.agent_get(token, case["handoff_id"])
    if operation == "list":
        return service.list(token, agent=True)
    return service.transition(token, case["handoff_id"], "accept")


def accepted(backend):
    _, service, _, _, agents = backend
    case = create(backend)
    return service.transition(agents[0], case["handoff_id"], "accept")


def publish(pg, change):
    pg.execute("INSERT INTO bank.releases SELECT 'next-release',manifest FROM bank.releases")
    if change == "inactive":
        pg.execute(
            "INSERT INTO bank.service_agents SELECT 'next-release',agent_id,agent_type,"
            "experience_level,languages,specialty,avg_csat,'Inactive',email "
            "FROM bank.service_agents"
        )
    pg.execute("UPDATE bank.current_release SET release_id='next-release'")


@pytest.mark.parametrize("operation", ["session", "get", "list", "replay"])
@pytest.mark.parametrize("change", ["removed", "inactive"])
def test_source_authorization_cannot_use_snapshot_after_publisher_cutover(
    backend, monkeypatch, operation, change
):
    store, service, _, _, agents = backend
    case = accepted(backend)
    current = store._current
    ready, proceed = Event(), Event()

    def paused(pg, **kwargs):
        release = current(pg, **kwargs)
        if not ready.is_set():
            ready.set()
            assert proceed.wait(5)
        return release

    monkeypatch.setattr(store, "_current", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(read, service, agents[0], case, operation)
        try:
            assert ready.wait(5)
            with store.connect() as pg:
                admitted = pg.execute(
                    "SELECT pg_try_advisory_xact_lock(7236148201) AS admitted"
                ).fetchone()["admitted"]
                if admitted:
                    publish(pg, change)
        finally:
            proceed.set()
        future.result(timeout=5)
    assert admitted is False, "Source authorization used an already-superseded accepted snapshot"
    monkeypatch.setattr(store, "_current", current)
    with store.connect() as pg:
        assert pg.execute("SELECT pg_try_advisory_xact_lock(7236148201) AS admitted").fetchone()[
            "admitted"
        ]
        publish(pg, change)
    with pytest.raises(HTTPException) as error:
        read(service, agents[0], case, operation)
    assert error.value.status_code == 401


@pytest.mark.parametrize("operation", ["get", "list", "replay"])
def test_agent_case_read_retains_publisher_lock_after_session_auth_until_read_commit(
    backend, monkeypatch, operation
):
    store, service, _, _, agents = backend
    case = accepted(backend)
    session = service.agents.session
    ready, proceed = Event(), Event()

    def paused(token, **kwargs):
        identity = session(token, **kwargs)
        if operation != "replay" or kwargs:
            ready.set()
            assert proceed.wait(5)
        return identity

    monkeypatch.setattr(service.agents, "session", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(read, service, agents[0], case, operation)
        try:
            assert ready.wait(5)
            with store.connect() as pg:
                admitted = pg.execute(
                    "SELECT pg_try_advisory_xact_lock(7236148201) AS admitted"
                ).fetchone()["admitted"]
        finally:
            proceed.set()
        future.result(timeout=5)
    assert admitted is False, "Authorization lock ended before the protected case read/replay"


@pytest.mark.parametrize("operation", ["get", "list", "replay"])
def test_busy_publication_fails_agent_case_read_closed(backend, operation):
    store, service, _, _, agents = backend
    case = accepted(backend)
    with store.connect() as pg:
        pg.execute("SELECT pg_advisory_xact_lock(7236148201)")
        with pytest.raises(HTTPException) as error:
            read(service, agents[0], case, operation)
        assert error.value.status_code == 503
        assert error.value.headers == {"Retry-After": "1"}
