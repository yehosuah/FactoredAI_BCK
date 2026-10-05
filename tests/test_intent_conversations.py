"""HTTP journeys through the backend with the trained classifier adapter enabled."""

import pytest
from fastapi.testclient import TestClient
from test_conversations import create, event, submit
from test_handoffs import backend as backend

from factored_bck.app import create_app


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
