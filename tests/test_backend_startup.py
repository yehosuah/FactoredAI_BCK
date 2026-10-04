"""Real private-socket server startup, old-schema migration and process/database restart."""

import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from httpx2 import Client, HTTPTransport
from psycopg import sql

from factored_bck.handoff_store import HandoffStore
from factored_bck.store import Store


@contextmanager
def server(settings, directory):
    socket = Path(directory) / "api.sock"
    socket.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("BCK_")}
    env.update(
        BCK_DATA_ENABLED="true",
        BCK_DB_HOST=settings.db_host,
        BCK_DB_USER=settings.db_user,
        BCK_DB_NAME=settings.db_name,
        PYTHONPATH=str(Path("src").resolve()),
    )
    with (Path(directory) / "api.log").open("a") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "factored_bck.app:create_app",
                "--factory",
                "--uds",
                str(socket),
                "--no-access-log",
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            with Client(
                transport=HTTPTransport(uds=str(socket)), base_url="http://private-test", timeout=3
            ) as client:
                deadline = time.monotonic() + 10
                while True:
                    assert process.poll() is None, "Isolated backend startup exited"
                    try:
                        if client.get("/health/live").status_code == 200:
                            break
                    except OSError:
                        pass
                    except Exception as exc:
                        if type(exc).__name__ != "ConnectError":
                            raise
                    assert time.monotonic() < deadline, "Isolated backend startup timed out"
                    time.sleep(0.05)
                yield client, env
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            socket.unlink(missing_ok=True)


def test_restricted_fresh_start_migrates_preserves_recovery_and_restarts(tool_backend):
    admin, _, _ = tool_backend
    directory = admin.settings.db_host
    with admin.connect() as pg:
        pg.execute("CREATE ROLE backend_api LOGIN")
        pg.execute("ALTER SCHEMA simulator OWNER TO backend_api")
        for row in pg.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='simulator'"
        ).fetchall():
            pg.execute(
                sql.SQL("ALTER TABLE simulator.{} OWNER TO backend_api").format(
                    sql.Identifier(row["tablename"])
                )
            )
        # Reproduce main's pre-feature simulator while retaining its fixture users/cards.
        for table in (
            "action_confirmations",
            "handoff_recoveries",
            "handoffs",
            "agent_sessions",
            "agent_users",
            "agent_login_attempts",
        ):
            pg.execute(sql.SQL("DROP TABLE simulator.{}").format(sql.Identifier(table)))
        pg.execute("ALTER TABLE simulator.card_states DROP COLUMN revision")
        pg.execute(
            "CREATE TABLE bank.customers(release_id text,customer_id text,segment text,email text)"
        )
        pg.execute(
            "CREATE TABLE bank.service_agents(release_id text,agent_id text,agent_type text,"
            "experience_level text,languages text,specialty text,avg_csat numeric,"
            "agent_status text,email text)"
        )
        pg.execute(
            "INSERT INTO bank.customers "
            "VALUES('test-release','customer-1','Basic','private-fixture')"
        )
        for number in (1, 2):
            pg.execute(
                "INSERT INTO bank.service_agents VALUES"
                "('test-release',%s,'Digital','Junior','español',NULL,4,'Active','private-fixture')",
                (f"agent-{number}",),
            )
        pg.execute("GRANT USAGE ON SCHEMA bank TO backend_api")
        pg.execute(
            "GRANT SELECT ON bank.releases,bank.current_release,bank.products,"
            "bank.transactions TO backend_api"
        )
        pg.execute("GRANT SELECT(release_id,customer_id) ON bank.customers TO backend_api")
    settings = admin.settings.model_copy(update={"db_user": "backend_api"})
    store = Store(settings)
    handoffs = HandoffStore(store)
    with server(settings, directory) as (client, env):
        assert client.get("/health/ready").status_code == 503
        with admin.connect() as pg:
            pg.execute("SELECT set_config('factored_bck.backend_role','backend_api',true)")
            pg.execute(Path("deploy/handoff-read-grants.sql").read_text())
        assert client.get("/health/ready").status_code == 200
        handoffs.check_configuration()
        for number in (1, 2):
            handoffs.agents.provision(
                f"agent-{number}", f"agent-{number}", "synthetic-agent-password"
            )
        login = client.post("/auth/login", json={"username": "user-1", "password": "test-password"})
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        agent_login = client.post(
            "/agent/auth/login",
            json={"username": "agent-1", "password": "synthetic-agent-password"},
        )
        assert agent_login.status_code == 200
        agent_headers = {"Authorization": "Bearer " + agent_login.json()["access_token"]}
        item = client.post(
            "/me/cards/card-1/actions",
            headers=headers | {"Idempotency-Key": "process-pause"},
            json={"action": "pause"},
        ).json()
        assert item["status"] == "pending"
        path = "/me/action-confirmations/" + item["confirmation_id"] + "/confirm"
        receipt = client.post(path, headers=headers).json()
        assert receipt["verified"] and receipt["evidence"]["simulator_state"] == "PAUSED"
        case = client.post(
            "/me/handoffs",
            headers=headers | {"Idempotency-Key": "process-case"},
            json={
                "reason": "card_support",
                "severity": "low",
                "required_specialty": None,
                "minimum_experience": "Junior",
                "language": "es",
                "summary": "Synthetic process restart",
                "product_id": "card-1",
            },
        ).json()
        case_path = "/agent/handoffs/" + case["handoff_id"]
        assert case["assigned_agent_id"] == "agent-1"
        assert (
            client.post(case_path + "/accept", headers=agent_headers).json()["status"] == "accepted"
        )
        with admin.connect() as pg:
            pg.execute("UPDATE simulator.agent_users SET enabled=false WHERE agent_id='agent-1'")
        recovery = subprocess.run(
            [
                sys.executable,
                "-m",
                "factored_bck.handoff_admin",
                "recover",
                "--handoff-id",
                case["handoff_id"],
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert recovery.returncode == 0 and "Recovery result: assigned" in recovery.stdout
        assert login.json()["access_token"] not in recovery.stdout + recovery.stderr
        assert (
            client.get("/me/handoffs/" + case["handoff_id"], headers=headers).json()[
                "assigned_agent_id"
            ]
            == "agent-2"
        )
    subprocess.run(
        [
            "pg_ctl",
            "-D",
            str(Path(directory) / "data"),
            "-l",
            str(Path(directory) / "postgres.log"),
            "-m",
            "fast",
            "-w",
            "restart",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    with server(settings, directory) as (client, _):
        assert client.get("/health/ready").status_code == 200
        assert (
            client.get("/me/cards/card-1", headers=headers).json()["card"]["simulator_state"]
            == "PAUSED"
        )
        assert client.post(path, headers=headers).json() == receipt
        assert store.action_metrics()["total_committed"] == 1
        recovered = client.get("/me/handoffs/" + case["handoff_id"], headers=headers).json()
        assert recovered["status"] == "assigned" and recovered["assigned_agent_id"] == "agent-2"
        with store.connect() as pg:
            audit = pg.execute("SELECT * FROM simulator.handoff_recoveries").fetchone()
        assert audit["prior_status"] == "accepted" and audit["prior_accepted_at"]
        assert audit["authority"]["database_role"] == "backend_api"
        new_login = client.post(
            "/agent/auth/login",
            json={"username": "agent-2", "password": "synthetic-agent-password"},
        )
        new_headers = {"Authorization": "Bearer " + new_login.json()["access_token"]}
        assert (
            client.post(case_path + "/accept", headers=new_headers).json()["status"] == "accepted"
        )
        assert (
            client.post(case_path + "/resolve", headers=new_headers).json()["status"] == "resolved"
        )
