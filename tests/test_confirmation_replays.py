"""Historical committed confirmation receipts survive source cutover with fresh auth."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import HTTPException

from factored_bck.confirmations import CardCommand, Confirmations


def confirmed(tool_backend):
    store, _, context = tool_backend
    with store.connect() as pg:
        pg.execute("DELETE FROM simulator.fixture_cards WHERE product_id='card-1'")
        pg.execute(
            "INSERT INTO bank.products VALUES('test-release','card-1','customer-1',"
            "'Tarjeta Crédito','TEAM-1234','USD',0,1000,'Active','2026-01-01')"
        )
    service = Confirmations(store)
    token = context.session_token.get_secret_value()
    item = service.prepare(token, CardCommand(product_id="card-1", action="pause"), "replay")
    return store, service, token, service.confirm(token, item["confirmation_id"])


@pytest.mark.parametrize("change", ["removed", "reassigned"])
def test_committed_receipt_replay_survives_current_source_ownership_change(tool_backend, change):
    store, service, token, original = confirmed(tool_backend)
    with store.connect() as pg:
        pg.execute("SELECT pg_advisory_xact_lock(7236148201)")
        pg.execute("INSERT INTO bank.releases SELECT 'next-release',manifest FROM bank.releases")
        if change == "reassigned":
            pg.execute(
                "INSERT INTO bank.products SELECT 'next-release',product_id,'customer-2',"
                "product_type,product_number,currency,current_balance,credit_limit,"
                "product_status,last_updated FROM bank.products WHERE product_id='card-1'"
            )
        pg.execute("UPDATE bank.current_release SET release_id='next-release'")
    assert service.confirm(token, original["confirmation_id"]) == original
    assert store.action_metrics()["total_committed"] == 1
    other = store.login("user-2", "test-password", "local")["access_token"]
    with pytest.raises(HTTPException) as error:
        service.confirm(other, original["confirmation_id"])
    assert error.value.status_code == 404


@pytest.mark.parametrize("revocation", ["logout", "expiry"])
def test_committed_receipt_replay_still_requires_fresh_session(tool_backend, revocation):
    store, service, token, original = confirmed(tool_backend)
    if revocation == "logout":
        store.logout(token)
    else:
        with store.connect() as pg:
            pg.execute("UPDATE simulator.sessions SET expires_at='2000-01-01'")
    with pytest.raises(HTTPException) as error:
        service.confirm(token, original["confirmation_id"])
    assert error.value.status_code == 401
    assert store.action_metrics()["total_committed"] == 1


def test_committed_replay_reauthenticates_after_waiting_for_customer_lock(
    tool_backend, monkeypatch
):
    store, service, token, original = confirmed(tool_backend)
    session = store.session
    ready, proceed = Event(), Event()

    def paused(value, **kwargs):
        identity = session(value, **kwargs)
        if not kwargs:
            ready.set()
            assert proceed.wait(5)
        return identity

    monkeypatch.setattr(store, "session", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.confirm, token, original["confirmation_id"])
        try:
            assert ready.wait(5)
            store.logout(token)
        finally:
            proceed.set()
        with pytest.raises(HTTPException) as error:
            future.result(timeout=5)
    assert error.value.status_code == 401
