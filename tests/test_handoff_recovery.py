"""Local recovery, persistent audit and serialized races on private synthetic PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from time import monotonic, sleep

import psycopg
import pytest
from fastapi import HTTPException
from test_handoffs import auth, create
from test_handoffs import backend as backend

from factored_bck.handoff_admin import main
from factored_bck.handoff_store import HandoffStore
from factored_bck.tools import ExecutionContext

REASON = "Assigned agent is no longer eligible"
AUTHORITY = "local-admin:synthetic-operator"


def recover(service, case):
    return service.recover(case["handoff_id"], reason=REASON, authority=AUTHORITY)


def stored(store, case):
    with store.connect() as pg:
        return pg.execute(
            "SELECT * FROM simulator.handoffs WHERE handoff_id=%s", (case["handoff_id"],)
        ).fetchone()


def disable(store, agent="a1"):
    with store.connect() as pg:
        pg.execute("UPDATE simulator.agent_users SET enabled=false WHERE agent_id=%s", (agent,))


@pytest.mark.parametrize("accepted", [False, True])
def test_disabled_recovery_preserves_audit_evidence_and_requires_fresh_acceptance(
    backend, accepted
):
    store, service, client, customers, agents = backend
    store.action(store.session(customers[0]), "card1", "block", "evidence")
    case = create(backend, product_id="card1")
    if accepted:
        case = service.transition(agents[0], case["handoff_id"], "accept")
    before = stored(store, case)
    action_history = client.get("/me/handoff", headers=auth(customers[0])).json()
    disable(store)
    result = recover(service, case)
    assert result["status"] == "assigned" and result["assigned_agent_id"] == "a2"
    assert result["accepted_at"] is None
    after = stored(store, case)
    for field in (
        "verified_evidence",
        "model_context",
        "request_payload",
        "release_id",
        "created_at",
    ):
        assert after[field] == before[field]
    assert client.get("/me/handoff", headers=auth(customers[0])).json() == action_history
    (audit,) = after["recovery_history"]
    assert audit["previous_agent_id"] == "a1"
    assert audit["previous_status"] == before["status"]
    assert audit["previous_assigned_at"] == case["assigned_at"]
    assert audit["previous_accepted_at"] == case["accepted_at"]
    assert audit["previous_routing"] == case["routing"]
    assert audit["reason"] == REASON and audit["authority"] == AUTHORITY
    assert audit["release_id"] == "test-release"
    assert audit["recovered_at"] == result["assigned_at"] == result["updated_at"]
    assert audit["resulting_status"] == "assigned" and audit["resulting_agent_id"] == "a2"
    assert audit["resulting_queue"] is None
    assert audit["resulting_routing"] == result["routing"]
    assert "recovery_history" not in result  # Operator metadata stays local.
    assert HandoffStore(store).get(customers[0], case["handoff_id"]) == result
    with pytest.raises(HTTPException) as exc:
        service.transition(agents[1], case["handoff_id"], "resolve")
    assert exc.value.status_code == 409
    service.transition(agents[1], case["handoff_id"], "accept")
    assert service.transition(agents[1], case["handoff_id"], "resolve")["status"] == "resolved"


@pytest.mark.parametrize(
    "change",
    ["missing", "inactive", "type", "language", "specialty", "experience", "unknown_experience"],
)
def test_ineligible_in_new_accepted_release(backend, change):
    store, service, _, _, agents = backend
    with store.connect() as pg:
        pg.execute("UPDATE bank.service_agents SET specialty='Fraudes',experience_level='Senior'")
    case = create(backend, reason="fraud", required_specialty="Fraudes")
    service.transition(agents[0], case["handoff_id"], "accept")
    with store.connect() as pg:
        pg.execute("INSERT INTO bank.releases SELECT 'new-release',manifest FROM bank.releases")
        pg.execute(
            "INSERT INTO bank.service_agents SELECT 'new-release',agent_id,agent_type,"
            "experience_level,languages,specialty,avg_csat,agent_status,email "
            "FROM bank.service_agents WHERE release_id='test-release'"
        )
        changes = {
            "missing": "DELETE FROM bank.service_agents",
            "inactive": "UPDATE bank.service_agents SET agent_status='Vacation'",
            "type": "UPDATE bank.service_agents SET agent_type='Branch'",
            "language": "UPDATE bank.service_agents SET languages='inglés'",
            "specialty": "UPDATE bank.service_agents SET specialty='Ventas'",
            "experience": "UPDATE bank.service_agents SET experience_level='Junior'",
            "unknown_experience": "UPDATE bank.service_agents SET experience_level='Unknown'",
        }
        pg.execute(changes[change] + " WHERE release_id='new-release' AND agent_id='a1'")
        pg.execute("UPDATE bank.current_release SET release_id='new-release'")
    result = recover(service, case)
    assert result["assigned_agent_id"] == "a2"
    assert result["release_id"] == "test-release"
    assert result["routing"]["release_id"] == "new-release"
    (audit,) = stored(store, case)["recovery_history"]
    assert audit["previous_status"] == "accepted" and audit["release_id"] == "new-release"


@pytest.mark.parametrize(
    "severity,queue", [("low", "manual_review"), ("critical", "critical_review")]
)
def test_requeue_and_multiple_recoveries_append_without_losing_history(backend, severity, queue):
    store, service, _, _, agents = backend
    with store.connect() as pg:
        pg.execute("UPDATE bank.service_agents SET experience_level='Specialist'")
    case = create(backend, severity=severity)
    service.transition(agents[0], case["handoff_id"], "accept")
    disable(store)
    recover(service, case)
    first = stored(store, case)["recovery_history"]
    service.transition(agents[1], case["handoff_id"], "accept")
    disable(store, "a2")
    result = recover(service, case)
    assert result["status"] == "queued" and result["assigned_agent_id"] is None
    assert result["queue"] == queue and result["manual_routing_required"]
    assert result["assigned_at"] is None and result["accepted_at"] is None
    history = stored(store, case)["recovery_history"]
    assert len(history) == 2 and history[:1] == first
    assert history[1]["previous_agent_id"] == "a2"
    assert history[1]["previous_status"] == "accepted"
    assert history[1]["resulting_status"] == "queued"
    assert history[1]["resulting_agent_id"] is None and history[1]["resulting_queue"] == queue
    with store.connect() as pg:
        pg.execute("UPDATE simulator.agent_users SET enabled=true WHERE agent_id='a1'")
    assert service.reroute(case["handoff_id"])["assigned_agent_id"] == "a1"
    store.initialize()  # Additive startup is idempotent and retains history.
    assert stored(store, case)["recovery_history"] == history


@pytest.mark.parametrize("status", ["resolved", "cancelled", "assigned", "accepted", "queued"])
def test_invalid_state_or_still_eligible_rejected_without_mutation(backend, status):
    store, service, _, customers, agents = backend
    case = create(backend, severity="critical" if status == "queued" else "low")
    if status in ("accepted", "resolved"):
        service.transition(agents[0], case["handoff_id"], "accept")
    if status == "resolved":
        service.transition(agents[0], case["handoff_id"], "resolve")
    if status == "cancelled":
        service.transition(customers[0], case["handoff_id"], "cancel")
    if status in ("resolved", "cancelled"):
        disable(store)
    else:
        # Higher-ranked replacement is not grounds to recover an eligible agent.
        with store.connect() as pg:
            pg.execute("UPDATE bank.service_agents SET avg_csat=5 WHERE agent_id='a2'")
    before = stored(store, case)
    with pytest.raises(HTTPException) as exc:
        recover(service, case)
    assert exc.value.status_code == 409
    assert stored(store, case) == before


def test_no_customer_agent_or_model_recovery_surface(backend):
    store, service, client, customers, agents = backend
    case = create(backend)
    disable(store)
    before = stored(store, case)
    for prefix, token in (("me", customers[0]), ("agent", agents[1])):
        assert (
            client.post(
                f"/{prefix}/handoffs/{case['handoff_id']}/recover",
                headers=auth(token),
                json={"reason": REASON, "authority": AUTHORITY},
            ).status_code
            == 404
        )
    for name in ("recover", "recover_handoff", "handoff_recovery", "reroute"):
        result = client.app.state.tools.execute(
            name,
            {"handoff_id": case["handoff_id"]},
            context=ExecutionContext(session_token=customers[0]),
        )
        assert not result["ok"]
    assert not any("recover" in path for path in client.app.openapi()["paths"])
    assert stored(store, case) == before


def test_cli_recovers_using_local_authority_without_printing_metadata(backend, monkeypatch, capsys):
    store, _, _, _, _ = backend
    case = create(backend)
    disable(store)
    monkeypatch.setattr("factored_bck.handoff_admin.Store", lambda _: store)
    monkeypatch.setattr("factored_bck.handoff_admin.getpass.getuser", lambda: "synthetic-operator")
    assert main(["recover", "--handoff-id", case["handoff_id"], "--reason", REASON]) == 0
    output = capsys.readouterr().out
    assert "Recovery result: assigned" in output
    assert REASON not in output and AUTHORITY not in output and case["handoff_id"] not in output
    assert stored(store, case)["recovery_history"][0]["authority"] == AUTHORITY


@pytest.mark.parametrize("field,value", [("reason", " "), ("reason", "x" * 501), ("authority", "")])
def test_invalid_audit_metadata_rejected(backend, field, value):
    store, service, _, _, _ = backend
    case = create(backend)
    disable(store)
    before = stored(store, case)
    with pytest.raises(ValueError):
        service.recover(
            case["handoff_id"], **({"reason": REASON, "authority": AUTHORITY} | {field: value})
        )
    assert stored(store, case) == before


def test_commit_failure_rolls_back_assignment_and_audit(backend):
    store, service, _, _, _ = backend
    case = create(backend)
    disable(store)
    before = stored(store, case)
    with store.connect() as pg:
        pg.execute(
            "CREATE FUNCTION simulator.reject_recovery() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'synthetic-commit-failure'; END $$"
        )
        pg.execute(
            "CREATE CONSTRAINT TRIGGER reject_recovery AFTER UPDATE ON simulator.handoffs "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION simulator.reject_recovery()"
        )
    with pytest.raises(psycopg.Error):
        recover(service, case)
    assert stored(store, case) == before


def outcome(call):
    try:
        return call()["status"]
    except HTTPException as exc:
        return exc.status_code


def test_concurrent_recoveries_commit_once(backend):
    store, service, _, _, _ = backend
    case = create(backend)
    disable(store)
    barrier = Barrier(6)

    def attempt(_):
        barrier.wait(timeout=10)
        return outcome(lambda: recover(service, case))

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))
    assert results.count("assigned") == 1 and results.count(409) == 5
    assert len(stored(store, case)["recovery_history"]) == 1


@pytest.mark.parametrize("operation", ["accept", "resolve"])
@pytest.mark.parametrize("first", ["transition", "recovery"])
def test_transition_recovery_races_serialize_both_orders(backend, monkeypatch, operation, first):
    store, service, _, _, agents = backend
    case = create(backend)
    if operation == "resolve":
        service.transition(agents[0], case["handoff_id"], "accept")
    # Language loss allows agent authentication but fails this case's routing eligibility.
    with store.connect() as pg:
        pg.execute("UPDATE bank.service_agents SET languages='inglés' WHERE agent_id='a1'")
    locked, release, second_started = Event(), Event(), Event()
    original_resource = service._resource

    def paused_resource(row):
        # Both operations call this while retaining the handoff row lock, before commit.
        if not locked.is_set():
            locked.set()
            assert release.wait(10)
        return original_resource(row)

    monkeypatch.setattr(service, "_resource", paused_resource)
    calls = {
        "transition": lambda: service.transition(agents[0], case["handoff_id"], operation),
        "recovery": lambda: recover(service, case),
    }
    second = "recovery" if first == "transition" else "transition"

    def competing_call():
        second_started.set()
        return outcome(calls[second])

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(outcome, calls[first])
        try:
            assert locked.wait(10)
            follower = pool.submit(competing_call)
            assert second_started.wait(10)
            # Confirm PostgreSQL actually has a blocked row-lock waiter before releasing.
            deadline = monotonic() + 10
            with store.connect() as pg:
                pg.autocommit = True  # Refresh pg_stat_activity for each observation.
                while True:
                    waiting = pg.execute(
                        "SELECT count(*) AS n FROM pg_stat_activity "
                        "WHERE datname=current_database() "
                        "AND cardinality(pg_blocking_pids(pid)) > 0"
                    ).fetchone()["n"]
                    if waiting:
                        break
                    assert monotonic() < deadline, "competing transaction did not reach the lock"
                    sleep(0.01)
        finally:
            release.set()
        results = {first: leader.result(timeout=10), second: follower.result(timeout=10)}
    row = stored(store, case)
    if first == "recovery":
        assert results == {"recovery": "assigned", "transition": 404}
        assert row["status"] == "assigned" and row["assigned_agent_id"] == "a2"
        assert len(row["recovery_history"]) == 1
    elif operation == "resolve":
        assert results == {"transition": "resolved", "recovery": 409}
        assert row["status"] == "resolved" and row["recovery_history"] == []
    else:
        assert results == {"transition": "accepted", "recovery": "assigned"}
        (audit,) = row["recovery_history"]
        assert audit["previous_status"] == "accepted" and audit["previous_accepted_at"]
        assert row["status"] == "assigned" and row["assigned_agent_id"] == "a2"
    assert row["verified_evidence"] == case["verified_evidence"]
