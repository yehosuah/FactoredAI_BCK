"""HTTP journeys through the backend with the trained classifier adapter enabled."""

import pytest
from fastapi.testclient import TestClient
from test_conversations import create, event, get, inject, submit
from test_handoffs import auth
from test_handoffs import backend as backend

from factored_bck.app import create_app
from factored_bck.intent.adapter import ClassifierAdapter


@pytest.fixture
def classifier_backend(backend):
    store, handoffs, _, customers, agents = backend
    store.settings.conversation_adapter = "classifier"
    with store.connect() as pg:
        pg.execute(
            "INSERT INTO simulator.card_states(product_id,customer_id,state) "
            "SELECT product_id,customer_id,'ACTIVE' FROM simulator.fixture_cards"
        )
    with TestClient(create_app(store.settings, store=store)) as client:
        yield store, handoffs, client, customers, agents


def test_lost_card_lists_the_customers_cards(classifier_backend):
    conversation = create(classifier_backend, "es")
    state = submit(classifier_backend, conversation, "Me robaron la tarjeta en el bus")
    assert state["adapter"] == {
        "provider": "intent-classifier",
        "version": "intent-tfidf-lr-v1",
        "mode": "injected",
    }
    assert event(state, "tool_result")["data"]["tool"] == "get_cards"


def test_unrecognized_charge_creates_a_fraud_handoff(classifier_backend):
    conversation = create(classifier_backend, "pt")
    state = submit(classifier_backend, conversation, "Tem uma cobrança de 300 reais que eu não fiz")
    assert event(state, "handoff_created")["handoff_id"]


def test_vague_message_gets_a_clarifying_question(classifier_backend):
    conversation = create(classifier_backend, "es")
    state = submit(classifier_backend, conversation, "Tengo una duda")
    assert "¿" in event(state, "clarification")["data"]["text"]


@pytest.mark.parametrize("reply", ["card1", "la primera"])
def test_card_selection_completes_the_actual_listed_request_without_executing(
    classifier_backend, reply
):
    backend = classifier_backend
    conversation = create(backend, "es")
    listed = submit(backend, conversation, "Me robaron la tarjeta en el bus")
    assert event(listed, "tool_result")["data"]["tool"] == "get_cards"
    prepared = submit(backend, conversation, reply, key="select-card")
    confirmation = event(prepared, "confirmation_prepared")
    assert confirmation["data"]["arguments"]["product_id"] == "card1"
    assert confirmation["data"]["result"]["status"] == "pending"
    assert backend[0].action_metrics()["total_committed"] == 0


@pytest.mark.parametrize(
    "language,message",
    [
        ("es", "Quiero pausar mi tarjeta por unos días"),
        ("pt", "Quero pausar o cartão por uns dias"),
    ],
)
def test_real_model_confirmation_reconnect_isolation_and_restart(
    classifier_backend, language, message
):
    backend = classifier_backend
    store, _, client, customers, agents = backend

    class RecordingClassifier(ClassifierAdapter):
        contexts = []

        async def propose(self, context):
            self.contexts.append(context)
            return await super().propose(context)

    adapter = RecordingClassifier()
    client.app.state.conversations.adapter = adapter
    conversation = create(backend, language)
    submit(backend, conversation, message)
    state = submit(backend, conversation, "card1", key="selection")
    pending = event(state, "confirmation_prepared")
    assert pending["data"]["tool"] == "pause_card"
    assert pending["data"]["result"]["verified"] is False
    assert store.action_metrics()["total_committed"] == 0
    observation = adapter.contexts[-1].messages[0].observation
    assert (observation.tool, observation.status, observation.product_ids) == (
        "get_cards",
        "read",
        ("card1",),
    )
    context = adapter.contexts[-1].model_dump_json()
    assert all(value not in context for value in customers + agents)
    assert all(
        value not in context
        for value in ["card2", "private-email", "TEAM-1234", "current_balance", "customer_id"]
    )
    path = f"/me/action-confirmations/{pending['confirmation_id']}/confirm"
    for token, status in [(customers[1], 404), (agents[0], 401)]:
        assert client.post(path, headers=auth(token)).status_code == status
    response = client.post(path, headers=auth(customers[0]))
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["verified"] and receipt["evidence"]["simulator_state"] == "PAUSED"
    assert client.post(path, headers=auth(customers[0])).json() == receipt
    submit(backend, conversation, "card1", key="selection")
    assert store.action_metrics()["total_committed"] == 1
    connected = get(backend, conversation)
    persisted = event(connected, "confirmation_status")["data"]["result"]
    assert persisted["evidence"]["action_id"] == receipt["evidence"]["action_id"]
    with TestClient(create_app(store.settings, store=store)) as restarted:
        assert (
            restarted.get(
                f"/me/conversations/{conversation['conversation_id']}", headers=auth(customers[0])
            ).json()
            == connected
        )
    store.logout(customers[0])
    assert (
        client.get(
            f"/me/conversations/{conversation['conversation_id']}", headers=auth(customers[0])
        ).status_code
        == 401
    )


def test_unusable_listed_ids_do_not_break_bounded_adapter_context(classifier_backend):
    backend = classifier_backend
    with backend[0].connect() as pg:
        pg.execute(
            "INSERT INTO simulator.fixture_cards VALUES(%s,'c1','Tarjeta Crédito',"
            "'TEAM-1234','USD',10,100,'ACTIVE','team_synthetic')",
            ("x" * 101,),
        )
    conversation = create(backend)
    submit(backend, conversation, "Me robaron la tarjeta en el bus")
    selected = submit(backend, conversation, "card1", key="selection")
    assert event(selected, "confirmation_prepared")["data"]["arguments"]["product_id"] == "card1"


@pytest.mark.parametrize(
    "language,message",
    [
        ("es", "Quiero pausar mi tarjeta por unos días"),
        ("pt", "Quero pausar o cartão por uns dias"),
    ],
)
def test_selected_owned_card_is_a_hint_and_only_prepares(classifier_backend, language, message):
    backend = classifier_backend
    state = submit(backend, create(backend, language), message, selected_product_id="card1")
    pending = event(state, "confirmation_prepared")
    assert pending["data"]["tool"] == "pause_card"
    assert pending["data"]["arguments"]["product_id"] == "card1"
    assert backend[0].action_metrics()["total_committed"] == 0


@pytest.mark.parametrize("selected", ["card2", "unknown-card"])
def test_selected_foreign_or_missing_card_fails_before_turn_or_adapter(
    classifier_backend, selected
):
    backend = classifier_backend
    conversation = create(backend)
    adapter = inject(backend, {"kind": "answer", "text": "must not run"})
    response = backend[2].post(
        f"/me/conversations/{conversation['conversation_id']}/turns",
        headers=auth(backend[3][0]) | {"Idempotency-Key": "selection"},
        json={"message": "help", "selected_product_id": selected},
    )
    assert response.status_code == 404
    assert adapter.contexts == [] and get(backend, conversation)["events"] == []


def test_selection_is_captured_in_retries_and_null_keeps_legacy_fingerprint(classifier_backend):
    backend = classifier_backend
    conversation = create(backend)
    adapter = inject(backend, {"kind": "answer", "text": "untrusted"})
    original = submit(backend, conversation, "help", selected_product_id="card1")
    assert adapter.contexts[0].selected_product_id == "card1"
    assert adapter.contexts[0].messages[-1].text == "help"
    assert submit(backend, conversation, "help", selected_product_id="card1") == original
    path = f"/me/conversations/{conversation['conversation_id']}/turns"
    for selected in [None, "card2"]:
        response = backend[2].post(
            path,
            headers=auth(backend[3][0]) | {"Idempotency-Key": "turn-one"},
            json={"message": "help", "selected_product_id": selected},
        )
        assert response.status_code == 409
    legacy = submit(backend, conversation, "old request", key="legacy")
    with backend[0].connect() as pg:
        payload = pg.execute(
            "SELECT payload FROM simulator.conversation_turns WHERE idempotency_key='legacy'"
        ).fetchone()["payload"]
    assert payload == {"message": "old request", "language": None}
    assert (
        submit(backend, conversation, "old request", key="legacy", selected_product_id=None)
        == legacy
    )
    assert len(adapter.contexts) == 2


@pytest.mark.parametrize(
    "kind", ["classifier", "classifier-lower-id", "matching-injected", "tool", "handoff"]
)
def test_conflicting_card_reference_is_clarified_even_for_injected_adapters(
    classifier_backend, kind
):
    backend = classifier_backend
    conversation = create(backend)
    message = "Quiero pausar mi tarjeta por unos días DEMO-CARD-OTHER"
    if kind == "classifier-lower-id":
        message = "Quiero pausar mi tarjeta por unos días card2"
    if kind == "matching-injected":
        inject(
            backend,
            {"kind": "tool_request", "name": "pause_card", "arguments": {"product_id": "card1"}},
        )
    if kind == "tool":
        inject(
            backend,
            {
                "kind": "tool_request",
                "name": "pause_card",
                "arguments": {"product_id": "DEMO-CARD-OTHER"},
            },
        )
    elif kind == "handoff":
        inject(
            backend,
            {
                "kind": "human_handoff",
                "triage": {
                    "reason": "card_support",
                    "severity": "low",
                    "required_specialty": None,
                    "minimum_experience": "Junior",
                    "language": "es",
                    "summary": "help",
                    "product_id": "DEMO-CARD-OTHER",
                },
            },
        )
    state = submit(backend, conversation, message, selected_product_id="card1")
    assert event(state, "clarification")["data"]["verified"] is False
    assert not any(
        e["kind"] in ("confirmation_prepared", "handoff_created") for e in state["events"]
    )
    assert backend[0].action_metrics()["total_committed"] == 0
    if kind == "matching-injected":
        assert backend[2].app.state.conversations.adapter.contexts == []


@pytest.mark.parametrize("kind", ["tool_request", "human_handoff"])
def test_host_clarifies_a_proposed_target_conflict_without_an_explicit_message_id(
    classifier_backend, kind
):
    backend = classifier_backend
    if kind == "tool_request":
        raw = {"kind": kind, "name": "pause_card", "arguments": {"product_id": "card2"}}
    else:
        raw = {
            "kind": kind,
            "triage": {
                "reason": "card_support",
                "severity": "low",
                "required_specialty": None,
                "minimum_experience": "Junior",
                "language": "es",
                "summary": "help",
                "product_id": "card2",
            },
        }
    adapter = inject(backend, raw)
    state = submit(backend, create(backend), "Necesito ayuda", selected_product_id="card1")
    assert event(state, "clarification")["data"]["verified"] is False
    assert len(adapter.contexts) == 1
    assert not any(
        e["kind"] in ("confirmation_prepared", "handoff_created") for e in state["events"]
    )


@pytest.mark.parametrize(
    "language,text,reply",
    [
        ("es", "Quiero pausar mi tarjeta", "la primera"),
        ("pt", "Quero pausar meu cartão", "a primeira"),
    ],
)
def test_opposite_direction_clarifies_then_customer_selects_pause(
    classifier_backend, language, text, reply
):
    backend = classifier_backend
    conversation = create(backend, language)
    clarified = submit(backend, conversation, text, selected_product_id="card1")
    assert event(clarified, "clarification")
    selected = submit(backend, conversation, reply, key="choose", selected_product_id="card1")
    assert event(selected, "confirmation_prepared")["data"]["tool"] == "pause_card"
    assert backend[0].action_metrics()["total_committed"] == 0


@pytest.mark.parametrize("selected", ["", "   ", "x" * 101, 1, True, {}])
def test_selected_hint_has_a_strict_bounded_input_contract(classifier_backend, selected):
    backend = classifier_backend
    conversation = create(backend)
    response = backend[2].post(
        f"/me/conversations/{conversation['conversation_id']}/turns",
        headers=auth(backend[3][0]) | {"Idempotency-Key": "invalid"},
        json={"message": "help", "selected_product_id": selected},
    )
    assert response.status_code == 422
    assert get(backend, conversation)["events"] == []


def test_publication_busy_rejects_selected_hint_without_persisting_a_turn(classifier_backend):
    backend = classifier_backend
    conversation = create(backend)
    adapter = inject(backend, {"kind": "answer", "text": "untrusted"})
    with backend[0].connect() as publisher:
        publisher.execute("SELECT pg_advisory_xact_lock(7236148201)")
        response = backend[2].post(
            f"/me/conversations/{conversation['conversation_id']}/turns",
            headers=auth(backend[3][0]) | {"Idempotency-Key": "publish-busy"},
            json={"message": "help", "selected_product_id": "card1"},
        )
        assert response.status_code == 503 and response.headers["Retry-After"] == "1"
    assert adapter.contexts == [] and get(backend, conversation)["events"] == []
    submit(backend, conversation, "help", key="publish-busy", selected_product_id="card1")
    assert len(adapter.contexts) == 1


@pytest.mark.parametrize(
    "language,message",
    [
        ("es", "Perdí mi tarjeta y aparecen compras que no reconozco"),
        ("pt", "Perdi meu cartão e há uma cobrança que não fiz"),
    ],
)
def test_loss_priority_survives_missing_card_selection_and_still_needs_confirmation(
    classifier_backend, language, message
):
    backend = classifier_backend
    conversation = create(backend, language)
    assert (
        event(submit(backend, conversation, message), "tool_result")["data"]["tool"] == "get_cards"
    )
    selected = submit(backend, conversation, "card1", key="choose-card")
    assert event(selected, "confirmation_prepared")["data"]["tool"] == "block_card"
    assert not any(e["kind"] == "handoff_created" for e in selected["events"])
    assert backend[0].action_metrics()["total_committed"] == 0
