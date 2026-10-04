"""Review regressions against a private disposable PostgreSQL cluster and synthetic data."""

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from factored_bck.app import create_app
from factored_bck.security import password_hash
from factored_bck.settings import Settings
from factored_bck.store import Store


@pytest.fixture(scope="module")
def regression_postgres():
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if not initdb or not pg_ctl:
        pytest.skip("Requires initdb/pg_ctl; uses a disposable cluster with TCP disabled")
    with TemporaryDirectory(prefix="bck-review-", dir="/tmp") as directory:
        data = str(Path(directory) / "data")
        subprocess.run(
            [initdb, "-D", data, "-A", "trust", "-U", "review_test", "--no-locale", "-E", "UTF8"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            [
                pg_ctl,
                "-D",
                data,
                "-l",
                str(Path(directory) / "postgres.log"),
                "-o",
                f"-F -h '' -k {directory}",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        try:
            yield Settings(
                _env_file=None, db_host=directory, db_name="postgres", db_user="review_test"
            )
        finally:
            subprocess.run(
                [pg_ctl, "-D", data, "-m", "immediate", "-w", "stop"],
                check=True,
                capture_output=True,
                timeout=30,
            )


@pytest.fixture
def regression_store(regression_postgres, tmp_path):
    secret = tmp_path / "demo-password"
    secret.write_text("old-team-password")
    cfg = regression_postgres.model_copy(update={"demo_password_file": secret})
    store = Store(cfg)
    with store.connect() as pg:
        pg.execute("DROP SCHEMA IF EXISTS simulator CASCADE")
        pg.execute("DROP SCHEMA IF EXISTS bank CASCADE")
        pg.execute("CREATE SCHEMA simulator")
        pg.execute("CREATE SCHEMA bank")
        pg.execute("CREATE TABLE bank.releases(release_id text PRIMARY KEY, manifest jsonb)")
        pg.execute("CREATE TABLE bank.current_release(singleton boolean, release_id text)")
        pg.execute(
            "CREATE TABLE bank.products(release_id text, product_id text, customer_id text, "
            "product_type text, product_number text, currency text, current_balance numeric, "
            "credit_limit numeric, product_status text, last_updated timestamp)"
        )
        pg.execute(
            "INSERT INTO bank.releases VALUES ('review-release',%s)",
            (Jsonb({"contract_version": "card-support-etl-v1"}),),
        )
        pg.execute("INSERT INTO bank.current_release VALUES (true,'review-release')")
        pg.execute(
            "CREATE TABLE bank.transactions(release_id text, customer_id text, product_id text, "
            "transaction_id text, transaction_date timestamp, process_date date, amount numeric, "
            "currency text, transaction_type text, transaction_status text, merchant_name text, "
            "PRIMARY KEY(release_id,process_date,transaction_id))"
        )
    store.initialize()
    return store


def status(call, expected):
    with pytest.raises(HTTPException) as error:
        call()
    assert error.value.status_code == expected


def test_rotation_revokes_sessions_and_preserves_state_and_other_users(regression_store):
    store = regression_store
    token = store.login("demo", "old-team-password", "test")["access_token"]
    with store.connect() as pg:
        pg.execute(
            "INSERT INTO simulator.users VALUES ('other',%s,'other','team_synthetic')",
            (password_hash("other-team-password"),),
        )
        old_hash = pg.execute(
            "SELECT password_hash FROM simulator.users WHERE username='demo'"
        ).fetchone()
    other = store.login("other", "other-team-password", "test")["access_token"]
    store.action(store.session(token), "TEAM-CARD-ACTIVE", "pause", "review-pause")
    store.initialize()
    assert store.session(token)["username"] == "demo"
    with store.connect() as pg:
        assert (
            pg.execute("SELECT password_hash FROM simulator.users WHERE username='demo'").fetchone()
            == old_hash
        )
    store.settings.demo_password_file.write_text("new-team-password")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: store.initialize(), range(2)))
    status(lambda: store.login("demo", "old-team-password", "test"), 401)
    status(lambda: store.session(token), 401)
    assert store.session(other)["username"] == "other"
    new_token = store.login("demo", "new-team-password", "test")["access_token"]
    assert (
        store.card(store.session(new_token), "TEAM-CARD-ACTIVE")["card"]["simulator_state"]
        == "PAUSED"
    )
    assert len(store.handoff(store.session(new_token))["verified_action_results"]) == 1
    store.initialize()
    assert store.session(new_token)["username"] == "demo"


def test_unknown_and_wrong_password_both_run_password_verification(regression_store, monkeypatch):
    from factored_bck import store as module

    original = module.password_matches
    calls = []

    def verify(password, stored):
        calls.append(stored)
        return original(password, stored)

    monkeypatch.setattr(module, "password_matches", verify)
    status(lambda: regression_store.login("missing", "wrong", "test"), 401)
    status(lambda: regression_store.login("demo", "wrong", "test"), 401)
    assert len(calls) == 2
    assert all(value.startswith("scrypt:") for value in calls)


def test_rotating_usernames_cannot_bypass_peer_budget(regression_store, monkeypatch):
    from factored_bck import store as module

    calls = []
    monkeypatch.setattr(module, "password_matches", lambda *args: calls.append(args) or False)
    for index in range(35):
        status(
            lambda index=index: regression_store.login(f"missing-{index}", "wrong", "peer-a"),
            401 if index < 30 else 429,
        )
    assert len(calls) == 30
    status(lambda: regression_store.login("demo", "wrong", "peer-a"), 429)
    assert len(calls) == 30
    status(lambda: regression_store.login("missing", "wrong", "peer-b"), 401)
    assert len(calls) == 31
    with regression_store.connect() as pg:
        pg.execute("UPDATE simulator.login_attempts SET window_start=now()-interval '6 minutes'")
    status(lambda: regression_store.login("missing-after-expiry", "wrong", "peer-a"), 401)
    assert len(calls) == 32


def test_peer_bursts_do_not_lock_out_an_unrelated_account(regression_store, monkeypatch):
    from factored_bck import store as module

    calls = []
    original = module.password_matches
    monkeypatch.setattr(module, "password_matches", lambda *args: calls.append(args) or False)
    for index in range(120):
        status(
            lambda index=index: regression_store.login(
                f"missing-{index}", "wrong", f"peer-{index // 30}"
            ),
            401,
        )
    assert len(calls) == 120
    monkeypatch.setattr(module, "password_matches", original)
    assert regression_store.login("demo", "old-team-password", "new-peer")["access_token"]


def test_kdf_concurrency_is_bounded_across_store_instances(regression_store, monkeypatch):
    from threading import Event

    from factored_bck import store as module

    entered, release = Event(), Event()
    calls = []

    def verify(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return False

    monkeypatch.setattr(module, "password_matches", verify)
    other_store = Store(regression_store.settings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(regression_store.login, "missing", "wrong", "peer-a")
        assert entered.wait(5)
        try:
            status(lambda: other_store.login("demo", "wrong", "peer-b"), 429)
            status(lambda: other_store.login("another-missing", "wrong", "peer-c"), 429)
            assert len(calls) == 1
        finally:
            release.set()
        with pytest.raises(HTTPException) as error:
            first.result(timeout=5)
        assert error.value.status_code == 401


def test_successful_login_does_not_reset_peer_budget(regression_store):
    for _ in range(30):
        assert regression_store.login("demo", "old-team-password", "peer-a")["access_token"]
    status(lambda: regression_store.login("demo", "old-team-password", "peer-a"), 429)
    status(lambda: regression_store.login("missing", "wrong", "peer-a"), 429)
    assert regression_store.login("demo", "old-team-password", "peer-b")["access_token"]


def seed_movements(store):
    with store.connect() as pg:
        # Many same-day, same-time rows and one older day, all controlled fixtures.
        for tx, process, time in [
            ("t1", "2026-01-03", "2026-01-02 14:00:00"),
            ("t2", "2026-01-03", "2026-01-02 14:00:00"),
            ("t3", "2026-01-03", "2026-01-02 14:00:00"),
            ("t4", "2026-01-03", "2026-01-02 13:00:00"),
            ("t1", "2026-01-02", "2026-01-01 14:00:00"),
        ]:
            pg.execute(
                "INSERT INTO bank.transactions VALUES ('review-release','TEAM-CUSTOMER-001',"
                "'TEAM-CARD-ACTIVE',%s,%s,%s,10,'USD','purchase','posted','Team Fixture')",
                (tx, time, process),
            )


def test_cursor_retrieves_every_same_day_movement_without_duplicates(regression_store):
    store = regression_store
    seed_movements(store)
    token = store.login("demo", "old-team-password", "test")["access_token"]
    with TestClient(create_app(store.settings, store=store)) as client:
        auth = {"Authorization": "Bearer " + token}
        params = {"limit": 2}
        seen = []
        for _ in range(4):
            response = client.get(
                "/me/cards/TEAM-CARD-ACTIVE/movements", params=params, headers=auth
            )
            assert response.status_code == 200
            page = response.json()
            seen.extend((row["process_date"], row["transaction_id"]) for row in page["movements"])
            if "next_cursor" not in page:
                params["before_date"] = page["movements"][-1]["process_date"]
                if len(page["movements"]) < 2:
                    break
            elif page["next_cursor"] is None:
                break
            else:
                params["cursor"] = page["next_cursor"]
        assert seen == [
            ("2026-01-03", "t1"),
            ("2026-01-03", "t2"),
            ("2026-01-03", "t3"),
            ("2026-01-03", "t4"),
            ("2026-01-02", "t1"),
        ]


def test_before_date_remains_exclusive(regression_store):
    seed_movements(regression_store)
    page = regression_store.movements(
        {"customer_id": "TEAM-CUSTOMER-001"}, "TEAM-CARD-ACTIVE", 10, date(2026, 1, 3)
    )
    assert [row["process_date"] for row in page["movements"]] == ["2026-01-02"]


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 100])
def test_cursor_pages_keep_order_at_every_page_boundary(regression_store, limit):
    store = regression_store
    seed_movements(store)
    user = {"customer_id": "TEAM-CUSTOMER-001"}
    cursor = None
    seen = []
    for _ in range(6):
        page = store.movements(user, "TEAM-CARD-ACTIVE", limit, None, cursor)
        seen.extend((r["process_date"], r["transaction_id"]) for r in page["movements"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [
        ("2026-01-03", "t1"),
        ("2026-01-03", "t2"),
        ("2026-01-03", "t3"),
        ("2026-01-03", "t4"),
        ("2026-01-02", "t1"),
    ]


def test_cursor_is_scoped_and_rejects_malformed_or_stale_continuation(regression_store):
    from factored_bck.pagination import MovementCursor

    store = regression_store
    seed_movements(store)
    user = {"customer_id": "TEAM-CUSTOMER-001"}
    page = store.movements(user, "TEAM-CARD-ACTIVE", 1, date(2026, 1, 4))
    cursor = page["next_cursor"]
    # Original before_date remains part of the continuation without being resent.
    assert (
        store.movements(user, "TEAM-CARD-ACTIVE", 1, None, cursor)["movements"][0]["transaction_id"]
        == "t2"
    )
    status(lambda: store.movements(user, "TEAM-CARD-ACTIVE", 1, date(2026, 1, 3), cursor), 422)
    status(lambda: store.movements(user, "TEAM-CARD-PAUSED", 1, None, cursor), 422)
    other = {"customer_id": "TEAM-CUSTOMER-OTHER"}
    status(lambda: store.movements(other, "TEAM-CARD-ACTIVE", 1, None, cursor), 422)
    parsed = MovementCursor.decode(cursor)
    for value in (
        "!broken",
        "a" * 2049,
        "",
        "e30",
        parsed.model_copy(update={"customer_id": "other"}).encode(),
    ):
        status(lambda value=value: store.movements(user, "TEAM-CARD-ACTIVE", 1, None, value), 422)
    with store.connect() as pg:
        pg.execute(
            "INSERT INTO bank.releases VALUES ('new-release',%s)",
            (Jsonb({"contract_version": "card-support-etl-v1"}),),
        )
        pg.execute("UPDATE bank.current_release SET release_id='new-release'")
    status(lambda: store.movements(user, "TEAM-CARD-ACTIVE", 1, None, cursor), 409)


def test_empty_page_has_no_continuation(regression_store):
    page = regression_store.movements(
        {"customer_id": "TEAM-CUSTOMER-001"}, "TEAM-CARD-ACTIVE", 1, None
    )
    assert page["movements"] == []
    assert page["next_cursor"] is None


def test_stale_cursor_returns_conflict_when_new_release_removes_organizer_card(regression_store):
    store = regression_store
    seed_movements(store)
    with store.connect() as pg:
        pg.execute(
            "INSERT INTO bank.products VALUES ('review-release','TEAM-ORGANIZER-CARD',"
            "'TEAM-CUSTOMER-001','Tarjeta Crédito','TEAM-TEST-7777','USD',0,1000,'Active',now())"
        )
        pg.execute("UPDATE bank.transactions SET product_id='TEAM-ORGANIZER-CARD'")
    token = store.login("demo", "old-team-password", "test")["access_token"]
    with TestClient(create_app(store.settings, store=store)) as client:
        headers = {"Authorization": "Bearer " + token}
        path = "/me/cards/TEAM-ORGANIZER-CARD/movements"
        first = client.get(path, params={"limit": 1}, headers=headers)
        assert first.status_code == 200
        cursor = first.json()["next_cursor"]
        with store.connect() as pg:
            pg.execute(
                "INSERT INTO bank.releases VALUES ('card-removed-release',%s)",
                (Jsonb({"contract_version": "card-support-etl-v1"}),),
            )
            pg.execute("UPDATE bank.current_release SET release_id='card-removed-release'")
        assert client.get(path, params={"cursor": cursor}, headers=headers).status_code == 409
        assert client.get(path, headers=headers).status_code == 404
        assert client.get(path, params={"cursor": cursor}).status_code == 401
        # A different trusted principal cannot use the former owner's cursor.
        status(
            lambda: store.movements(
                {"customer_id": "TEAM-CUSTOMER-OTHER"}, "TEAM-ORGANIZER-CARD", 1, None, cursor
            ),
            422,
        )


def test_invalid_rotation_is_atomic(regression_store):
    store = regression_store
    token = store.login("demo", "old-team-password", "test")["access_token"]
    store.settings.demo_password_file.write_text("too-short")
    with pytest.raises(ValueError, match="invalid_demo_secret"):
        store.initialize()
    assert store.session(token)["username"] == "demo"
    assert store.login("demo", "old-team-password", "test")["mode"] == "test_simulator"


def test_rotation_serializes_with_an_inflight_login(regression_store, monkeypatch):
    from threading import Event

    from factored_bck import store as module

    store = regression_store
    original = module.password_matches
    entered, release = Event(), Event()

    def verify(password, stored):
        if password == "old-team-password":
            entered.set()
            assert release.wait(5)
        return original(password, stored)

    monkeypatch.setattr(module, "password_matches", verify)
    with ThreadPoolExecutor(max_workers=2) as pool:
        login = pool.submit(store.login, "demo", "old-team-password", "test")
        assert entered.wait(5)
        store.settings.demo_password_file.write_text("new-team-password")
        rotation = pool.submit(store.initialize)
        release.set()
        token = login.result(timeout=5)["access_token"]
        rotation.result(timeout=5)
    status(lambda: store.session(token), 401)
    status(lambda: store.login("demo", "old-team-password", "test"), 401)
    assert store.login("demo", "new-team-password", "test")["mode"] == "test_simulator"
