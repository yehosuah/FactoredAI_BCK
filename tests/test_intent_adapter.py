import asyncio

import pytest

from factored_bck.conversation_contract import (
    PROPOSAL,
    AdapterContext,
    ContextMessage,
    ToolObservation,
)
from factored_bck.intent.adapter import CHOICE, UNCLEAR_PROMPTS, ClassifierAdapter
from factored_bck.intent.taxonomy import NAMES


def propose(language, *messages, observations=None, selected_product_id=None):
    observations = observations or {}
    context = AdapterContext(
        language=language,
        history_truncated=False,
        selected_product_id=selected_product_id,
        messages=tuple(
            ContextMessage(role=role, text=text, observation=observations.get(i))
            for i, (role, text) in enumerate(messages)
        ),
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
    proposal = propose(
        "es",
        ("user", "Me robaron la tarjeta en el bus"),
        ("user", "DEMO-CARD-001"),
        observations={
            0: ToolObservation(tool="get_cards", status="read", product_ids=("DEMO-CARD-001",))
        },
    )
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
        "es",
        ("user", "Quiero bloquear la tarjeta DEMO-CARD-001, me la robaron"),
        ("user", "ok"),
        observations={
            0: ToolObservation(tool="block_card", status="prepared", product_ids=("DEMO-CARD-001",))
        },
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
        observations={
            2: ToolObservation(tool="pause_card", status="prepared", product_ids=("DEMO-CARD-001",))
        },
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
    proposal = propose("es", ("user", f"Quiero pausar la tarjeta por unos días {long_id}"))
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


@pytest.mark.parametrize("observation", [None, ToolObservation(tool="get_cards", status="failed")])
def test_a_confident_message_is_not_evidence_of_a_successful_card_listing(observation):
    proposal = propose(
        "es",
        ("user", "Me robaron la tarjeta en el bus"),
        ("user", "DEMO-CARD-001"),
        observations={0: observation},
    )
    assert proposal.kind == "clarification"


def test_listed_card_ids_complete_an_intent_chosen_from_a_clarification():
    question = CHOICE["pt"].format(NAMES["pt"]["pause_card"], NAMES["pt"]["reactivate_card"])
    proposal = propose(
        "pt",
        ("user", "Meu cartão"),
        ("assistant", question),
        ("user", "a primeira"),
        ("user", "a segunda"),
        observations={
            2: ToolObservation(
                tool="get_cards", status="read", product_ids=("card-one", "card-two")
            )
        },
    )
    assert proposal.kind == "tool_request"
    assert proposal.name == "pause_card"
    assert proposal.arguments == {"product_id": "card-two"}


def test_unusable_card_id_keeps_its_list_position_for_an_ordinal_selection():
    proposal = propose(
        "es",
        ("user", "Me robaron la tarjeta en el bus"),
        ("user", "la primera"),
        observations={
            0: ToolObservation(tool="get_cards", status="read", product_ids=(None, "card-one"))
        },
    )
    assert proposal.kind == "tool_request"
    assert proposal.name == "get_cards"


@pytest.mark.parametrize(
    "language,text",
    [
        ("es", "Quiero pausar mi tarjeta DEMO-CARD-001"),
        ("pt", "Quero pausar meu cartão DEMO-CARD-001"),
    ],
)
def test_explicit_pause_request_cannot_silently_propose_the_opposite_direction(language, text):
    proposal = propose(language, ("user", text))
    assert proposal.kind == "clarification"
    assert NAMES[language]["pause_card"] in proposal.question
    assert NAMES[language]["reactivate_card"] in proposal.question


@pytest.mark.parametrize(
    "language,text",
    [
        ("es", "No quiero pausar mi tarjeta por unos días"),
        ("pt", "Não quero pausar o cartão por uns dias"),
    ],
)
def test_negated_action_requests_are_clarified(language, text):
    assert propose(language, ("user", text), selected_product_id="card1").kind == "clarification"


@pytest.mark.parametrize(
    "language,text",
    [
        ("es", "Perdí mi tarjeta y aparecen compras que no reconozco"),
        ("pt", "Perdi meu cartão e há uma cobrança que não fiz"),
    ],
)
def test_explicit_loss_takes_priority_over_a_concurrent_charge_request(language, text):
    proposal = propose(language, ("user", text), selected_product_id="card1")
    assert proposal.kind == "tool_request"
    assert proposal.name == "block_card" and proposal.arguments == {"product_id": "card1"}


@pytest.mark.parametrize(
    "language,text",
    [
        ("es", "Necesito bloquear la tarjeta"),
        ("pt", "Preciso bloquear meu cartão"),
    ],
)
def test_unspecified_blocking_asks_loss_or_temporary_pause(language, text):
    proposal = propose(language, ("user", text), selected_product_id="card1")
    assert proposal.kind == "clarification"
    assert NAMES[language]["block_lost_stolen"] in proposal.question
    assert NAMES[language]["pause_card"] in proposal.question


@pytest.mark.parametrize(
    "language,text",
    [
        ("es", "Quiero ampliar el límite de mi tarjeta"),
        ("pt", "Quero aumentar o limite do cartão"),
    ],
)
def test_unsupported_limit_change_reaches_a_human_instead_of_a_balance_read(language, text):
    proposal = propose(language, ("user", text), selected_product_id="card1")
    assert proposal.kind == "human_handoff"
    assert proposal.triage.reason == "card_support"


def test_a_negated_block_declines_preparation_even_with_a_loss_report():
    proposal = propose(
        "es", ("user", "Perdí mi tarjeta pero no quiero bloquearla"), selected_product_id="card1"
    )
    assert proposal.kind == "clarification"


@pytest.mark.parametrize(
    "language,text",
    [
        ("es", "No quiero pausar mi tarjeta por unos días"),
        ("pt", "Não quero pausar o cartão por uns dias"),
    ],
)
def test_selecting_a_card_after_clarification_does_not_reverse_a_negated_request(language, text):
    proposal = propose(
        language,
        ("user", text),
        ("assistant", UNCLEAR_PROMPTS[language]),
        ("user", "card1"),
        selected_product_id="card1",
    )
    assert proposal.kind == "clarification"
