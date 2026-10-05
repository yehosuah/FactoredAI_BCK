"""Conversation adapter: trained intent model plus a deterministic routing policy.

The model only ranks intents. Every proposal stays untrusted: the backend validates it,
checks ownership and eligibility, and asks the customer to confirm any mutation.
"""

import re

from factored_bck.conversation_contract import AdapterInfo
from factored_bck.intent.model import IntentModel
from factored_bck.intent.taxonomy import NAMES, PRIORITY, UNCLEAR

# Chosen in notebooks/01_evaluacion_clasificador_intenciones.ipynb, section 7: lowest
# threshold with at least 90% out-of-fold accuracy on the requests it automates.
CONFIDENCE_THRESHOLD = 0.6
# Unclear turns tolerated before a human takes over.
MAX_UNCLEAR_PROMPTS = 2
# Card identifiers look like DEMO-CARD-001: uppercase segments joined by hyphens.
PRODUCT_ID = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b")

CARD_TOOLS = {
    "block_lost_stolen": "block_card",
    "pause_card": "pause_card",
    "reactivate_card": "reactivate_card",
    "activate_card": "activate_card",
    "request_replacement": "request_replacement",
    "query_card_status": "get_card",
    "query_balance_limit": "get_card",
    "query_movements": "get_movements",
}

UNCLEAR_PROMPTS = {
    "es": "Puedo ayudarte con tu tarjeta: bloquearla por pérdida o robo, pausarla o "
    "reactivarla, activarla, pedir un reemplazo, consultar su estado, saldo o movimientos, "
    "o reportar un cargo que no reconoces. ¿Qué necesitas?",
    "pt": "Posso ajudar com seu cartão: bloquear por perda ou roubo, pausar ou reativar, "
    "ativar, pedir uma substituição, consultar status, saldo ou movimentações, ou registrar "
    "uma cobrança que você não reconhece. O que você precisa?",
}
CHOICE = {"es": "Para ayudarte bien, ¿quieres {} o {}?", "pt": "Para ajudar, você quer {} ou {}?"}
FRAUD_SUMMARY = {
    "es": "El cliente reporta un cargo no reconocido o posible fraude. Mensaje: {}",
    "pt": "O cliente relata uma cobrança não reconhecida ou possível fraude. Mensagem: {}",
}
FRAUD_QUESTIONS = {
    "es": [
        "Identificador y fecha de la transacción disputada",
        "Si el cliente aún tiene la tarjeta en su poder",
    ],
    "pt": [
        "Identificador e data da transação contestada",
        "Se o cliente ainda está com o cartão",
    ],
}
UNCLEAR_SUMMARY = {
    "es": "El asistente no pudo identificar la solicitud tras varias aclaraciones. Mensaje: {}",
    "pt": "O assistente não identificou a solicitação após várias tentativas. Mensagem: {}",
}


def _rank(model, text):
    probabilities = model.predict_proba(text)
    return sorted(probabilities, key=probabilities.get, reverse=True), probabilities


def _recent_clarifications(context):
    """Clarifying questions we asked since the last reply that was not one."""
    prefix = CHOICE[context.language].split("{")[0]
    count = 0
    for message in reversed(context.messages):
        if message.role != "assistant":
            continue
        if message.text != UNCLEAR_PROMPTS[context.language] and not message.text.startswith(
            prefix
        ):
            break
        count += 1
    return count


def _clip(text, limit=300):
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _handoff(language, reason, severity, specialty, experience, summary, questions, product):
    triage = {
        "reason": reason,
        "severity": severity,
        "required_specialty": specialty,
        "minimum_experience": experience,
        "language": language,
        "summary": summary,
        "unresolved_questions": questions,
    }
    if product:
        triage["product_id"] = product
    return {"kind": "human_handoff", "triage": triage}


def route(model, context):
    language = context.language
    users = [m.text for m in context.messages if m.role == "user"]
    last = users[-1]
    products = PRODUCT_ID.findall(" ".join(users))
    product = products[-1] if products else None

    ranked, probabilities = _rank(model, last)
    if probabilities[ranked[0]] < CONFIDENCE_THRESHOLD and len(users) > 1:
        # A short follow-up such as a card id inherits the earlier request.
        joined_ranked, joined = _rank(model, " ".join(users[-3:]))
        if joined[joined_ranked[0]] > probabilities[ranked[0]]:
            ranked, probabilities = joined_ranked, joined
    intent, confidence = ranked[0], probabilities[ranked[0]]

    if intent == UNCLEAR or confidence < CONFIDENCE_THRESHOLD:
        if _recent_clarifications(context) >= MAX_UNCLEAR_PROMPTS:
            summary = UNCLEAR_SUMMARY[language].format(_clip(last))
            return _handoff(language, "card_support", "low", None, "Junior", summary, [], product)
        if intent == UNCLEAR:
            return {"kind": "clarification", "question": UNCLEAR_PROMPTS[language]}
        options = [label for label in ranked if label != UNCLEAR][:2]
        if PRIORITY in options:
            options.sort(key=lambda label: label != PRIORITY)
        names = NAMES[language]
        return {
            "kind": "clarification",
            "question": CHOICE[language].format(names[options[0]], names[options[1]]),
        }

    if intent == "register_unrecognized_charge":
        summary = FRAUD_SUMMARY[language].format(_clip(last))
        return _handoff(
            language,
            "fraud",
            "high",
            "Fraudes",
            "Mid-Senior",
            summary,
            FRAUD_QUESTIONS[language],
            product,
        )

    if product is None:
        return {"kind": "tool_request", "name": "get_cards", "arguments": {}}
    return {
        "kind": "tool_request",
        "name": CARD_TOOLS[intent],
        "arguments": {"product_id": product},
    }


class ClassifierAdapter:
    def __init__(self, model=None):
        self.model = model or IntentModel.load()
        self.info = AdapterInfo(
            provider="intent-classifier", version=self.model.version, mode="injected"
        )

    async def propose(self, context):
        # Inference is pure CPU work in microseconds; it never blocks on I/O.
        return route(self.model, context)
