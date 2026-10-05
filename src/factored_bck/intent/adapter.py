"""Conversation adapter: trained intent model plus a deterministic routing policy.

The model only ranks intents. Every proposal stays untrusted: the backend validates it,
checks ownership and eligibility, and asks the customer to confirm any mutation.
"""

import re

from factored_bck.conversation_contract import CARD_SELECTION_CONFLICT, MUTATING_TOOLS, AdapterInfo
from factored_bck.intent.datasets import normalize
from factored_bck.intent.model import IntentModel
from factored_bck.intent.taxonomy import NAMES, PRIORITY, UNCLEAR

# Chosen in notebooks/01_evaluacion_clasificador_intenciones.ipynb, section 7: lowest
# threshold with at least 90% out-of-fold accuracy on the requests it automates.
CONFIDENCE_THRESHOLD = 0.6
# Unclear turns tolerated before a human takes over.
MAX_UNCLEAR_PROMPTS = 2
# Card identifiers look like DEMO-CARD-001: uppercase segments joined by hyphens.
PRODUCT_ID = re.compile(r"\b(?:[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+|card\d+[A-Za-z0-9_-]*)\b")

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
DIRECTION_VERBS = {
    "pause_card": r"\b(?:pausar|pausarla|pausarlo|pausa|pause)\b",
    "reactivate_card": (
        r"\b(?:reactivar|reactivarla|reactivarlo|reactiva|reactive|reativar|reativa|reative)\b"
    ),
    "activate_card": r"\b(?:activar|activarla|activarlo|activa|active|ativar|ativa|ative)\b",
}
BLOCK_REQUEST = r"\b(?:bloquea|bloqueala|bloquear|bloquearla|bloqueie|bloque|bloquees)\b"
LOSS_REPORT = (
    r"\b(?:perdi|perdido|perdida|extravie|extraviado|extraviada|robaron|roubaram|roubado|roubada)\b"
)


def _segments(text):
    return re.split(r"[.;,!?:]|\b(?:y|e|pero|mas|tambien|tambem)\b", normalize(text))


def _positive_loss(text):
    for segment in _segments(text):
        match = re.search(LOSS_REPORT, segment)
        if (
            match
            and re.search(r"\b(?:tarjeta|cartao)\b", segment)
            and not re.search(r"\b(?:no|nao|nunca)\b", segment[: match.start()])
        ):
            return True
    return False


def _negated_action(text):
    patterns = [*DIRECTION_VERBS.values(), BLOCK_REQUEST, LOSS_REPORT]
    return any(
        re.search(r"\b(?:no|nao|nunca)\b", segment[: match.start()])
        for segment in _segments(text)
        for pattern in patterns
        for match in re.finditer(pattern, segment)
    )


def _card_ids(text, known=()):
    """Distinct card ids in order of appearance; overlong tokens are not ids."""
    found = [
        (m.start(), m.group()) for m in PRODUCT_ID.finditer(text) if len(m.group()) <= MAX_ID_LENGTH
    ]
    for product in known:
        pattern = rf"(?<![\w-]){re.escape(product)}(?![\w-])"
        found.extend((m.start(), product) for m in re.finditer(pattern, text))
    return list(dict.fromkeys(product for _, product in sorted(found)))


def _strip_ids(text, known=()):
    # Ids such as TEAM-CARD-PAUSED would otherwise read as words like "pausar".
    for product in _card_ids(text, known):
        text = re.sub(rf"(?<![\w-]){re.escape(product)}(?![\w-])", " ", text)
    return PRODUCT_ID.sub(" ", text)


def _rank(model, text, known=()):
    probabilities = model.predict_proba(_strip_ids(text, known))
    return sorted(probabilities, key=probabilities.get, reverse=True), probabilities


def _handled(model, text, known=()):
    """Confident intent only; this never establishes that a request succeeded."""
    ranked, probabilities = _rank(model, text, known)
    if ranked[0] != UNCLEAR and probabilities[ranked[0]] >= CONFIDENCE_THRESHOLD:
        return ranked[0]
    return None


def _is_clarification(text):
    prompts = (*UNCLEAR_PROMPTS.values(), *CARD_SELECTION_CONFLICT.values())
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


def _choose_product(users, known=()):
    """One unambiguous card id: from the last message, else from earlier ones."""
    for scope in (users[-1:], users[:-1]):
        ids = _card_ids(" ".join(scope), known)
        if ids:
            return ids[0] if len(ids) == 1 else None
    return None


def _intent_at(model, messages, index, known):
    if _positive_loss(_strip_ids(messages[index].text, known)):
        return PRIORITY
    intent = _handled(model, messages[index].text, known)
    if intent is None and index and messages[index - 1].role == "assistant":
        options = _choice_options(messages[index - 1].text)
        choice = _ordinal(messages[index].text)
        if options and choice is not None:
            intent = options[choice]
    return intent


def route(model, context):
    language = context.language
    messages = list(context.messages)
    if not any(m.role == "user" for m in messages):
        return {"kind": "clarification", "question": UNCLEAR_PROMPTS[language]}
    users = [m.text for m in messages if m.role == "user"]
    last = users[-1]
    previous = messages[:-1]
    known = tuple(
        dict.fromkeys(
            product
            for m in previous
            if m.observation
            for product in m.observation.product_ids
            if product is not None
        )
    )
    selected = context.selected_product_id
    if selected is not None:
        known = tuple(dict.fromkeys((*known, selected)))
    explicit_ids = _card_ids(last, known)
    if selected and explicit_ids and any(product != selected for product in explicit_ids):
        return {"kind": "clarification", "question": CARD_SELECTION_CONFLICT[language]}
    product = selected or _choose_product(users, known)

    # Only persisted backend outcomes establish a turn boundary. A confident message
    # alone is not evidence of success; failures also stop stale intent aggregation.
    outcome_at, pending_intent = -1, None
    listed = ()
    for index in range(len(previous) - 1, -1, -1):
        observation = previous[index].observation
        if previous[index].role == "user" and observation:
            outcome_at = index
            if observation.tool == "get_cards" and observation.status == "read":
                pending_intent = _intent_at(model, previous, index, known)
                listed = observation.product_ids
            break
    pending = previous[outcome_at + 1 :]
    clarifications = sum(m.role == "assistant" and _is_clarification(m.text) for m in pending)

    ranked, probabilities = _rank(model, last, known)
    intent, confidence = ranked[0], probabilities[ranked[0]]
    wording = normalize(_strip_ids(last, known))
    if confidence < CONFIDENCE_THRESHOLD:
        options = []
        if previous and previous[-1].role == "assistant":
            options = _choice_options(previous[-1].text)
        choice = _ordinal(last)
        if options and choice is not None:
            # The customer picked one of the two options we offered.
            intent, confidence = options[choice], 1.0
        elif pending_intent in CARD_TOOLS and (
            _card_ids(last, known) or choice is not None and choice < len(listed)
        ):
            # Complete only an actual successful card listing. Ordinals refer to its
            # bounded owned IDs; the dispatcher still rechecks ownership and source.
            if not _card_ids(last, known):
                product = listed[choice]
                if selected and product != selected:
                    return {"kind": "clarification", "question": CARD_SELECTION_CONFLICT[language]}
            intent, confidence = pending_intent, 1.0
        elif clarifications:
            texts = [m.text for m in pending if m.role == "user"] + [last]
            joined_text = " ".join(texts)
            joined_ranked, joined = _rank(model, joined_text, known)
            if joined[joined_ranked[0]] > confidence:
                ranked, probabilities = joined_ranked, joined
                intent, confidence = ranked[0], probabilities[ranked[0]]
                wording = normalize(_strip_ids(joined_text, known))

    positive_loss = _positive_loss(wording)
    if (
        not positive_loss
        and re.search(r"\b(?:aumentar|aumenta|aumente|subir|ampliar|elevar)\b", wording)
        and re.search(r"\b(?:limite|cupo)\b", wording)
    ):
        summary = (
            "Solicitud de cambio de límite fuera del alcance del simulador: "
            if language == "es"
            else "Pedido de alteração de limite fora do escopo do simulador: "
        )
        return _handoff(
            language, "card_support", "low", None, "Junior", summary + _clip(last, 200), [], product
        )
    if re.search(BLOCK_REQUEST, wording) and not positive_loss:
        return {
            "kind": "clarification",
            "question": CHOICE[language].format(
                NAMES[language][PRIORITY], NAMES[language]["pause_card"]
            ),
        }
    if positive_loss:
        # Accepted policy gives an explicit loss/theft report priority over other
        # intents. This changes routing, not the learned probabilities or authority.
        intent, confidence = PRIORITY, 1.0

    # Character n-grams can confuse opposite action directions. Contradictory wording
    # or negation asks the customer; it never silently rewrites the model's ranking.
    directions = [
        label for label, pattern in DIRECTION_VERBS.items() if re.search(pattern, wording)
    ]
    if confidence >= CONFIDENCE_THRESHOLD and CARD_TOOLS.get(intent) in MUTATING_TOOLS:
        if _negated_action(wording) or (
            not positive_loss and re.search(r"\b(?:no|nao|nunca)\b", wording)
        ):
            intent, confidence = UNCLEAR, 0.0
        elif intent in DIRECTION_VERBS and directions and directions != [intent]:
            ranked = list(dict.fromkeys([*directions, *ranked]))
            confidence = 0.0

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
