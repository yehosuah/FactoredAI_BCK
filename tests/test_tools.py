"""The dispatcher interface rejects unsafe calls before crossing the Store seam."""

import json
from datetime import date
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from factored_bck.app import create_app
from factored_bck.evidence import verified_action_evidence
from factored_bck.settings import Settings
from factored_bck.tools import ExecutionContext, ToolDispatcher

TOKEN = "private-session-secret"
CONTEXT = ExecutionContext(session_token=TOKEN)
PRINCIPAL = {"customer_id": "session-customer", "username": "session-user"}
ACTION_NAMES = {
    "block_card": "block",
    "pause_card": "pause",
    "reactivate_card": "reactivate",
    "activate_card": "activate",
    "request_replacement": "replacement",
    "register_unrecognized_charge": "unrecognized-charge",
}


def receipt(action="pause", product_id="card-one"):
    result = {
        "action_id": "a" * 32,
        "product_id": product_id,
        "action": action,
        "status": "succeeded",
        "outcome": "state_change_verified",
        "simulator_state": "PAUSED",
        "simulated": True,
        "source_kind": "team_synthetic",
        "release_id": "release-one",
    }
    if action in ("replacement", "unrecognized-charge"):
        result["outcome"] = (
            "replacement_request_registered"
            if action == "replacement"
            else "request_registered_for_human_review"
        )
        result["request_id"] = "b" * 32
    return result


@pytest.fixture
def backend():
    store = Mock()
    store.session.return_value = PRINCIPAL
    store.action.return_value = receipt()
    return store


@pytest.mark.parametrize("name", ["get_cards", "create_handoff", "get_handoff"])
@pytest.mark.parametrize("context", [None, {}, PRINCIPAL, {"session_token": TOKEN}])
def test_untrusted_or_missing_context_is_rejected(backend, context, name):
    result = ToolDispatcher(backend).execute(name, {}, context=context)
    assert result["error"]["code"] == "unauthenticated"
    backend.session.assert_not_called()
    backend.cards.assert_not_called()


def test_context_cannot_supply_customer_identity_and_hides_credential():
    with pytest.raises(ValidationError):
        ExecutionContext(session_token=TOKEN, customer_id="forged")
    assert TOKEN not in repr(CONTEXT)
    assert CONTEXT.model_dump() == {}
    assert TOKEN not in CONTEXT.model_dump_json()


@pytest.mark.parametrize("tool", ["get_cards", "get_card", "get_movements", *ACTION_NAMES])
def test_customer_override_always_rejected(backend, tool):
    result = ToolDispatcher(backend).execute(tool, {"customer_id": "forged"}, context=CONTEXT)
    assert result["error"]["code"] == "invalid_arguments"
    backend.cards.assert_not_called()
    backend.card.assert_not_called()
    backend.movements.assert_not_called()
    backend.action.assert_not_called()


@pytest.mark.parametrize(
    "name", ["action", "login", "handoff", "__dict__", "connect", "refund", TOKEN, [], None]
)
def test_only_catalog_names_can_execute(backend, name, caplog):
    result = ToolDispatcher(backend).execute(name, {}, context=CONTEXT)
    assert result["error"]["code"] == "invalid_tool"
    assert backend.method_calls == [("session", (TOKEN,), {})]
    assert TOKEN not in json.dumps(result) + caplog.text


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        [],
        {"product_id": 123},
        {"product_id": " "},
        {"product_id": "x" * 101},
        {"product_id": "card-one", "limit": True},
        {"product_id": "card-one", "limit": "10"},
        {"product_id": "card-one", "limit": 101},
        {"product_id": "card-one", "limit": 0},
        {"product_id": "card-one", "before_date": "2026-02-30"},
        {"product_id": "card-one", "before_date": "20260101"},
        {"product_id": "card-one", "session_token": TOKEN},
    ],
)
def test_strict_bounded_read_arguments(backend, arguments):
    result = ToolDispatcher(backend).execute("get_movements", arguments, context=CONTEXT)
    assert result["error"]["code"] == "invalid_arguments"
    backend.movements.assert_not_called()
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize(
    "arguments",
    [
        {"product_id": "card-one"},
        {"product_id": "card-one", "idempotency_key": ""},
        {"product_id": "card-one", "idempotency_key": "x" * 101},
        {"product_id": "card-one", "idempotency_key": "bad key"},
        {"product_id": "card-one", "idempotency_key": "key", "action": "refund"},
        {"product_id": "card-one", "idempotency_key": "key", "transaction_id": "tx"},
    ],
)
def test_action_arguments_cannot_select_arbitrary_actions(backend, arguments):
    result = ToolDispatcher(backend).execute("pause_card", arguments, context=CONTEXT)
    assert result["error"]["code"] == "invalid_arguments"
    backend.action.assert_not_called()


def test_reads_get_scope_only_from_fresh_session(backend):
    dispatcher = ToolDispatcher(backend)
    backend.cards.return_value = {"cards": []}
    backend.card.return_value = {"card": {"product_id": "card-one"}}
    backend.movements.return_value = {"movements": []}
    assert dispatcher.execute("get_cards", {}, context=CONTEXT)["data"] == {"cards": []}
    dispatcher.execute("get_card", {"product_id": "card-one"}, context=CONTEXT)
    dispatcher.execute(
        "get_movements", {"product_id": "card-one", "before_date": "2026-01-02"}, context=CONTEXT
    )
    backend.cards.assert_called_once_with(PRINCIPAL)
    backend.card.assert_called_once_with(PRINCIPAL, "card-one")
    backend.movements.assert_called_once_with(PRINCIPAL, "card-one", 50, date(2026, 1, 2))
    assert backend.session.call_count == 3


@pytest.mark.parametrize("tool,action", ACTION_NAMES.items())
def test_actions_only_prepare_fixed_command(backend, tool, action):
    confirmations = Mock()
    confirmations.prepare.return_value = {"status": "pending", "verified": False}
    arguments = {"product_id": "card-one", "idempotency_key": "stable-key"}
    if tool == "register_unrecognized_charge":
        arguments.update(transaction_id="tx-one", process_date="2026-01-02")
    result = ToolDispatcher(backend, confirmations=confirmations).execute(
        tool, arguments, context=CONTEXT
    )
    assert result == {"ok": True, "data": {"status": "pending", "verified": False}}
    backend.action.assert_not_called()
    token, command, key = confirmations.prepare.call_args.args
    assert token == TOKEN and key == "stable-key"
    assert command.action == action and command.product_id == "card-one"
    assert command.process_date == ("2026-01-02" if action == "unrecognized-charge" else None)


@pytest.mark.parametrize(
    "change",
    [
        {"status": "requested"},
        {"simulated": False},
        {"simulated": 1},
        {"action": "block"},
        {"product_id": "other-card"},
        {"action_id": ""},
        {"outcome": "replacement_request_registered"},
    ],
)
def test_invalid_evidence_is_never_reported_as_success(backend, change):
    with pytest.raises(ValueError):
        verified_action_evidence("pause", "card-one", "state_change_verified", receipt() | change)


def test_requested_intent_is_not_evidence_and_unexpected_metadata_is_removed(backend):
    with pytest.raises(ValueError):
        verified_action_evidence(
            "pause", "card-one", "state_change_verified", {"status": "succeeded"}
        )
    result = verified_action_evidence(
        "pause", "card-one", "state_change_verified", receipt() | {"access_token": TOKEN}
    )
    assert TOKEN not in json.dumps(result)
    with pytest.raises(ValueError):
        verified_action_evidence(
            "replacement",
            "card-one",
            "replacement_request_registered",
            receipt("replacement") | {"request_id": None},
        )


@pytest.mark.parametrize(
    "failure,code",
    [
        (HTTPException(401, TOKEN), "unauthenticated"),
        (HTTPException(404, TOKEN), "not_found"),
        (HTTPException(409, TOKEN), "conflict"),
        (HTTPException(503, TOKEN), "unavailable"),
        (RuntimeError(TOKEN), "backend_error"),
    ],
)
def test_failures_never_become_fake_success_or_leak_secrets(backend, failure, code, caplog):
    confirmations = Mock()
    confirmations.prepare.side_effect = failure
    result = ToolDispatcher(backend, confirmations=confirmations).execute(
        "pause_card",
        {"product_id": "card-one", "idempotency_key": "key"},
        context=CONTEXT,
    )
    assert result == {"ok": False, "error": {"code": code, "message": result["error"]["message"]}}
    assert TOKEN not in json.dumps(result) + caplog.text


def test_revoked_session_is_rechecked_before_any_tool(backend):
    backend.session.side_effect = HTTPException(401)
    result = ToolDispatcher(backend).execute("get_cards", {}, context=CONTEXT)
    assert result["error"]["code"] == "unauthenticated"
    backend.cards.assert_not_called()


def test_catalog_is_safe_and_cannot_register_tools(backend):
    dispatcher = ToolDispatcher(backend)
    catalog = dispatcher.catalog()
    assert {item["name"] for item in catalog} == {
        "get_cards",
        "get_card",
        "get_movements",
        "create_handoff",
        "get_handoff",
        *ACTION_NAMES,
    }
    for item in catalog:
        assert item["input_schema"]["additionalProperties"] is False
    assert "customer_id" not in json.dumps(catalog)
    assert "session_token" not in json.dumps(catalog)
    catalog[0]["name"] = "refund"
    catalog[0]["input_schema"]["additionalProperties"] = True
    assert dispatcher.catalog()[0]["name"] == "create_handoff"
    assert dispatcher.catalog()[0]["input_schema"]["additionalProperties"] is False


def test_app_wires_internal_dispatcher_without_public_endpoint(backend):
    app = create_app(Settings(_env_file=None), store=backend)
    assert isinstance(app.state.tools, ToolDispatcher)
    with TestClient(app) as client:
        assert client.post("/tools/execute").status_code == 404
        assert not any("tools" in p for p in client.get("/openapi.json").json()["paths"])
    assert create_app(Settings(_env_file=None)).state.tools is None
