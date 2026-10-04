"""Handoff behavior against disposable PostgreSQL, through HTTP and tool interfaces."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from factored_bck.app import create_app
from factored_bck.handoff import CreateHandoffArguments
from factored_bck.handoff_admin import main
from factored_bck.handoff_store import HandoffStore
from factored_bck.security import password_hash
from factored_bck.tools import ExecutionContext

TRIAGE = {
    "reason": "card_support",
    "severity": "low",
    "required_specialty": None,
    "minimum_experience": "Junior",
    "language": "es",
    "summary": "Model claim, not verified",
}


@pytest.fixture
def backend(store):
    with store.connect() as pg:
        pg.execute(
            "CREATE TABLE bank.customers(release_id text,customer_id text,segment text,email text)"
        )
        pg.execute(
            "CREATE TABLE bank.service_agents(release_id text,agent_id text,agent_type text,"
            "experience_level text,languages text,specialty text,avg_csat "
            "numeric,agent_status text,email text)"
        )
        pg.execute(
            "CREATE TABLE bank.products(release_id text,product_id text,customer_id "
            "text,product_type text,"
            "product_number text,currency text,current_balance numeric,credit_limit numeric,"
            "product_status text,last_updated timestamp)"
        )
        for n in (1, 2):
            pg.execute(
                "INSERT INTO bank.customers VALUES('test-release',%s,'Basic','private-email')",
                (f"c{n}",),
            )
            pg.execute(
                "INSERT INTO simulator.users VALUES(%s,%s,%s,'team_synthetic')",
                (f"u{n}", password_hash("customer-password"), f"c{n}"),
            )
            pg.execute(
                "INSERT INTO simulator.fixture_cards VALUES(%s,%s,'Tarjeta Crédito','TEAM-1234',"
                "'USD',10,100,'ACTIVE','team_synthetic')",
                (f"card{n}", f"c{n}"),
            )
            pg.execute(
                "INSERT INTO bank.service_agents VALUES('test-release',%s,'Digital',"
                "'Junior','español',NULL,4,'Active','private-email')",
                (f"a{n}",),
            )
    service = HandoffStore(store)
    for n in (1, 2):
        service.agents.provision(f"agent{n}", f"a{n}", "agent-password")
    customers = [store.login(f"u{n}", "customer-password", "local")["access_token"] for n in (1, 2)]
    agents = [
        service.agents.login(f"agent{n}", "agent-password", "local")["access_token"] for n in (1, 2)
    ]
    app = create_app(store.settings, store=store)
    with TestClient(app) as client:
        yield store, service, client, customers, agents


def auth(token):
    return {"Authorization": "Bearer " + token}


def create(backend, **changes):
    _, _, client, customers, _ = backend
    response = client.post(
        "/me/handoffs",
        headers=auth(customers[0]) | {"Idempotency-Key": "one"},
        json=TRIAGE | changes,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_persistence_shared_tool_contract_and_replay(backend):
    store, service, client, customers, _ = backend
    case = create(backend)
    assert case["persisted"] and case["status"] == "assigned"
    assert case["assigned_agent_id"] == "a1"
    assert case["accepted_at"] is None and case["resolved_at"] is None
    assert case["model_context"]["trust"] == "model_or_customer_provided"
    context = ExecutionContext(session_token=customers[0])
    dispatcher = client.app.state.tools
    result = dispatcher.execute(
        "create_handoff", {"triage": TRIAGE, "idempotency_key": "one"}, context=context
    )
    assert result == {"ok": True, "data": case}
    assert (
        dispatcher.execute("get_handoff", {"handoff_id": case["handoff_id"]}, context=context)
        == result
    )
    assert (
        dispatcher.execute(
            "create_handoff",
            {"triage": TRIAGE | {"severity": "high"}, "idempotency_key": "one"},
            context=context,
        )["error"]["code"]
        == "conflict"
    )
    assert service.metrics()["total_handoffs"] == 1
    assert HandoffStore(store).get(customers[0], case["handoff_id"]) == case
    assert store.handoff(store.session(customers[0]))["verified_action_results"] == []


def test_auth_scope_agent_identity_and_hidden_internal_fields(backend):
    _, service, client, customers, agents = backend
    case = create(backend)
    path = "/me/handoffs/" + case["handoff_id"]
    assert client.get(path).status_code == 401
    assert (
        client.post("/me/handoffs", json=TRIAGE, headers={"Idempotency-Key": "key"}).status_code
        == 401
    )
    assert client.get(path, headers=auth(customers[1])).status_code == 404
    assert client.get("/me/handoffs", headers=auth(customers[1])).json()["handoffs"] == []
    assert client.get("/agent/handoffs", headers=auth(customers[0])).status_code == 401
    assert client.get("/me/handoffs", headers=auth(agents[0])).status_code == 401
    assert (
        client.get("/agent/handoffs/" + case["handoff_id"], headers=auth(agents[1])).status_code
        == 404
    )
    assert client.get("/agent/handoffs", headers=auth(agents[1])).json()["handoffs"] == []
    assert client.get("/agent/handoffs", headers=auth(agents[0])).json()["handoffs"] == [case]
    for field in [
        "customer_id",
        "created_by",
        "idempotency_key",
        "request_payload",
        "password_hash",
        "token_hash",
    ]:
        assert field not in case
    assert service.get(customers[0], case["handoff_id"]) == case


@pytest.mark.parametrize(
    "field,value",
    [
        ("customer_id", "c2"),
        ("agent_id", "a2"),
        ("assigned_agent_id", "a2"),
        ("verified_evidence", {"refund": True}),
        ("severity", "urgent"),
        ("required_specialty", "unsupported"),
    ],
)
def test_override_forged_evidence_and_invalid_triage_rejected(backend, field, value):
    _, service, client, customers, _ = backend
    body = TRIAGE | {field: value}
    response = client.post(
        "/me/handoffs", headers=auth(customers[0]) | {"Idempotency-Key": "one"}, json=body
    )
    assert response.status_code == 422
    result = client.app.state.tools.execute(
        "create_handoff",
        {"triage": body, "idempotency_key": "one"},
        context=ExecutionContext(session_token=customers[0]),
    )
    assert result["error"]["code"] == "invalid_arguments"
    assert service.metrics()["total_handoffs"] == 0


def test_evidence_is_scoped_allowlisted_and_model_claims_are_not_facts(backend):
    store, _, client, customers, _ = backend
    receipts = [
        store.action(store.session(token), f"card{n}", "block", "action")
        for n, token in enumerate(customers, 1)
    ]
    case = create(backend, summary="The model says the refund was completed.", product_id="card1")
    evidence = case["verified_evidence"]
    assert evidence["actions"][0]["receipt"] == receipts[0]
    assert evidence["card"] == {
        "product_id": "card1",
        "simulator_state": "BLOCKED",
        "source_kind": "team_synthetic",
    }
    assert "refund" not in json.dumps(evidence)
    assert "card2" not in json.dumps(case)
    assert "customer" not in json.dumps(evidence)
    response = client.post(
        "/me/handoffs",
        headers=auth(customers[0]) | {"Idempotency-Key": "two"},
        json=TRIAGE | {"product_id": "card2"},
    )
    assert response.status_code == 404
    legacy = client.get("/me/handoff", headers=auth(customers[0])).json()
    assert legacy["verified_action_results"][0]["result"] == receipts[0]


def test_lifecycle_and_customer_cancellation_before_acceptance_only(backend):
    _, service, client, customers, agents = backend
    case = create(backend)
    base = "/agent/handoffs/" + case["handoff_id"]
    assert client.post(base + "/resolve", headers=auth(agents[0])).status_code == 409
    assert client.post(base + "/accept", headers=auth(agents[1])).status_code == 404
    assert client.post(base + "/accept", headers=auth(customers[0])).status_code == 401
    accepted = client.post(base + "/accept", headers=auth(agents[0])).json()
    assert accepted["status"] == "accepted" and accepted["accepted_at"]
    assert client.post(base + "/accept", headers=auth(agents[0])).json() == accepted
    assert (
        client.post(
            "/me/handoffs/" + case["handoff_id"] + "/cancel", headers=auth(customers[0])
        ).status_code
        == 409
    )
    resolved = client.post(base + "/resolve", headers=auth(agents[0])).json()
    assert resolved["status"] == "resolved" and resolved["resolved_at"]
    assert client.post(base + "/resolve", headers=auth(agents[0])).json() == resolved
    assert client.post(base + "/accept", headers=auth(agents[0])).status_code == 409
    # Replays return this same case's CURRENT state, never a new assignment or old success.
    assert (
        service.create(customers[0], CreateHandoffArguments(triage=TRIAGE, idempotency_key="one"))
        == resolved
    )


@pytest.mark.parametrize("unassigned", [False, True])
def test_cancel_blocks_later_acceptance_or_rerouting(backend, unassigned):
    _, service, client, customers, agents = backend
    case = create(backend, severity="critical" if unassigned else "low")
    path = "/me/handoffs/" + case["handoff_id"] + "/cancel"
    assert client.post(path, headers=auth(customers[1])).status_code == 404
    cancelled = client.post(path, headers=auth(customers[0])).json()
    assert cancelled["status"] == "cancelled"
    assert not cancelled["manual_routing_required"]
    assert client.post(path, headers=auth(customers[0])).json() == cancelled
    assert client.post(
        "/agent/handoffs/" + case["handoff_id"] + "/accept", headers=auth(agents[0])
    ).status_code == (404 if unassigned else 409)
    with pytest.raises(HTTPException) as exc:
        service.reroute(case["handoff_id"])
    assert exc.value.status_code == 409


def test_concurrent_idempotency_one_case_and_lifecycle_race(backend):
    _, service, _, customers, agents = backend
    arguments = CreateHandoffArguments(triage=TRIAGE, idempotency_key="race")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: service.create(customers[0], arguments), range(12)))
    assert all(case == results[0] for case in results)
    assert service.metrics()["total_handoffs"] == 1
    handoff_id = results[0]["handoff_id"]

    def transition(operation):
        try:
            service.transition(
                customers[0] if operation == "cancel" else agents[0], handoff_id, operation
            )
            return 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(transition, ["cancel", "accept"]))
    assert sorted(outcomes) == [200, 409]


def test_routing_queue_reroute_and_persistent_metrics(backend):
    store, service, client, customers, _ = backend
    queued = create(backend, severity="critical")
    assert queued["queue"] == "critical_review" and queued["status"] == "queued"
    assert queued["assigned_agent_id"] is None and queued["manual_routing_required"]
    with store.connect() as pg:
        pg.execute("UPDATE bank.service_agents SET experience_level='Senior' WHERE agent_id='a1'")
    assert service.reroute(queued["handoff_id"])["status"] == "queued"
    store.settings.critical_senior_fallback_reasons = ["card_support"]
    routed = service.reroute(queued["handoff_id"])
    assert routed["status"] == "assigned" and routed["routing"]["fallback_used"]
    body = client.get("/operations/metrics", headers=auth(customers[0])).json()["handoffs"]
    assert body["total_handoffs"] == body["assigned"] == body["fallback_assignments"] == 1
    assert body["unassigned"] == body["critical_review"] == 0
    assert body["by_severity"]["critical"] == body["by_required_level"]["Specialist"] == 1
    assert body["by_reason"]["card_support"] == 1
    rendered = json.dumps(body)
    for forbidden in ["a1", "c1", queued["handoff_id"], TRIAGE["summary"], customers[0]]:
        assert forbidden not in rendered


def test_no_provisioned_accounts_and_release_is_pinned(backend):
    store, service, _, customers, _ = backend
    with store.connect() as pg:
        pg.execute("UPDATE simulator.agent_users SET enabled=false")
    case = create(backend)
    assert case["status"] == "queued" and case["queue"] == "manual_review"
    assert case["release_id"] == case["routing"]["release_id"] == "test-release"
    assert service.get(customers[0], case["handoff_id"]) == case


def test_creation_rollback_and_unavailable_metrics_never_claim_transfer(backend, caplog):
    store, service, client, customers, _ = backend
    # Fail after INSERT (deferred commit constraint trigger), not just before routing.
    with store.connect() as pg:
        pg.execute(
            "CREATE FUNCTION simulator.reject_handoff() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'private-database-error'; END $$"
        )
        pg.execute(
            "CREATE CONSTRAINT TRIGGER reject_handoff AFTER INSERT ON simulator.handoffs "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION simulator.reject_handoff()"
        )
    response = client.post(
        "/me/handoffs", headers=auth(customers[0]) | {"Idempotency-Key": "one"}, json=TRIAGE
    )
    assert response.status_code == 500
    tool = client.app.state.tools.execute(
        "create_handoff",
        {"triage": TRIAGE, "idempotency_key": "one"},
        context=ExecutionContext(session_token=customers[0]),
    )
    assert not tool["ok"] and "data" not in tool
    assert service.metrics()["total_handoffs"] == 0
    assert "private-database-error" not in response.text + caplog.text
    with store.connect() as pg:
        pg.execute("DROP TABLE simulator.handoffs CASCADE")
    response = client.get("/operations/metrics", headers=auth(customers[0]))
    assert response.json()["handoffs"] == {
        "status": "unavailable",
        "reason": "aggregation_unavailable",
    }


@pytest.mark.parametrize("change", ["logout", "expiry", "disabled", "inactive"])
def test_agent_sessions_are_revocable_and_bound_to_snapshot_account(backend, change):
    store, service, client, _, agents = backend
    if change == "logout":
        assert client.post("/agent/auth/logout", headers=auth(agents[0])).status_code == 200
    else:
        with store.connect() as pg:
            if change == "expiry":
                pg.execute("UPDATE simulator.agent_sessions SET expires_at='2000-01-01'")
            elif change == "disabled":
                pg.execute("UPDATE simulator.agent_users SET enabled=false")
            else:
                pg.execute("UPDATE bank.service_agents SET agent_status='Vacation'")
    assert client.get("/agent/handoffs", headers=auth(agents[0])).status_code == 401
    with pytest.raises(HTTPException):
        service.agents.session(agents[0])


def test_agent_login_rate_limit_no_secrets_and_preflight(backend, caplog):
    store, service, client, customers, agents = backend
    service.check_configuration()
    for _ in range(10):
        assert (
            client.post(
                "/agent/auth/login", json={"username": "missing", "password": "secret"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/agent/auth/login", json={"username": "missing", "password": "secret"}
        ).status_code
        == 429
    )
    case = create(backend)
    for value in customers + agents:
        assert value not in json.dumps(case) + caplog.text
    response = client.post(
        "/me/handoffs",
        headers=auth(customers[0]) | {"Idempotency-Key": "secret-test"},
        json=TRIAGE | {"summary": customers[0]},
    )
    assert response.status_code == 422 and customers[0] not in response.text
    with store.connect() as pg:
        row = pg.execute("SELECT token_hash FROM simulator.agent_sessions LIMIT 1").fetchone()
    assert row["token_hash"] not in agents and len(row["token_hash"]) == 64


def test_minimal_reader_grants_survive_etl_refresh_without_contact_access(backend):
    store, service, _, _, _ = backend
    sql = Path("deploy/handoff-read-grants.sql").read_text()
    with store.connect() as pg:
        pg.execute("CREATE ROLE backend_api LOGIN")
        pg.execute("SELECT set_config('factored_bck.backend_role','backend_api',true)")
        pg.execute(sql)
        pg.execute("REVOKE ALL ON ALL TABLES IN SCHEMA bank FROM backend_api")
        pg.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA bank REVOKE ALL ON TABLES FROM backend_api")
        pg.execute("GRANT SELECT(customer_id,release_id) ON bank.customers TO backend_api")
        pg.execute("SET LOCAL ROLE backend_api")
        pg.execute("SELECT agent_id,experience_level FROM bank.service_agents")
        pg.execute("SELECT segment FROM bank.customers")
        for table in ("customers", "service_agents"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), pg.transaction():
                pg.execute(f"SELECT email FROM bank.{table}")


def test_admin_failure_does_not_print_sensitive_details(monkeypatch, capsys):
    def fail():
        raise RuntimeError("private-credential")

    monkeypatch.setattr("factored_bck.handoff_admin.Settings", fail)
    assert main(["check"]) == 1
    assert "private-credential" not in capsys.readouterr().out
