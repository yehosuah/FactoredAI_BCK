"""Tool security and evidence through the real Store on disposable PostgreSQL."""

import json
from datetime import date

import pytest

from factored_bck.security import password_hash
from factored_bck.tools import ExecutionContext, ToolDispatcher


@pytest.fixture
def tool_backend(store):
    with store.connect() as pg:
        pg.execute(
            "CREATE TABLE bank.products (release_id text, product_id text, customer_id text, "
            "product_type text, product_number text, currency text, current_balance numeric, "
            "credit_limit numeric, product_status text, last_updated timestamp)"
        )
        pg.execute(
            "CREATE TABLE bank.transactions (release_id text, customer_id text, product_id text, "
            "transaction_id text, transaction_date date, process_date date, amount numeric, "
            "currency text, transaction_type text, transaction_status text, merchant_name text)"
        )
        for number in (1, 2):
            pg.execute(
                "INSERT INTO simulator.users VALUES(%s,%s,%s,'team_synthetic')",
                (f"user-{number}", password_hash("test-password"), f"customer-{number}"),
            )
            pg.execute(
                "INSERT INTO simulator.fixture_cards VALUES "
                "(%s,%s,'Tarjeta Crédito','TEAM-1234','USD',100,1000,'ACTIVE','team_synthetic')",
                (f"card-{number}", f"customer-{number}"),
            )
            pg.execute(
                "INSERT INTO simulator.card_states(product_id,customer_id,state) "
                "VALUES(%s,%s,'ACTIVE')",
                (f"card-{number}", f"customer-{number}"),
            )
            pg.execute(
                "INSERT INTO bank.transactions VALUES ('test-release',%s,%s,%s,"
                "'2026-01-01','2026-01-02',10,'USD','purchase','posted','Fixture Merchant')",
                (f"customer-{number}", f"card-{number}", f"tx-{number}"),
            )
    token = store.login("user-1", "test-password", "local-test")["access_token"]
    return store, ToolDispatcher(store), ExecutionContext(session_token=token)


def test_real_reads_preserve_customer_scope_and_historical_semantics(tool_backend):
    _, dispatcher, context = tool_backend
    cards = dispatcher.execute("get_cards", {}, context=context)
    assert cards["ok"]
    assert [card["product_id"] for card in cards["data"]["cards"]] == ["card-1"]
    assert cards["data"]["release_id"] == "test-release"
    card = dispatcher.execute("get_card", {"product_id": "card-1"}, context=context)
    assert card["data"]["card"]["last_four"] == "1234"
    assert "product_number" not in card["data"]["card"]
    movements = dispatcher.execute(
        "get_movements", {"product_id": "card-1", "limit": 1}, context=context
    )
    assert movements["data"]["semantics"] == "historical_source_movements"
    assert [tx["transaction_id"] for tx in movements["data"]["movements"]] == ["tx-1"]
    filtered = dispatcher.execute(
        "get_movements",
        {"product_id": "card-1", "before_date": "2026-01-02"},
        context=context,
    )
    assert filtered["data"]["movements"] == []


@pytest.mark.parametrize("tool", ["get_card", "get_movements", "block_card"])
def test_cross_customer_access_is_rejected_by_real_store(tool_backend, tool):
    store, dispatcher, context = tool_backend
    arguments = {"product_id": "card-2"}
    if tool == "block_card":
        arguments["idempotency_key"] = "key"
    result = dispatcher.execute(tool, arguments, context=context)
    assert result["error"]["code"] == "not_found"
    assert store.action_metrics()["total_committed"] == 0
    with store.connect() as pg:
        assert (
            pg.execute(
                "SELECT state FROM simulator.card_states WHERE product_id='card-2'"
            ).fetchone()["state"]
            == "ACTIVE"
        )


@pytest.mark.parametrize(
    "tool,state,outcome",
    [
        ("block_card", "ACTIVE", "state_change_verified"),
        ("pause_card", "ACTIVE", "state_change_verified"),
        ("reactivate_card", "PAUSED", "state_change_verified"),
        ("activate_card", "PENDING_ACTIVATION", "state_change_verified"),
        ("request_replacement", "ACTIVE", "replacement_request_registered"),
        ("register_unrecognized_charge", "ACTIVE", "request_registered_for_human_review"),
    ],
)
def test_verified_evidence_is_the_committed_record_and_replay_is_identical(
    tool_backend, tool, state, outcome
):
    store, dispatcher, context = tool_backend
    with store.connect() as pg:
        pg.execute("UPDATE simulator.card_states SET state=%s WHERE product_id='card-1'", (state,))
    arguments = {"product_id": "card-1", "idempotency_key": "stable-key"}
    if tool == "register_unrecognized_charge":
        arguments.update(transaction_id="tx-1", process_date="2026-01-02")
    result = dispatcher.execute(tool, arguments, context=context)
    assert result["ok"] and result["data"]["verified"]
    assert result["data"]["evidence"]["outcome"] == outcome
    assert result["data"]["evidence"]["simulated"] is True
    with store.connect() as pg:
        evidence = pg.execute("SELECT result FROM simulator.actions").fetchall()
    assert evidence == [{"result": result["data"]["evidence"]}]
    assert dispatcher.execute(tool, arguments, context=context) == result
    assert store.action_metrics()["total_committed"] == 1


def test_cross_transport_replay_and_conflicting_reuse(tool_backend):
    store, dispatcher, context = tool_backend
    # Existing HTTP routes call this exact Store interface: evidence must be shared.
    principal = store.session(context.session_token.get_secret_value())
    evidence = store.action(principal, "card-1", "pause", "same-key")
    arguments = {"product_id": "card-1", "idempotency_key": "same-key"}
    assert (
        dispatcher.execute("pause_card", arguments, context=context)["data"]["evidence"] == evidence
    )
    result = dispatcher.execute("block_card", arguments, context=context)
    assert result["error"]["code"] == "conflict"
    assert store.action_metrics()["total_committed"] == 1


def test_ineligible_action_and_other_customers_charge_never_succeed(tool_backend):
    store, dispatcher, context = tool_backend
    result = dispatcher.execute(
        "activate_card", {"product_id": "card-1", "idempotency_key": "key-1"}, context=context
    )
    assert result["error"]["code"] == "conflict"
    result = dispatcher.execute(
        "register_unrecognized_charge",
        {
            "product_id": "card-1",
            "idempotency_key": "key-2",
            "transaction_id": "tx-2",
            "process_date": "2026-01-02",
        },
        context=context,
    )
    assert result["error"]["code"] == "not_found"
    assert store.action_metrics()["total_committed"] == 0


@pytest.mark.parametrize("revocation", ["logout", "expiry"])
def test_real_session_revocation_is_checked_even_for_replays(tool_backend, revocation, caplog):
    store, dispatcher, context = tool_backend
    arguments = {"product_id": "card-1", "idempotency_key": "key"}
    assert dispatcher.execute("pause_card", arguments, context=context)["ok"]
    token = context.session_token.get_secret_value()
    if revocation == "logout":
        store.logout(token)
    else:
        with store.connect() as pg:
            pg.execute("UPDATE simulator.sessions SET expires_at=%s", (date(2000, 1, 1),))
    result = dispatcher.execute("pause_card", arguments, context=context)
    assert result["error"]["code"] == "unauthenticated"
    assert token not in json.dumps(result) + caplog.text
    assert store.action_metrics()["total_committed"] == 1
