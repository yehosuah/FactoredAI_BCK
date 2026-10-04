"""Combined HTTP/tool journey under backend_api on controlled ETL-shaped fixtures."""

from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from factored_bck.app import create_app
from factored_bck.handoff_store import HandoffStore
from factored_bck.store import Store
from factored_bck.tools import ExecutionContext


def test_restricted_role_journey_confirmations_handoff_cursor_and_refresh(tool_backend):
    admin, _, _ = tool_backend
    with admin.connect() as pg:
        pg.execute("CREATE ROLE backend_api LOGIN")
        pg.execute("ALTER SCHEMA simulator OWNER TO backend_api")
        tables = pg.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='simulator'"
        ).fetchall()
        for table in tables:
            pg.execute(
                sql.SQL("ALTER TABLE simulator.{} OWNER TO backend_api").format(
                    sql.Identifier(table["tablename"])
                )
            )
        pg.execute("ALTER TABLE bank.releases ADD COLUMN published_at timestamptz DEFAULT now()")
        pg.execute(
            "CREATE TABLE bank.etl_runs(run_id text,started_at timestamptz,finished_at timestamptz,"
            "status text,error_code text,phase text)"
        )
        pg.execute(
            "CREATE TABLE bank.customers(release_id text,customer_id text,segment text,email text)"
        )
        pg.execute(
            "CREATE TABLE bank.service_agents(release_id text,agent_id text,agent_type text,"
            "experience_level text,languages text,specialty text,avg_csat numeric,"
            "agent_status text,email text)"
        )
        pg.execute(
            "INSERT INTO bank.customers VALUES"
            "('test-release','customer-1','Premium','private-fixture')"
        )
        pg.execute(
            "INSERT INTO bank.service_agents VALUES"
            "('test-release','agent-1','Digital','Specialist',"
            "'español','Fraudes',4.5,'Active','private-fixture')"
        )
        pg.execute("DELETE FROM simulator.fixture_cards WHERE product_id='card-1'")
        pg.execute(
            "INSERT INTO bank.products VALUES"
            "('test-release','card-1','customer-1','Tarjeta Crédito',"
            "'TEAM-1234','USD',10,1000,'Active','2026-01-01')"
        )
        pg.execute(
            "INSERT INTO simulator.fixture_cards VALUES('pending','customer-1','Tarjeta Crédito',"
            "'TEAM-4321','USD',0,1000,'PENDING_ACTIVATION','team_synthetic')"
        )
        pg.execute(
            "INSERT INTO simulator.card_states(product_id,customer_id,state) "
            "VALUES('pending','customer-1','PENDING_ACTIVATION')"
        )
        pg.execute(
            "ALTER TABLE bank.transactions ALTER COLUMN transaction_date TYPE timestamp "
            "USING transaction_date::timestamp"
        )
        for number in (3, 4):
            pg.execute(
                "INSERT INTO bank.transactions VALUES('test-release','customer-1','card-1',%s,"
                "'2026-01-01 00:00:00','2026-01-02',10,'USD','purchase','posted','Team Fixture')",
                (f"tx-{number}",),
            )
        pg.execute("GRANT USAGE ON SCHEMA bank TO backend_api")
        pg.execute(
            "GRANT SELECT ON bank.releases,bank.current_release,bank.products,bank.transactions,"
            "bank.etl_runs TO backend_api"
        )
        pg.execute("GRANT SELECT(release_id,customer_id) ON bank.customers TO backend_api")
    store = Store(admin.settings.model_copy(update={"db_user": "backend_api"}))
    handoffs = HandoffStore(store)
    # Readiness alone misses handoff grants; preflight catches the actual dependency.
    assert store.ready()["release_id"] == "test-release"
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        handoffs.check_configuration()
    with TestClient(create_app(store.settings, store=store)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
    with admin.connect() as pg:
        pg.execute(Path("deploy/handoff-read-grants.sql").read_text())
    handoffs.check_configuration()
    handoffs.agents.provision("agent-login", "agent-1", "team-agent-password")

    with TestClient(create_app(store.settings, store=store)) as client:
        response = client.post(
            "/auth/login", json={"username": "user-1", "password": "test-password"}
        )
        assert response.status_code == 200
        token = response.json()["access_token"]
        headers = {"Authorization": "Bearer " + token}
        agent = client.post(
            "/agent/auth/login", json={"username": "agent-login", "password": "team-agent-password"}
        )
        assert agent.status_code == 200
        agent_headers = {"Authorization": "Bearer " + agent.json()["access_token"]}
        assert client.get("/health/ready").status_code == 200
        assert client.get("/me/cards", headers=headers).status_code == 200
        assert client.get("/me/cards/card-2", headers=headers).status_code == 404
        assert client.get("/me/cards/card-1", headers=headers).json()["card"]["last_four"] == "1234"

        first = client.get(
            "/me/cards/card-1/movements", params={"limit": 1}, headers=headers
        ).json()
        context = ExecutionContext(session_token=token)
        second = client.app.state.tools.execute(
            "get_movements",
            {"product_id": "card-1", "limit": 1, "cursor": first["next_cursor"]},
            context=context,
        )
        assert second["ok"]
        assert second["data"]["movements"][0]["transaction_id"] == "tx-3"
        assert (
            client.app.state.tools.execute(
                "get_movements", {"product_id": "card-1", "cursor": "bad!"}, context=context
            )["error"]["code"]
            == "invalid_arguments"
        )

        commands = [
            ("card-1", {"action": "pause"}),
            ("card-1", {"action": "reactivate"}),
            ("pending", {"action": "activate"}),
            ("card-1", {"action": "replacement"}),
            (
                "card-1",
                {
                    "action": "unrecognized-charge",
                    "transaction_id": "tx-1",
                    "process_date": "2026-01-02",
                },
            ),
            ("card-1", {"action": "block"}),
        ]
        for number, (product, command) in enumerate(commands):
            before = store.action_metrics()["total_committed"]
            prepared = client.post(
                f"/me/cards/{product}/actions",
                json=command,
                headers=headers | {"Idempotency-Key": f"journey-{number}"},
            )
            assert prepared.status_code == 200, command["action"]
            assert prepared.json()["status"] == "pending" and not prepared.json()["verified"]
            assert store.action_metrics()["total_committed"] == before
            path = "/me/action-confirmations/" + prepared.json()["confirmation_id"] + "/confirm"
            confirmed = client.post(path, headers=headers)
            assert confirmed.status_code == 200
            assert confirmed.json()["verified"] and confirmed.json()["status"] == "executed"
            assert client.post(path, headers=headers).json() == confirmed.json()
            assert store.action_metrics()["total_committed"] == before + 1
        case = client.post(
            "/me/handoffs",
            headers=headers | {"Idempotency-Key": "journey-case"},
            json={
                "reason": "fraud",
                "severity": "high",
                "required_specialty": "Fraudes",
                "minimum_experience": "Senior",
                "language": "es",
                "summary": "Team fixture charge question",
                "product_id": "card-1",
            },
        )
        assert case.status_code == 200
        assert case.json()["persisted"] and case.json()["status"] == "assigned"
        assert case.json()["assigned_agent_id"] == "agent-1"
        assert len(case.json()["verified_evidence"]["actions"]) == 5
        path = "/agent/handoffs/" + case.json()["handoff_id"]
        assert client.post(path + "/accept", headers=agent_headers).json()["status"] == "accepted"
        assert client.post(path + "/resolve", headers=agent_headers).json()["status"] == "resolved"
        metrics = client.get("/operations/metrics", headers=headers).json()
        assert metrics["card_actions"]["total_committed"] == 6
        assert metrics["handoffs"]["total_handoffs"] == 1
        assert client.get("/operations/etl", headers=headers).status_code == 200

        # Mirror the source permission refresh, without any production ETL execution.
        with admin.connect() as pg:
            pg.execute(
                "INSERT INTO bank.releases VALUES('new-release',%s,now())",
                (Jsonb({"contract_version": "card-support-etl-v1"}),),
            )
            for table in ("customers", "service_agents", "products", "transactions"):
                columns = pg.execute(
                    "SELECT column_name FROM information_schema.columns WHERE "
                    "table_schema='bank' AND table_name=%s ORDER BY ordinal_position",
                    (table,),
                ).fetchall()
                names = [column["column_name"] for column in columns]
                rest = sql.SQL(",").join(
                    sql.Identifier(name) for name in names if name != "release_id"
                )
                pg.execute(
                    sql.SQL(
                        "INSERT INTO bank.{} SELECT 'new-release',{} FROM bank.{} "
                        "WHERE release_id='test-release'"
                    ).format(sql.Identifier(table), rest, sql.Identifier(table))
                )
            pg.execute("UPDATE bank.current_release SET release_id='new-release'")
            pg.execute("REVOKE ALL ON ALL TABLES IN SCHEMA bank FROM backend_api")
            pg.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA bank REVOKE ALL ON TABLES FROM backend_api"
            )
            pg.execute(
                "GRANT SELECT ON bank.releases,bank.current_release,"
                "bank.products,bank.transactions,"
                "bank.etl_runs TO backend_api"
            )
            pg.execute("GRANT SELECT(release_id,customer_id) ON bank.customers TO backend_api")
        store.initialize()
        handoffs.check_configuration()
        assert (
            client.get("/me/cards/card-1", headers=headers).json()["card"]["simulator_state"]
            == "BLOCKED"
        )
        assert (
            client.get("/me/handoffs/" + case.json()["handoff_id"], headers=headers).json()[
                "status"
            ]
            == "resolved"
        )
        assert (
            client.get(
                "/me/cards/card-1/movements",
                params={"cursor": first["next_cursor"]},
                headers=headers,
            ).status_code
            == 409
        )
        assert client.get("/agent/handoffs", headers=agent_headers).status_code == 200
        assert store.action_metrics()["total_committed"] == 6
        with store.connect() as pg:
            for table in ("customers", "service_agents"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege), pg.transaction():
                    pg.execute(sql.SQL("SELECT email FROM bank.{}").format(sql.Identifier(table)))
        assert client.post("/auth/logout", headers=headers).status_code == 200
        assert client.get("/me/cards", headers=headers).status_code == 401
