"""P04 HTTP engineering acceptance on private synthetic PostgreSQL, no real ML."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_handoffs import auth
from test_handoffs import backend as backend

from factored_bck.app import create_app
from factored_bck.conversation_contract import AdapterInfo, SubmitTurn
from factored_bck.ml_adapter import DeterministicStub


@pytest.fixture
def conversation_backend(backend):
    store, handoffs, _, customers, agents = backend
    store.settings.conversation_adapter = "stub"
    with store.connect() as pg:
        pg.execute(
            "INSERT INTO simulator.card_states(product_id,customer_id,state) "
            "SELECT product_id,customer_id,'ACTIVE' FROM simulator.fixture_cards"
        )
        pg.execute(
            "CREATE TABLE bank.transactions(release_id text,customer_id text,product_id text,"
            "transaction_id text,transaction_date date,process_date date,amount numeric,"
            "currency text,transaction_type text,transaction_status text,merchant_name text)"
        )
        pg.execute(
            "INSERT INTO bank.transactions VALUES('test-release','c1','card1','tx1',"
            "'2026-01-01','2026-01-02',10,'USD','purchase','posted','Fixture Merchant')"
        )
    with TestClient(create_app(store.settings, store=store)) as client:
        yield store, handoffs, client, customers, agents


def create(backend, language="es", key="conversation-one", customer=0):
    response = backend[2].post(
        "/me/conversations",
        json={"language": language},
        headers=auth(backend[3][customer]) | {"Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()


def submit(backend, conversation, message="hello", key="turn-one", **changes):
    response = backend[2].post(
        f"/me/conversations/{conversation['conversation_id']}/turns",
        json={"message": message, **changes},
        headers=auth(backend[3][0]) | {"Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()


def get(backend, conversation, **params):
    response = backend[2].get(
        f"/me/conversations/{conversation['conversation_id']}",
        headers=auth(backend[3][0]),
        params=params,
    )
    assert response.status_code == 200, response.text
    return response.json()


def event(state, kind):
    matches = [e for e in state["events"] if e["kind"] == kind]
    assert matches, [
        (e["kind"], e["data"].get("code"), e["data"].get("tool_error")) for e in state["events"]
    ]
    return matches[0]


class ScriptedAdapter:
    info = AdapterInfo(provider="scripted-engineering-test", version="1", mode="stub")

    def __init__(self, output):
        self.output = output
        self.contexts = []

    async def propose(self, context):
        self.contexts.append(context)
        return self.output


def inject(backend, output):
    adapter = ScriptedAdapter(output)
    backend[2].app.state.conversations.adapter = adapter
    return adapter


@pytest.mark.parametrize("language,expected", [("es", "Modo de prueba"), ("pt", "Modo de teste")])
def test_create_reconnect_restart_order_language_and_explicit_stub(
    conversation_backend, language, expected
):
    backend = conversation_backend
    store, _, client, customers, _ = backend
    initial = create(backend, language)
    assert len(initial["conversation_id"]) == 32 and initial["events"] == []
    assert initial["adapter"]["mode"] == "stub" and initial["language"] == language
    assert create(backend, language) == initial
    first = submit(backend, initial)
    second = submit(backend, initial, message="again", key="turn-two")
    answer = event(first, "answer")
    assert expected in answer["data"]["text"]
    assert answer["trust"] == "untrusted" and answer["data"]["verified"] is False
    assert answer["adapter"]["provider"] == "deterministic-engineering-stub"
    state = get(backend, initial)
    assert [e["sequence"] for e in state["events"]] == [1, 2, 3, 4]
    assert len({e["event_id"] for e in state["events"]}) == 4
    assert first["submitted_turn_id"] != second["submitted_turn_id"]
    with TestClient(create_app(store.settings, store=store)) as restarted:
        result = restarted.get(
            f"/me/conversations/{initial['conversation_id']}", headers=auth(customers[0])
        )
    assert result.json() == state
    page = get(backend, initial, limit=2)
    assert page["next_after"] == 2
    assert get(backend, initial, after=page["next_after"])["events"] == state["events"][2:]
    assert (
        client.get(
            f"/me/conversations/{initial['conversation_id']}", headers=auth(customers[0])
        ).headers["cache-control"]
        == "no-store"
    )
    assert store.action_metrics()["total_committed"] == 0


@pytest.mark.parametrize("method", ["get", "post"])
def test_wrong_customer_and_unauthenticated_cannot_read_or_turn(conversation_backend, method):
    backend = conversation_backend
    case = create(backend)
    path = f"/me/conversations/{case['conversation_id']}" + ("/turns" if method == "post" else "")
    kwargs = {"json": {"message": "hello"}} if method == "post" else {}
    for token, status in ((backend[3][1], 404), (backend[4][0], 401), ("invalid", 401)):
        response = getattr(backend[2], method)(
            path, headers=auth(token) | {"Idempotency-Key": "other"}, **kwargs
        )
        assert response.status_code == status
    assert get(backend, case)["events"] == []


@pytest.mark.parametrize("change", ["expired", "revoked"])
def test_expired_revoked_session_blocks_every_conversation_operation(conversation_backend, change):
    backend = conversation_backend
    case = create(backend)
    token = backend[3][0]
    if change == "revoked":
        backend[0].logout(token)
    else:
        with backend[0].connect() as pg:
            pg.execute("UPDATE simulator.sessions SET expires_at='2000-01-01'")
    headers = auth(token) | {"Idempotency-Key": "retry"}
    assert (
        backend[2].post("/me/conversations", json={"language": "es"}, headers=headers).status_code
        == 401
    )
    path = f"/me/conversations/{case['conversation_id']}"
    assert backend[2].get(path, headers=headers).status_code == 401
    assert (
        backend[2].post(path + "/turns", json={"message": "hello"}, headers=headers).status_code
        == 401
    )


def test_language_change_preserves_context_and_idempotent_turns(conversation_backend):
    backend = conversation_backend
    case = create(backend)
    adapter = inject(backend, {"kind": "clarification", "question": "Which card?"})
    original = submit(backend, case, message="first")
    second = submit(backend, case, message="segundo", language="pt", key="two")
    assert second["language"] == "pt"
    assert [m.text for m in adapter.contexts[1].messages] == ["first", "Which card?", "segundo"]
    assert adapter.contexts[1].language == "pt"
    serialized = adapter.contexts[1].model_dump_json()
    assert all(token not in serialized for token in backend[3])
    assert "customer_id" not in serialized and "session" not in serialized
    replay = submit(backend, case, message="first")
    assert replay["submitted_turn_id"] == original["submitted_turn_id"]
    assert len(adapter.contexts) == 2 and len(get(backend, case)["events"]) == 4
    response = backend[2].post(
        f"/me/conversations/{case['conversation_id']}/turns",
        json={"message": "changed"},
        headers=auth(backend[3][0]) | {"Idempotency-Key": "turn-one"},
    )
    assert response.status_code == 409


@pytest.mark.parametrize("language", ["es", "pt"])
def test_stub_clarification_stops_before_actions(conversation_backend, language):
    backend = conversation_backend
    state = submit(backend, create(backend, language), "/clarify")
    assert event(state, "clarification")["trust"] == "untrusted"
    assert backend[0].action_metrics()["total_committed"] == 0


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_cards", {}),
        ("get_card", {"product_id": "card1"}),
        ("get_movements", {"product_id": "card1", "limit": 5}),
    ],
)
def test_read_tools_through_dispatcher_persist_correlated_results(conversation_backend, name, args):
    backend = conversation_backend
    inject(backend, {"kind": "tool_request", "name": name, "arguments": args})
    state = submit(backend, create(backend))
    result = event(state, "tool_result")
    assert result["turn_id"] == state["submitted_turn_id"]
    assert result["trust"] == "backend" and result["data"]["tool"] == name
    assert result["data"]["result"]["release_id"] == "test-release"
    assert result["confirmation_id"] is None and result["handoff_id"] is None


def test_prepare_confirm_reconnect_verified_evidence_and_no_model_self_confirmation(
    conversation_backend,
):
    backend = conversation_backend
    store, _, client, customers, _ = backend
    case = create(backend)
    state = submit(backend, case, "/pause card1")
    prepared = event(state, "confirmation_prepared")
    pending = prepared["data"]["result"]
    assert pending["status"] == "pending" and not pending["verified"]
    assert "evidence" not in pending and store.action_metrics()["total_committed"] == 0
    assert store.card(store.session(customers[0]), "card1")["card"]["simulator_state"] == "ACTIVE"
    with store.connect() as pg:
        row = pg.execute("SELECT conversation_id FROM simulator.action_confirmations").fetchone()
    assert row["conversation_id"] == case["conversation_id"]
    assert get(backend, create(backend, key="other-conversation"))["events"] == []
    confirmation_id = pending["confirmation_id"]
    assert (
        client.post(
            f"/me/action-confirmations/{confirmation_id}/confirm", headers=auth(customers[1])
        ).status_code
        == 404
    )
    receipt = client.post(
        f"/me/action-confirmations/{confirmation_id}/confirm", headers=auth(customers[0])
    ).json()
    assert receipt["verified"] and receipt["status"] == "executed"
    reconnected = get(backend, case)
    evidence = event(reconnected, "confirmation_status")
    assert evidence["turn_id"] == prepared["turn_id"]
    assert evidence["confirmation_id"] == confirmation_id
    assert evidence["data"]["result"]["evidence"]["action_id"] == receipt["evidence"]["action_id"]
    assert evidence["data"]["result"]["evidence"]["simulator_state"] == "PAUSED"
    assert get(backend, case) == reconnected  # Reconciliation never duplicates audit events.
    submit(backend, case, "/pause card1")  # Lost-response retry never calls model/tool again.
    assert store.action_metrics()["total_committed"] == 1
    with store.connect() as pg:
        assert (
            pg.execute("SELECT count(*) AS n FROM simulator.action_confirmations").fetchone()["n"]
            == 1
        )


def test_handoff_scope_excludes_other_conversation_actions_and_recovery_remains_available(
    conversation_backend,
):
    backend = conversation_backend
    store, handoffs, client, customers, _ = backend
    # Standalone committed action must not be swept into conversation handoff evidence.
    unrelated = store.action(store.session(customers[0]), "card1", "replacement", "unrelated")
    case = create(backend)
    prepared = event(submit(backend, case, "/pause card1"), "confirmation_prepared")
    receipt = client.post(
        f"/me/action-confirmations/{prepared['confirmation_id']}/confirm",
        headers=auth(customers[0]),
    ).json()
    state = submit(backend, case, "/handoff", key="handoff")
    created = event(state, "handoff_created")
    handoff = created["data"]["result"]
    assert handoff["conversation_id"] == case["conversation_id"]
    assert created["handoff_id"] == handoff["handoff_id"]
    assert handoff["model_context"]["trust"] == "model_or_customer_provided"
    receipts = handoff["verified_evidence"]["actions"]
    assert [r["receipt"]["action_id"] for r in receipts] == [receipt["evidence"]["action_id"]]
    assert unrelated["action_id"] not in json.dumps(receipts)
    with store.connect() as pg:
        pg.execute("UPDATE simulator.agent_users SET enabled=false WHERE agent_id='a1'")
    recovered = handoffs.recover(handoff["handoff_id"])
    assert recovered["assigned_agent_id"] == "a2"
    assert event(get(backend, case), "handoff_status")["data"]["result"] == {
        key: value for key, value in recovered.items() if key != "recovery"
    }


@pytest.mark.parametrize(
    "output",
    [
        None,
        [],
        {"kind": "other"},
        {"kind": "answer", "text": " "},
        {"kind": "answer", "text": "x" * 2001},
        {"kind": "answer", "text": "done", "verified": True},
        {"kind": "answer", "text": "done", "evidence": {"action_id": "a" * 32}},
        {"kind": "answer", "text": "hello", "customer_id": "c2"},
        {"kind": "tool_request", "name": "confirm", "arguments": {}},
        {"kind": "tool_request", "name": "recover_handoff", "arguments": {}},
        {"kind": "tool_request", "name": "unsupported", "arguments": {}},
        {
            "kind": "tool_request",
            "name": "pause_card",
            "arguments": {"product_id": "card1", "confirmed": True},
        },
        {"kind": "tool_request", "name": "get_cards", "arguments": {"customer_id": "c2"}},
        {
            "kind": "tool_request",
            "name": "pause_card",
            "arguments": {"product_id": "card1", "conversation_id": "forged"},
        },
        {
            "kind": "tool_request",
            "name": "pause_card",
            "arguments": {"product_id": "card1", "idempotency_key": "chosen"},
        },
    ],
)
def test_malformed_hostile_adapter_outputs_fail_closed(conversation_backend, output):
    backend = conversation_backend
    inject(backend, output)
    state = submit(backend, create(backend))
    assert event(state, "error")["data"] == {"code": "invalid_adapter_output", "verified": False}
    assert backend[0].action_metrics()["total_committed"] == 0
    with backend[0].connect() as pg:
        assert (
            pg.execute("SELECT count(*) AS n FROM simulator.action_confirmations").fetchone()["n"]
            == 0
        )
        assert pg.execute("SELECT count(*) AS n FROM simulator.handoffs").fetchone()["n"] == 0


def test_freeform_model_claim_is_never_verified_evidence(conversation_backend):
    backend = conversation_backend
    inject(backend, {"kind": "answer", "text": "The card was paused and refund issued"})
    state = submit(backend, create(backend))
    answer = event(state, "answer")
    assert answer["trust"] == "untrusted" and not answer["data"]["verified"]
    assert all(e["confirmation_id"] is None for e in state["events"])
    assert backend[0].action_metrics()["total_committed"] == 0


@pytest.mark.parametrize("failure", ["timeout", "exception", "disabled", "cancelled"])
def test_adapter_failure_is_persisted_sanitized_and_never_mutates(
    conversation_backend, failure, caplog
):
    backend = conversation_backend
    service = backend[2].app.state.conversations

    class Failing(DeterministicStub):
        async def propose(self, context):
            if failure == "cancelled":
                raise asyncio.CancelledError
            if failure == "timeout":
                await asyncio.sleep(1)
            raise RuntimeError("private-provider-secret")

    service.adapter = None if failure == "disabled" else Failing()
    backend[0].settings.adapter_timeout_seconds = 0.01
    case = create(backend)
    state = submit(backend, case, "/pause card1")
    code = {
        "timeout": "adapter_timeout",
        "exception": "adapter_failure",
        "disabled": "adapter_unavailable",
        "cancelled": "adapter_failure",
    }[failure]
    assert event(state, "error")["data"]["code"] == code
    assert "private-provider-secret" not in json.dumps(state) + caplog.text
    assert get(backend, case)["events"] == state["events"]
    assert backend[0].action_metrics()["total_committed"] == 0


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_card", {"product_id": "card2"}),
        ("pause_card", {"product_id": "card2"}),
    ],
)
def test_tool_ownership_failure_is_persisted_without_fallback(conversation_backend, name, args):
    backend = conversation_backend
    inject(backend, {"kind": "tool_request", "name": name, "arguments": args})
    result = submit(backend, create(backend))
    assert event(result, "error")["data"]["tool_error"] == "not_found"
    assert backend[0].action_metrics()["total_committed"] == 0


def test_handoff_database_failure_rolls_back_savepoint_but_persists_failed_turn(
    conversation_backend,
):
    backend = conversation_backend
    with backend[0].connect() as pg:
        pg.execute("ALTER TABLE simulator.handoffs ADD CONSTRAINT reject_fixture CHECK (false)")
    state = submit(backend, create(backend), "/handoff")
    assert event(state, "error")["data"]["tool_error"] == "backend_error"
    assert len(state["events"]) == 2


@pytest.mark.parametrize("command", ["/pause card1", "/handoff"])
def test_commit_failure_leaves_no_orphan_confirmation_handoff_or_turn(
    conversation_backend, command
):
    backend = conversation_backend
    case = create(backend)
    with backend[0].connect() as pg:
        pg.execute(
            "CREATE FUNCTION simulator.reject_turn() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'private-commit-error'; END $$"
        )
        pg.execute(
            "CREATE CONSTRAINT TRIGGER reject_turn AFTER INSERT ON simulator.conversation_events "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION simulator.reject_turn()"
        )
    response = backend[2].post(
        f"/me/conversations/{case['conversation_id']}/turns",
        json={"message": command},
        headers=auth(backend[3][0]) | {"Idempotency-Key": "commit-failure"},
    )
    assert response.status_code == 500 and "private-commit-error" not in response.text
    with backend[0].connect() as pg:
        for table in (
            "conversation_events",
            "conversation_turns",
            "action_confirmations",
            "handoffs",
            "actions",
        ):
            assert pg.execute(f"SELECT count(*) AS n FROM simulator.{table}").fetchone()["n"] == 0


def test_concurrent_identical_and_distinct_turns_have_one_effect_and_total_order(
    conversation_backend,
):
    backend = conversation_backend
    case = create(backend)
    service, token = backend[2].app.state.conversations, backend[3][0]

    def call(key):
        return service.submit(
            token, case["conversation_id"], SubmitTurn(message="/pause card1"), key
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(call, ["same"] * 4))
    assert len({r["submitted_turn_id"] for r in results}) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(call, ["two", "three", "four", "five"]))
    state = get(backend, case)
    assert [e["sequence"] for e in state["events"]] == list(range(1, 11))
    with backend[0].connect() as pg:
        assert (
            pg.execute("SELECT count(*) AS n FROM simulator.action_confirmations").fetchone()["n"]
            == 5
        )
    assert backend[0].action_metrics()["total_committed"] == 0


def test_stub_cards_and_disabled_default_are_explicit(conversation_backend):
    backend = conversation_backend
    state = submit(backend, create(backend), "/cards")
    assert event(state, "tool_result")["data"]["tool"] == "get_cards"
    backend[0].settings.conversation_adapter = "disabled"
    with TestClient(create_app(backend[0].settings, store=backend[0])) as client:
        body = client.post(
            "/me/conversations",
            json={"language": "es"},
            headers=auth(backend[3][0]) | {"Idempotency-Key": "disabled"},
        ).json()
        assert body["adapter"]["mode"] == "disabled"
        result = client.post(
            f"/me/conversations/{body['conversation_id']}/turns",
            json={"message": "/cards"},
            headers=auth(backend[3][0]) | {"Idempotency-Key": "disabled"},
        ).json()
        assert event(result, "error")["data"]["code"] == "adapter_unavailable"


@pytest.mark.parametrize("status", ["cancelled", "expired", "stale"])
def test_reconnect_never_marks_unexecuted_confirmations_verified(conversation_backend, status):
    backend = conversation_backend
    case = create(backend)
    state = submit(backend, case, "/pause card1")
    confirmation_id = event(state, "confirmation_prepared")["confirmation_id"]
    path = f"/me/action-confirmations/{confirmation_id}"
    if status == "cancelled":
        assert backend[2].post(path + "/cancel", headers=auth(backend[3][0])).status_code == 200
    elif status == "stale":
        backend[0].action(backend[0].session(backend[3][0]), "card1", "block", "separate-command")
        assert backend[2].post(path + "/confirm", headers=auth(backend[3][0])).status_code == 409
    else:
        with backend[0].connect() as pg:
            pg.execute(
                "UPDATE simulator.action_confirmations "
                "SET created_at=clock_timestamp()-interval '2h',"
                "expires_at=clock_timestamp()-interval '1h' WHERE confirmation_id=%s",
                (confirmation_id,),
            )
    result = event(get(backend, case), "confirmation_status")["data"]["result"]
    assert result["status"] == status and result["verified"] is False and "evidence" not in result


@pytest.mark.parametrize(
    "field,value",
    [
        ("customer_id", "c2"),
        ("conversation_id", "f" * 32),
        ("verified", True),
        ("language", "en"),
    ],
)
def test_http_cannot_choose_identity_or_inject_verification(conversation_backend, field, value):
    backend = conversation_backend
    headers = auth(backend[3][0]) | {"Idempotency-Key": "invalid"}
    assert (
        backend[2]
        .post(
            "/me/conversations",
            json={"language": "es", field: value},
            headers=headers,
        )
        .status_code
        == 422
    )
    case = create(backend)
    assert (
        backend[2]
        .post(
            f"/me/conversations/{case['conversation_id']}/turns",
            json={"message": "hello", field: value},
            headers=headers,
        )
        .status_code
        == 422
    )
    assert get(backend, case)["events"] == []


def test_same_creation_key_conflicts_on_changed_language(conversation_backend):
    backend = conversation_backend
    create(backend)
    assert (
        backend[2]
        .post(
            "/me/conversations",
            json={"language": "pt"},
            headers=auth(backend[3][0]) | {"Idempotency-Key": "conversation-one"},
        )
        .status_code
        == 409
    )


def test_provider_exception_after_preparation_cannot_create_a_fallback_action(
    conversation_backend, monkeypatch
):
    backend = conversation_backend
    case = create(backend)
    confirmations = backend[2].app.state.confirmations
    prepare = confirmations.prepare

    def fail_after_prepare(*args, **kwargs):
        prepare(*args, **kwargs)
        raise RuntimeError("private-tool-failure")

    monkeypatch.setattr(confirmations, "prepare", fail_after_prepare)
    state = submit(backend, case, "/pause card1")
    assert event(state, "error")["data"]["tool_error"] == "backend_error"
    with backend[0].connect() as pg:
        assert (
            pg.execute("SELECT count(*) AS n FROM simulator.action_confirmations").fetchone()["n"]
            == 0
        )
    assert backend[0].action_metrics()["total_committed"] == 0


@pytest.mark.parametrize(
    "output",
    [
        {
            "kind": "tool_request",
            "name": "register_unrecognized_charge",
            "arguments": {
                "product_id": "card1",
                "transaction_id": "tx1",
                "process_date": "2026-02-31",
            },
        },
        {
            "kind": "human_handoff",
            "triage": {
                "reason": "card_support",
                "severity": "low",
                "required_specialty": None,
                "minimum_experience": "Junior",
                "language": "pt",
                "summary": "Wrong language proposal",
            },
        },
    ],
)
def test_semantically_invalid_date_and_handoff_language_rejected_before_dispatch(
    conversation_backend,
    output,
    monkeypatch,
):
    backend = conversation_backend
    inject(backend, output)
    called = []
    monkeypatch.setattr(backend[2].app.state.tools, "execute", lambda *a, **k: called.append(True))
    state = submit(backend, create(backend, "es"))
    assert event(state, "error")["data"]["code"] == "invalid_adapter_output"
    assert called == []


@pytest.mark.parametrize("before_date,valid", [("2026-01-03", True), ("2026-01-04", False)])
def test_adapter_movement_cursor_accepts_four_fields_and_preserves_filter_checks(
    conversation_backend, before_date, valid
):
    backend = conversation_backend
    with backend[0].connect() as pg:
        pg.execute(
            "ALTER TABLE bank.transactions ALTER COLUMN transaction_date TYPE timestamp "
            "USING transaction_date::timestamp"
        )
        pg.execute(
            "INSERT INTO bank.transactions VALUES('test-release','c1','card1','tx2',"
            "'2026-01-01','2026-01-02',20,'USD','purchase','posted','Fixture Merchant')"
        )
    first = (
        backend[2]
        .get(
            "/me/cards/card1/movements",
            params={"limit": 1, "before_date": "2026-01-03"},
            headers=auth(backend[3][0]),
        )
        .json()
    )
    assert first["next_cursor"]
    inject(
        backend,
        {
            "kind": "tool_request",
            "name": "get_movements",
            "arguments": {
                "product_id": "card1",
                "limit": 1,
                "before_date": before_date,
                "cursor": first["next_cursor"],
            },
        },
    )
    state = submit(backend, create(backend))
    if valid:
        result = event(state, "tool_result")["data"]["result"]
        assert [row["transaction_id"] for row in result["movements"]] == ["tx2"]
    else:
        error = event(state, "error")["data"]
        assert error["code"] == "tool_failure" and error["tool_error"] == "invalid_arguments"
    assert backend[0].action_metrics()["total_committed"] == 0
