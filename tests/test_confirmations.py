"""Atomic customer confirmation through the real Store, HTTP, and closed tool registry."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from factored_bck.app import create_app
from factored_bck.confirmations import CardCommand, Confirmations


def pending(backend, key="prepare-one", **changes):
    store, _, context = backend
    service = Confirmations(store)
    command = CardCommand.model_validate({"product_id": "card-1", "action": "pause", **changes})
    token = context.session_token.get_secret_value()
    return service, token, service.prepare(token, command, key)


def count(store):
    return store.action_metrics()["total_committed"]


def status(service, token, item):
    return service.get(token, item["confirmation_id"])["status"]


def test_prepare_exact_owned_command_no_action_and_idempotent_replay(tool_backend):
    store, dispatcher, context = tool_backend
    service, token, item = pending(tool_backend)
    assert item["status"] == "pending" and item["confirmation_required"]
    assert item["verified"] is False and "evidence" not in item
    assert item["command"] == {
        "product_id": "card-1",
        "action": "pause",
        "transaction_id": None,
        "process_date": None,
    }
    assert item["prepared_state"]["state"] == "ACTIVE"
    assert item["created_at"] < item["expires_at"] and count(store) == 0
    assert service.prepare(token, CardCommand(**item["command"]), "prepare-one") == item
    with pytest.raises(HTTPException) as conflict:
        service.prepare(token, CardCommand(product_id="card-1", action="block"), "prepare-one")
    assert conflict.value.status_code == 409
    with store.connect() as pg:
        row = pg.execute("SELECT * FROM simulator.action_confirmations").fetchone()
    assert row["customer_id"] == "customer-1"
    assert row["command_key"] == "confirmation:" + item["confirmation_id"]
    assert row["conversation_id"] is None
    assert token not in json.dumps(item)


def test_confirmation_executes_stored_command_and_replays_after_commit(tool_backend):
    store, _, _ = tool_backend
    service, token, item = pending(tool_backend)
    confirmed = service.confirm(token, item["confirmation_id"])
    assert confirmed["status"] == "executed" and confirmed["verified"]
    assert confirmed["evidence"]["action"] == "pause"
    assert confirmed["evidence"]["product_id"] == "card-1"
    assert confirmed["evidence"]["simulator_state"] == "PAUSED"
    assert Confirmations(store).confirm(token, item["confirmation_id"]) == confirmed
    assert count(store) == 1
    with store.connect() as pg:
        saved = pg.execute(
            "SELECT command_key,action_id,evidence FROM simulator.action_confirmations"
        ).fetchone()
        action = pg.execute(
            "SELECT idempotency_key,payload,result FROM simulator.actions"
        ).fetchone()
    assert saved["action_id"] == confirmed["evidence"]["action_id"]
    assert action["idempotency_key"] == saved["command_key"]
    assert action["payload"] == item["command"]
    assert action["result"] == saved["evidence"] == confirmed["evidence"]


@pytest.mark.parametrize("operation", ["get", "confirm", "cancel"])
def test_other_customer_and_forged_id_safe(tool_backend, operation):
    store, _, _ = tool_backend
    service, token, item = pending(tool_backend)
    other = store.login("user-2", "test-password", "local")["access_token"]
    method = getattr(service, operation)
    for credential, identifier in [(other, item["confirmation_id"]), (token, "f" * 32)]:
        with pytest.raises(HTTPException) as error:
            method(credential, identifier)
        assert error.value.status_code == 404
    assert count(store) == 0 and status(service, token, item) == "pending"


@pytest.mark.parametrize("revocation", ["logout", "expiry"])
def test_revoked_or_expired_session_prevents_execution(tool_backend, revocation):
    store, _, _ = tool_backend
    service, token, item = pending(tool_backend)
    if revocation == "logout":
        store.logout(token)
    else:
        with store.connect() as pg:
            pg.execute("UPDATE simulator.sessions SET expires_at='2000-01-01'")
    with pytest.raises(HTTPException) as error:
        service.confirm(token, item["confirmation_id"])
    assert error.value.status_code == 401 and count(store) == 0


def test_cancel_prevents_execution_and_is_idempotent(tool_backend):
    store, _, _ = tool_backend
    service, token, item = pending(tool_backend)
    cancelled = service.cancel(token, item["confirmation_id"])
    assert cancelled["status"] == "cancelled" and not cancelled["verified"]
    assert service.cancel(token, item["confirmation_id"]) == cancelled
    with pytest.raises(HTTPException) as error:
        service.confirm(token, item["confirmation_id"])
    assert error.value.status_code == 409 and count(store) == 0
    assert service.prepare(token, CardCommand(**item["command"]), "prepare-one") == cancelled


def test_confirmation_vs_cancel_and_duplicate_confirmation_concurrency(tool_backend):
    store, _, _ = tool_backend
    service, token, item = pending(tool_backend)

    def operate(operation):
        try:
            return getattr(service, operation)(token, item["confirmation_id"])["status"]
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(operate, ["confirm", "cancel"]))
    assert 409 in outcomes and len(set(outcomes)) == 2
    winner = status(service, token, item)
    assert winner in ("executed", "cancelled")
    assert count(store) == (1 if winner == "executed" else 0)
    # Another card/request registration can be safely confirmed by many concurrent retries.
    next_item = service.prepare(
        token, CardCommand(product_id="card-1", action="replacement"), "next"
    )
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(lambda _: service.confirm(token, next_item["confirmation_id"]), range(12))
        )
    assert all(r == results[0] for r in results)
    assert count(store) == (2 if winner == "executed" else 1)


def test_concurrent_prepare_does_not_duplicate_or_extend_expiry(tool_backend):
    store, _, context = tool_backend
    service, token = Confirmations(store), context.session_token.get_secret_value()
    command = CardCommand(product_id="card-1", action="pause")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: service.prepare(token, command, "same-key"), range(12)))
    assert all(r == results[0] for r in results) and count(store) == 0
    with store.connect() as pg:
        assert (
            pg.execute("SELECT count(*) FROM simulator.action_confirmations").fetchone()["count"]
            == 1
        )


def test_expiry_persists_and_cannot_be_extended_by_replay(tool_backend):
    store, _, _ = tool_backend
    service, token, item = pending(tool_backend)
    with store.connect() as pg:
        pg.execute(
            "UPDATE simulator.action_confirmations SET "
            "created_at=clock_timestamp()-interval '5 minutes', "
            "expires_at=clock_timestamp()-interval '1 second'"
        )
    with pytest.raises(HTTPException) as error:
        service.confirm(token, item["confirmation_id"])
    assert error.value.status_code == 409
    assert status(service, token, item) == "expired"
    assert (
        service.prepare(token, CardCommand(**item["command"]), "prepare-one")["status"] == "expired"
    )
    assert count(store) == 0


@pytest.mark.parametrize("change", ["state", "aba", "ownership", "source_release", "policy"])
def test_changed_state_ownership_release_or_policy_is_stale(tool_backend, change):
    store, _, _ = tool_backend
    service, token, item = pending(tool_backend)
    principal = store.session(token)
    if change in ("state", "aba"):
        store.action(principal, "card-1", "pause", "other-action")
        if change == "aba":
            store.action(principal, "card-1", "reactivate", "back-to-active")
    else:
        with store.connect() as pg:
            if change == "ownership":
                pg.execute(
                    "UPDATE simulator.fixture_cards SET customer_id='customer-2' WHERE "
                    "product_id='card-1'"
                )
            elif change == "source_release":
                pg.execute(
                    "INSERT INTO bank.releases SELECT 'new-release',manifest FROM bank.releases"
                )
                pg.execute("UPDATE bank.current_release SET release_id='new-release'")
            else:
                pg.execute("UPDATE simulator.action_confirmations SET policy_version='old-policy'")
    before = count(store)
    with pytest.raises(HTTPException) as error:
        service.confirm(token, item["confirmation_id"])
    assert error.value.status_code == 409
    assert status(service, token, item) == "stale" and count(store) == before
    with pytest.raises(HTTPException):
        service.confirm(token, item["confirmation_id"])


def test_charge_parameters_and_transaction_ownership_rechecked(tool_backend):
    store, _, _ = tool_backend
    service, token, item = pending(
        tool_backend, action="unrecognized-charge", transaction_id="tx-1", process_date="2026-01-02"
    )
    with store.connect() as pg:
        pg.execute(
            "UPDATE bank.transactions SET customer_id='customer-2' WHERE transaction_id='tx-1'"
        )
    with pytest.raises(HTTPException):
        service.confirm(token, item["confirmation_id"])
    assert status(service, token, item) == "stale" and count(store) == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("confirmed", True),
        ("user_confirmed", True),
        ("customer_id", "customer-2"),
        ("action", "block"),
        ("product_id", "card-2"),
        ("transaction_id", "tx-2"),
        ("process_date", "2026-01-02"),
        ("model_text", "yes"),
        ("conversation_id", "forged"),
        ("idempotency_key", "replacement-key"),
    ],
)
def test_confirmation_body_cannot_replace_command_or_authority(tool_backend, field, value):
    store, _, _ = tool_backend
    service, token, item = pending(tool_backend)
    with TestClient(create_app(store.settings, store=store)) as client:
        response = client.post(
            "/me/action-confirmations/" + item["confirmation_id"] + "/confirm",
            json={field: value},
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 422
    assert status(service, token, item) == "pending" and count(store) == 0


def test_http_and_tool_bypass_protection_and_unchanged_reads(tool_backend):
    store, dispatcher, context = tool_backend
    token = context.session_token.get_secret_value()
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "http"}
    with TestClient(create_app(store.settings, store=store)) as client:
        item = client.post(
            "/me/cards/card-1/actions", headers=headers, json={"action": "pause"}
        ).json()
        assert item["status"] == "pending" and count(store) == 0
        response = client.post(
            "/me/action-confirmations",
            headers=headers,
            json={"action": "pause", "product_id": "card-1"},
        )
        assert response.json() == item
        assert client.get("/me/action-confirmations/" + item["confirmation_id"]).status_code == 401
        assert client.get("/me/action-confirmations/not-an-id", headers=headers).status_code == 422
        assert (
            client.post(
                "/me/action-confirmations",
                headers=headers,
                json={"action": "refund", "product_id": "card-1"},
            ).status_code
            == 422
        )
        for name in ("confirm_action", "confirm", "execute_action", "cancel_confirmation"):
            assert (
                dispatcher.execute(
                    name, {"confirmation_id": item["confirmation_id"]}, context=context
                )["error"]["code"]
                == "invalid_tool"
            )
        for extra in (
            {"confirmed": True},
            {"user_confirmed": True},
            {"confirmation_id": item["confirmation_id"]},
        ):
            result = dispatcher.execute(
                "pause_card",
                {"product_id": "card-1", "idempotency_key": "llm"} | extra,
                context=context,
            )
            assert result["error"]["code"] == "invalid_arguments"
        assert count(store) == 0
        assert (
            dispatcher.execute("get_card", {"product_id": "card-1"}, context=context)["data"][
                "card"
            ]["simulator_state"]
            == "ACTIVE"
        )
        response = client.post(
            "/me/action-confirmations/" + item["confirmation_id"] + "/confirm",
            headers=headers,
            json={},
        )
        assert response.status_code == 200 and response.json()["verified"]
        assert response.headers["cache-control"] == "no-store"
    assert count(store) == 1


@pytest.mark.parametrize("failure", ["forged_evidence", "mismatched_evidence", "commit"])
def test_action_and_confirmation_rollback_together_without_fake_success(
    tool_backend, monkeypatch, failure, caplog
):
    store, _, _ = tool_backend
    service, token, item = pending(tool_backend)
    if failure == "commit":
        with store.connect() as pg:
            pg.execute(
                "CREATE FUNCTION simulator.reject_confirmation() RETURNS trigger LANGUAGE "
                "plpgsql AS $$ BEGIN RAISE EXCEPTION 'private-error'; END $$"
            )
            pg.execute(
                "CREATE CONSTRAINT TRIGGER reject_confirmation AFTER UPDATE ON "
                "simulator.action_confirmations DEFERRABLE INITIALLY DEFERRED FOR EACH "
                "ROW EXECUTE FUNCTION simulator.reject_confirmation()"
            )
    else:
        actual = store.action

        def action(*args, **kwargs):
            receipt = actual(*args, **kwargs)
            if failure == "forged_evidence":
                return {"status": "succeeded", "access_token": token}
            return receipt | {"action_id": "f" * 32}

        monkeypatch.setattr(store, "action", action)
    with TestClient(create_app(store.settings, store=store)) as client:
        response = client.post(
            "/me/action-confirmations/" + item["confirmation_id"] + "/confirm",
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 500 and "evidence" not in response.json()
    assert token not in response.text + caplog.text and "private-error" not in caplog.text
    assert count(store) == 0 and status(service, token, item) == "pending"
    assert store.card(store.session(token), "card-1")["card"]["simulator_state"] == "ACTIVE"


def test_server_owned_conversation_seam_and_no_secret_persistence(tool_backend):
    store, dispatcher, context = tool_backend
    token = context.session_token.get_secret_value()
    service = Confirmations(store)
    command = CardCommand(product_id="card-1", action="pause")
    item = service.prepare(token, command, "key", conversation_id="server-owned-future-id")
    with store.connect() as pg:
        row = pg.execute("SELECT * FROM simulator.action_confirmations").fetchone()
    assert row["conversation_id"] == "server-owned-future-id"
    assert token not in str(row) + json.dumps(item)
    with pytest.raises(HTTPException):
        service.prepare(token, command, token)
    assert "conversation_id" not in json.dumps(dispatcher.catalog())
    with pytest.raises(HTTPException) as error:
        service.prepare(token, command, "key", conversation_id="different-server-scope")
    assert error.value.status_code == 409


def test_two_pending_commands_on_one_card_only_first_is_valid(tool_backend):
    store, _, _ = tool_backend
    service, token, first = pending(tool_backend)
    second = service.prepare(token, CardCommand(product_id="card-1", action="pause"), "second")
    service.confirm(token, first["confirmation_id"])
    with pytest.raises(HTTPException):
        service.confirm(token, second["confirmation_id"])
    assert status(service, token, second) == "stale" and count(store) == 1


@pytest.mark.parametrize("revocation", ["logout", "expiry"])
def test_session_is_revalidated_after_initial_authentication(tool_backend, monkeypatch, revocation):
    from threading import Event

    store, _, _ = tool_backend
    service, token, item = pending(tool_backend)
    authenticated, proceed = Event(), Event()
    actual_session = store.session

    def session(credential, **kwargs):
        result = actual_session(credential, **kwargs)
        if not kwargs:
            authenticated.set()
            assert proceed.wait(5)
        return result

    monkeypatch.setattr(store, "session", session)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.confirm, token, item["confirmation_id"])
        try:
            assert authenticated.wait(5)
            if revocation == "logout":
                store.logout(token)
            else:
                with store.connect() as pg:
                    pg.execute("UPDATE simulator.sessions SET expires_at='2000-01-01'")
        finally:
            proceed.set()
        with pytest.raises(HTTPException) as error:
            future.result(timeout=5)
    assert error.value.status_code == 401 and count(store) == 0
    with store.connect() as pg:
        assert (
            pg.execute("SELECT status FROM simulator.action_confirmations").fetchone()["status"]
            == "pending"
        )
