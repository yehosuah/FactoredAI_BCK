import asyncio

import pytest

from factored_bck.conversation_contract import PROPOSAL, AdapterContext, ContextMessage
from factored_bck.intent.adapter import CHOICE, UNCLEAR_PROMPTS, ClassifierAdapter
from factored_bck.intent.taxonomy import NAMES


def propose(language, *messages):
    context = AdapterContext(
        language=language,
        history_truncated=False,
        messages=tuple(ContextMessage(role=role, text=text) for role, text in messages),
    )
    raw = asyncio.run(ClassifierAdapter().propose(context))
    proposal = PROPOSAL.validate_python(raw, strict=True)
    if raw["kind"] == "human_handoff":
        assert raw["triage"]["language"] == language
    return proposal


def test_info_identifies_an_injected_model_version():
    info = ClassifierAdapter().info
    assert info.mode == "injected"
    assert info.version == "intent-tfidf-lr-v1"


def test_loss_without_card_lists_cards_first():
    proposal = propose("es", ("user", "Me robaron la tarjeta en el bus"))
    assert (proposal.kind, proposal.name) == ("tool_request", "get_cards")


def test_card_id_in_a_follow_up_completes_the_earlier_request():
    proposal = propose("es", ("user", "Me robaron la tarjeta en el bus"), ("user", "DEMO-CARD-001"))
    assert proposal.kind == "tool_request"
    assert (proposal.name, proposal.arguments) == ("block_card", {"product_id": "DEMO-CARD-001"})


@pytest.mark.parametrize(
    "language,text,tool",
    [
        ("pt", "Quero pausar o cartão DEMO-CARD-001 por uns dias", "pause_card"),
        ("es", "Muéstrame las últimas compras de DEMO-CARD-001", "get_movements"),
        ("pt", "Qual é o limite disponível do DEMO-CARD-001?", "get_card"),
    ],
)
def test_confident_requests_with_a_card_become_tool_requests(language, text, tool):
    proposal = propose(language, ("user", text))
    assert proposal.kind == "tool_request"
    assert proposal.name == tool
    assert proposal.arguments["product_id"] == "DEMO-CARD-001"


@pytest.mark.parametrize("language", ["es", "pt"])
def test_unrecognized_charge_goes_to_a_fraud_specialist(language):
    text = {
        "es": "Hay un cobro de 300 dólares que yo no hice",
        "pt": "Tem uma cobrança de 300 reais que eu não fiz",
    }[language]
    proposal = propose(language, ("user", text))
    assert proposal.kind == "human_handoff"
    assert proposal.triage.reason == "fraud"
    assert proposal.triage.required_specialty == "Fraudes"
    assert proposal.triage.unresolved_questions


@pytest.mark.parametrize(
    "language,text", [("es", "Tengo una duda"), ("pt", "Estou com uma dúvida")]
)
def test_vague_requests_get_a_clarifying_question_in_their_language(language, text):
    proposal = propose(language, ("user", text))
    assert proposal.kind == "clarification"
    assert ("¿" in proposal.question) == (language == "es")


def test_repeated_unclear_requests_reach_a_human():
    asked = UNCLEAR_PROMPTS["es"]
    proposal = propose(
        "es",
        ("user", "Hola"),
        ("assistant", asked),
        ("user", "mmm no sé"),
        ("assistant", asked),
        ("user", "Hola de nuevo"),
    )
    assert proposal.kind == "human_handoff"
    assert proposal.triage.reason == "card_support"


def test_an_acknowledgement_after_a_served_request_does_not_repeat_it():
    proposal = propose(
        "es", ("user", "Quiero bloquear la tarjeta DEMO-CARD-001, me la robaron"), ("user", "ok")
    )
    assert proposal.kind == "clarification"


def test_clarifications_before_a_served_request_do_not_count_toward_handoff():
    asked = UNCLEAR_PROMPTS["es"]
    proposal = propose(
        "es",
        ("user", "Hola"),
        ("assistant", asked),
        ("user", "Quiero pausar la tarjeta DEMO-CARD-001"),
        ("user", "Hola"),
        ("assistant", asked),
        ("user", "Tengo una duda"),
    )
    assert proposal.kind == "clarification"


@pytest.mark.parametrize("reply,expected", [("la primera", 0), ("2", 1)])
def test_an_ordinal_reply_picks_an_option_from_the_choice_question(reply, expected):
    question = CHOICE["es"].format(NAMES["es"]["pause_card"], NAMES["es"]["reactivate_card"])
    proposal = propose(
        "es", ("user", "la tarjeta DEMO-CARD-001"), ("assistant", question), ("user", reply)
    )
    assert proposal.kind == "tool_request"
    assert proposal.name == ("pause_card", "reactivate_card")[expected]


def test_overlong_or_several_card_ids_fall_back_to_listing_cards():
    long_id = "A-" + "B" * 120
    proposal = propose("es", ("user", f"Quiero pausar la tarjeta {long_id}"))
    assert (proposal.kind, getattr(proposal, "name", None)) == ("tool_request", "get_cards")
    proposal = propose("es", ("user", "Me robaron DEMO-CARD-001 en el BRT-2"))
    assert (proposal.kind, proposal.name) == ("tool_request", "get_cards")


def test_card_ids_do_not_sway_the_intent():
    with_id = propose("es", ("user", "Quiero ver el estado de la tarjeta TEAM-CARD-PAUSED"))
    without_id = propose("es", ("user", "Quiero ver el estado de la tarjeta"))
    assert with_id == without_id
    assert with_id.question.startswith(
        CHOICE["es"].format(NAMES["es"]["query_card_status"], "")[:-2]
    )


def test_a_context_without_user_messages_gets_the_generic_question():
    context = AdapterContext(language="pt", history_truncated=False, messages=())
    raw = asyncio.run(ClassifierAdapter().propose(context))
    assert raw == {"kind": "clarification", "question": UNCLEAR_PROMPTS["pt"]}
