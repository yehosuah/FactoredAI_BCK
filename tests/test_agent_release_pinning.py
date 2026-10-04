"""Agent account/session writes pin the accepted release through eligibility and commit."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import HTTPException

from factored_bck.agent_auth import AgentAuth


@pytest.fixture
def auth_store(store):
    with store.connect() as pg:
        pg.execute(
            "CREATE TABLE bank.service_agents(release_id text,agent_id text,"
            "agent_status text,agent_type text)"
        )
        pg.execute("INSERT INTO bank.service_agents VALUES('test-release','a1','Active','Digital')")
    return store, AgentAuth(store)


def write(auth, operation):
    if operation == "provision":
        return auth.provision("agent", "a1", "synthetic-agent-password")
    return auth.login("agent", "synthetic-agent-password", "local-test")


@pytest.mark.parametrize("operation", ["provision", "login"])
@pytest.mark.parametrize("change", ["removed", "inactive"])
def test_agent_write_holds_publisher_lock_from_eligibility_through_commit(
    auth_store, monkeypatch, operation, change
):
    store, auth = auth_store
    if operation == "login":
        write(auth, "provision")
    ready, proceed = Event(), Event()
    current = store._current

    def paused(pg, **kwargs):
        release = current(pg, **kwargs)
        ready.set()
        assert proceed.wait(5)
        return release

    monkeypatch.setattr(store, "_current", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(write, auth, operation)
        try:
            assert ready.wait(5)
            with store.connect() as pg:
                admitted = pg.execute(
                    "SELECT pg_try_advisory_xact_lock(7236148201) AS admitted"
                ).fetchone()["admitted"]
                if admitted:
                    pg.execute(
                        "INSERT INTO bank.releases SELECT 'next-release',manifest "
                        "FROM bank.releases"
                    )
                    if change == "inactive":
                        pg.execute(
                            "INSERT INTO bank.service_agents VALUES"
                            "('next-release','a1','Inactive','Digital')"
                        )
                    pg.execute("UPDATE bank.current_release SET release_id='next-release'")
        finally:
            proceed.set()
        result = future.result(timeout=5)
    assert admitted is False, "Publisher cutover overtook an agent account/session commit"
    monkeypatch.setattr(store, "_current", current)
    with store.connect() as pg:
        assert pg.execute("SELECT pg_try_advisory_xact_lock(7236148201) AS admitted").fetchone()[
            "admitted"
        ]
        pg.execute("INSERT INTO bank.releases SELECT 'next-release',manifest FROM bank.releases")
        if change == "inactive":
            pg.execute(
                "INSERT INTO bank.service_agents VALUES('next-release','a1','Inactive','Digital')"
            )
        pg.execute("UPDATE bank.current_release SET release_id='next-release'")
    if operation == "login":
        with pytest.raises(HTTPException) as error:
            auth.session(result["access_token"])
        assert error.value.status_code == 401
    else:
        with pytest.raises(HTTPException) as error:
            write(auth, "login")
        assert error.value.status_code == 401


@pytest.mark.parametrize("operation", ["provision", "login"])
def test_busy_publisher_rejects_agent_write_without_committing_credentials(auth_store, operation):
    store, auth = auth_store
    if operation == "login":
        write(auth, "provision")
    with store.connect() as pg:
        pg.execute("SELECT pg_advisory_xact_lock(7236148201)")
        with pytest.raises(HTTPException) as error:
            write(auth, operation)
        assert error.value.status_code == 503
        assert error.value.headers == {"Retry-After": "1"}
    with store.connect() as pg:
        assert (
            pg.execute("SELECT count(*) AS count FROM simulator.agent_sessions").fetchone()["count"]
            == 0
        )
        if operation == "provision":
            assert (
                pg.execute("SELECT count(*) AS count FROM simulator.agent_users").fetchone()[
                    "count"
                ]
                == 0
            )
