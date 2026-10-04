"""Match the accepted ETL publisher's exclusive lock without source-write privileges."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import HTTPException

from factored_bck.confirmations import CardCommand, Confirmations

# Merged ETL 646bfd9 src/factored_bank/etl/load.py: publication transaction lock.
PUBLISH_LOCK = 7236148201


def test_etl_cannot_accept_removed_product_before_confirmation_commits(tool_backend, monkeypatch):
    store, _, context = tool_backend
    with store.connect() as pg:
        pg.execute("DELETE FROM simulator.fixture_cards WHERE product_id='card-1'")
        pg.execute(
            "INSERT INTO bank.products VALUES('test-release','card-1','customer-1',"
            "'Tarjeta Crédito','TEAM-1234','USD',0,1000,'Active','2026-01-01')"
        )
    token = context.session_token.get_secret_value()
    confirmations = Confirmations(store)
    pending = confirmations.prepare(token, CardCommand(product_id="card-1", action="pause"), "race")
    validated, proceed = Event(), Event()
    preview = store.preview_action

    def pause_after_validation(*args, **kwargs):
        result = preview(*args, **kwargs)
        validated.set()
        assert proceed.wait(5)
        return result

    monkeypatch.setattr(store, "preview_action", pause_after_validation)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(confirmations.confirm, token, pending["confirmation_id"])
        try:
            assert validated.wait(5)
            with store.connect() as publisher:
                admitted = publisher.execute(
                    "SELECT pg_try_advisory_xact_lock(%s) AS admitted", (PUBLISH_LOCK,)
                ).fetchone()["admitted"]
                if admitted:
                    publisher.execute(
                        "INSERT INTO bank.releases SELECT 'removed-release',manifest "
                        "FROM bank.releases"
                    )
                    publisher.execute(
                        "UPDATE bank.current_release SET release_id='removed-release'"
                    )
            assert admitted is False, "ETL cutover overtook a confirmation before its commit"
        finally:
            proceed.set()
        receipt = future.result(timeout=5)
    assert receipt["verified"] and receipt["evidence"]["release_id"] == "test-release"
    # Once confirmation committed, the publisher can advance; a new pending command goes stale.
    monkeypatch.setattr(store, "preview_action", preview)
    next_item = confirmations.prepare(
        token, CardCommand(product_id="card-1", action="reactivate"), "next"
    )
    with store.connect() as publisher:
        assert publisher.execute(
            "SELECT pg_try_advisory_xact_lock(%s) AS admitted", (PUBLISH_LOCK,)
        ).fetchone()["admitted"]
        publisher.execute(
            "INSERT INTO bank.releases SELECT 'removed-release',manifest FROM bank.releases"
        )
        publisher.execute("UPDATE bank.current_release SET release_id='removed-release'")
    with pytest.raises(HTTPException) as error:
        confirmations.confirm(token, next_item["confirmation_id"])
    assert error.value.status_code == 409
    assert confirmations.get(token, next_item["confirmation_id"])["status"] == "stale"
    assert store.action_metrics()["total_committed"] == 1


def test_publishing_rejects_confirmation_without_waiting_or_writing(tool_backend):
    store, _, context = tool_backend
    service = Confirmations(store)
    token = context.session_token.get_secret_value()
    item = service.prepare(token, CardCommand(product_id="card-1", action="pause"), "busy")
    with store.connect() as publisher:
        publisher.execute("SELECT pg_advisory_xact_lock(%s)", (PUBLISH_LOCK,))
        with pytest.raises(HTTPException) as error:
            service.confirm(token, item["confirmation_id"])
        assert error.value.status_code == 503 and error.value.headers == {"Retry-After": "1"}
    assert service.get(token, item["confirmation_id"])["status"] == "pending"
    assert store.action_metrics()["total_committed"] == 0


def test_pin_requires_only_existing_source_select_privileges(tool_backend):
    store, _, _ = tool_backend
    with store.connect() as pg:
        pg.execute("CREATE ROLE read_only_release LOGIN")
        pg.execute("GRANT USAGE ON SCHEMA bank TO read_only_release")
        pg.execute("GRANT SELECT ON bank.current_release,bank.releases TO read_only_release")
        pg.execute("SET LOCAL ROLE read_only_release")
        assert store._current(pg, pin=True) == "test-release"
        assert not pg.execute(
            "SELECT has_table_privilege(current_user,'bank.current_release','UPDATE') AS can_write"
        ).fetchone()["can_write"]
