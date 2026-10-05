"""Conversation adapter: trained intent model plus a deterministic routing policy.

The model only ranks intents. Every proposal stays untrusted: the backend validates it,
checks ownership and eligibility, and asks the customer to confirm any mutation.
"""

import re

from factored_bck.conversation_contract import AdapterInfo
from factored_bck.intent.datasets import normalize
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


FIRST = {
    "1",
    "uno",
    "una",
    "primera",
    "la primera",
    "el primero",
    "primero",
    "a primeira",
    "primeira",
    "o primeiro",
}
SECOND = {"2", "dos", "segunda", "la segunda", "el segundo", "segundo", "a segunda", "o segundo"}
MAX_ID_LENGTH = 100


def _card_ids(text):
    """Distinct card ids in order of appearance; overlong tokens are not ids."""
    found = [m for m in PRODUCT_ID.findall(text) if len(m) <= MAX_ID_LENGTH]
    return list(dict.fromkeys(found))


def _strip_ids(text):
    # Ids such as TEAM-CARD-PAUSED would otherwise read as words like "pausar".
    return PRODUCT_ID.sub(" ", text)


def _rank(model, text):
    probabilities = model.predict_proba(_strip_ids(text))
    return sorted(probabilities, key=probabilities.get, reverse=True), probabilities


def _handled(model, text):
    """Intent of a user message the policy would have acted on alone, else None."""
    ranked, probabilities = _rank(model, text)
    if ranked[0] != UNCLEAR and probabilities[ranked[0]] >= CONFIDENCE_THRESHOLD:
        return ranked[0]
    return None


def _is_clarification(text):
    prompts = UNCLEAR_PROMPTS.values()
    prefixes = [choice.split("{")[0] for choice in CHOICE.values()]
    return text in prompts or any(text.startswith(prefix) for prefix in prefixes)


def _choice_options(question):
    """Intents named in one of our choice questions, in the order they were offered."""
    for names in NAMES.values():
        found = [(question.find(name), label) for label, name in names.items() if name in question]
        if len(found) >= 2:
            return [label for _, label in sorted(found)]
    return []


def _ordinal(text):
    clean = normalize(text).strip(" .!")
    if clean in FIRST:
        return 0
    if clean in SECOND:
        return 1
    return None


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


def _choose_product(users):
    """One unambiguous card id: from the last message, else from earlier ones."""
    for scope in (users[-1:], users[:-1]):
        ids = _card_ids(" ".join(scope))
        if ids:
            return ids[0] if len(ids) == 1 else None
    return None


def route(model, context):
    language = context.language
    messages = list(context.messages)
    if not any(m.role == "user" for m in messages):
        return {"kind": "clarification", "question": UNCLEAR_PROMPTS[language]}
    users = [m.text for m in messages if m.role == "user"]
    last = users[-1]
    product = _choose_product(users)

    # The adapter never sees tool results, so it infers them: an earlier user message the
    # policy acted on with confidence was served, and only later turns are still pending.
    previous = messages[:-1]
    served_at, served_intent = -1, None
    for index in range(len(previous) - 1, -1, -1):
        if previous[index].role == "user":
            served_intent = _handled(model, previous[index].text)
            if served_intent:
                served_at = index
                break
    pending = previous[served_at + 1 :]
    clarifications = sum(m.role == "assistant" and _is_clarification(m.text) for m in pending)

    ranked, probabilities = _rank(model, last)
    intent, confidence = ranked[0], probabilities[ranked[0]]
    if confidence < CONFIDENCE_THRESHOLD:
        options = []
        if previous and previous[-1].role == "assistant":
            options = _choice_options(previous[-1].text)
        choice = _ordinal(last)
        if options and choice is not None:
            # The customer picked one of the two options we offered.
            intent, confidence = options[choice], 1.0
        elif (
            _card_ids(last)
            and served_intent in CARD_TOOLS
            and not _card_ids(previous[served_at].text)
        ):
            # A card id completes the served request that had to list cards first.
            intent, confidence = served_intent, 1.0
        elif clarifications:
            texts = [m.text for m in pending if m.role == "user"] + [last]
            joined_ranked, joined = _rank(model, " ".join(texts))
            if joined[joined_ranked[0]] > confidence:
                ranked, probabilities = joined_ranked, joined
                intent, confidence = ranked[0], probabilities[ranked[0]]

    if intent == UNCLEAR or confidence < CONFIDENCE_THRESHOLD:
        if clarifications >= MAX_UNCLEAR_PROMPTS:
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
