"""Audited local recovery of unavailable assigned/accepted agents, through Store."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import psycopg
import pytest
from fastapi import HTTPException
from test_handoffs import auth, create
from test_handoffs import backend as handoff_fixture


@pytest.fixture
def backend(store):
    yield from handoff_fixture.__wrapped__(store)


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("loss", ["disabled", "inactive", "language", "experience"])
def test_recover_unavailable_agent_preserves_evidence_and_audits_lifecycle(backend, accepted, loss):
    store, service, client, customers, agents = backend
    original = create(backend)
    if accepted:
        original = service.transition(agents[0], original["handoff_id"], "accept")
    with store.connect() as pg:
        if loss == "disabled":
            pg.execute("UPDATE simulator.agent_users SET enabled=false WHERE agent_id='a1'")
        else:
            if loss == "inactive":
                pg.execute(
                    "UPDATE bank.service_agents SET agent_status='Vacation' WHERE agent_id='a1'"
                )
            elif loss == "language":
                pg.execute("UPDATE bank.service_agents SET languages='inglés' WHERE agent_id='a1'")
            else:
                pg.execute(
                    "UPDATE bank.service_agents SET experience_level='Unknown' WHERE agent_id='a1'"
                )
    recovered = service.recover(original["handoff_id"])
    assert recovered["status"] == "assigned" and recovered["assigned_agent_id"] == "a2"
    assert recovered["accepted_at"] is None
    assert recovered["verified_evidence"] == original["verified_evidence"]
    assert recovered["model_context"] == original["model_context"]
    audit = recovered["recovery"]
    assert audit["prior_status"] == ("accepted" if accepted else "assigned")
    assert audit["prior_agent_id"] == "a1" and audit["assigned_agent_id"] == "a2"
    assert audit["prior_accepted_at"] == original["accepted_at"]
    assert audit["reason"] == "assigned_agent_unavailable"
    assert audit["release_id"] == "test-release"
    assert audit["authority"]["kind"] == "local_database_credentials"
    assert audit["authority"]["database_role"] == "metrics_test"
    assert audit["recovered_at"]
    case_path = "/agent/handoffs/" + original["handoff_id"]
    assert (
        client.post(case_path + "/accept", headers=auth(agents[1])).json()["status"] == "accepted"
    )
    assert (
        client.post(case_path + "/resolve", headers=auth(agents[1])).json()["status"] == "resolved"
    )
    assert service.get(customers[0], original["handoff_id"])["status"] == "resolved"
    with store.connect() as pg:
        assert (
            pg.execute("SELECT count(*) FROM simulator.handoff_recoveries").fetchone()["count"] == 1
        )


def test_recovery_can_queue_then_existing_reroute_assigns_without_losing_history(backend):
    store, service, _, customers, agents = backend
    case = create(backend)
    service.transition(agents[0], case["handoff_id"], "accept")
    with store.connect() as pg:
        pg.execute("UPDATE simulator.agent_users SET enabled=false")
    recovered = service.recover(case["handoff_id"])
    assert recovered["status"] == "queued" and recovered["assigned_agent_id"] is None
    assert recovered["verified_evidence"] == case["verified_evidence"]
    with store.connect() as pg:
        pg.execute("UPDATE simulator.agent_users SET enabled=true WHERE agent_id='a2'")
    assert service.reroute(case["handoff_id"])["assigned_agent_id"] == "a2"
    assert service.get(customers[0], case["handoff_id"])["status"] == "assigned"


def test_recovery_uses_new_accepted_release_eligibility(backend):
    store, service, _, _, agents = backend
    case = create(backend)
    service.transition(agents[0], case["handoff_id"], "accept")
    with store.connect() as pg:
        pg.execute("INSERT INTO bank.releases SELECT 'next-release',manifest FROM bank.releases")
        pg.execute(
            "INSERT INTO bank.customers SELECT 'next-release',customer_id,segment,email "
            "FROM bank.customers"
        )
        pg.execute(
            "INSERT INTO bank.service_agents SELECT 'next-release',agent_id,agent_type,"
            "experience_level,languages,specialty,avg_csat,agent_status,email "
            "FROM bank.service_agents WHERE agent_id='a2'"
        )
        pg.execute("UPDATE bank.current_release SET release_id='next-release'")
    recovered = service.recover(case["handoff_id"])
    assert recovered["assigned_agent_id"] == "a2"
    assert (
        recovered["release_id"] == "test-release"
    )  # Original evidence provenance stays immutable.
    assert recovered["routing"]["release_id"] == "next-release"
    assert recovered["recovery"]["release_id"] == "next-release"


@pytest.mark.parametrize("state", ["eligible", "resolved", "cancelled", "queued"])
def test_recovery_rejects_eligible_or_unsupported_lifecycle(backend, state):
    _, service, _, customers, agents = backend
    case = create(backend, severity="critical" if state == "queued" else "low")
    if state == "resolved":
        service.transition(agents[0], case["handoff_id"], "accept")
        service.transition(agents[0], case["handoff_id"], "resolve")
    elif state == "cancelled":
        service.transition(customers[0], case["handoff_id"], "cancel")
    with pytest.raises(HTTPException) as error:
        service.recover(case["handoff_id"])
    assert error.value.status_code == 409


def test_concurrent_recovery_commits_one_audit_and_prevents_old_agent_transition(backend):
    store, service, _, _, agents = backend
    case = create(backend)
    service.transition(agents[0], case["handoff_id"], "accept")
    with store.connect() as pg:
        pg.execute("UPDATE simulator.agent_users SET enabled=false WHERE agent_id='a1'")

    def operate(_):
        try:
            return service.recover(case["handoff_id"])["status"]
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(operate, range(2)))
    assert sorted(outcomes, key=str) == [409, "assigned"]
    with pytest.raises(HTTPException) as error:
        service.transition(agents[0], case["handoff_id"], "resolve")
    assert error.value.status_code == 401
    with store.connect() as pg:
        assert (
            pg.execute("SELECT count(*) FROM simulator.handoff_recoveries").fetchone()["count"] == 1
        )


def test_recovery_wins_after_old_agent_authentication_without_old_resolution(backend, monkeypatch):
    store, service, _, customers, agents = backend
    case = create(backend)
    service.transition(agents[0], case["handoff_id"], "accept")
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
        future = pool.submit(service.transition, agents[0], case["handoff_id"], "resolve")
        try:
            assert ready.wait(5)
            with store.connect() as pg:
                pg.execute("UPDATE simulator.agent_users SET enabled=false WHERE agent_id='a1'")
            assert service.recover(case["handoff_id"])["assigned_agent_id"] == "a2"
        finally:
            proceed.set()
        with pytest.raises(HTTPException) as error:
            future.result(timeout=5)
    assert error.value.status_code in (401, 404)
    assert service.get(customers[0], case["handoff_id"])["status"] == "assigned"


def test_recovery_audit_and_case_rollback_together_on_commit_failure(backend):
    store, service, _, customers, agents = backend
    case = create(backend)
    original = service.transition(agents[0], case["handoff_id"], "accept")
    with store.connect() as pg:
        pg.execute("UPDATE simulator.agent_users SET enabled=false WHERE agent_id='a1'")
        pg.execute(
            "CREATE FUNCTION simulator.reject_recovery() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'private-synthetic-error'; END $$"
        )
        pg.execute(
            "CREATE CONSTRAINT TRIGGER reject_recovery AFTER INSERT ON "
            "simulator.handoff_recoveries DEFERRABLE INITIALLY DEFERRED "
            "FOR EACH ROW EXECUTE FUNCTION simulator.reject_recovery()"
        )
    with pytest.raises(psycopg.errors.RaiseException):
        service.recover(case["handoff_id"])
    assert service.get(customers[0], case["handoff_id"]) == original
    with store.connect() as pg:
        assert (
            pg.execute("SELECT count(*) FROM simulator.handoff_recoveries").fetchone()["count"] == 0
        )
