"""Starting card routing taxonomy card-routing-v1, mirrored from the ETL handoff."""

TAXONOMY_ID = "card-routing-v1"
TAXONOMY_VERSION = "0.1.0-draft"

LABELS = (
    "block_lost_stolen",
    "pause_card",
    "reactivate_card",
    "activate_card",
    "request_replacement",
    "query_card_status",
    "query_balance_limit",
    "query_movements",
    "register_unrecognized_charge",
    "unsupported_unclear",
)

# Accepted routing rule: loss or theft is handled before any other request.
PRIORITY = "block_lost_stolen"
UNCLEAR = "unsupported_unclear"

# Display names from the ETL taxonomy file, used in clarifying questions.
NAMES = {
    "es": {
        "block_lost_stolen": "bloquear tu tarjeta por pérdida o robo",
        "pause_card": "pausar tu tarjeta temporalmente",
        "reactivate_card": "reactivar una tarjeta pausada",
        "activate_card": "activar una tarjeta nueva",
        "request_replacement": "solicitar un reemplazo",
        "query_card_status": "consultar el estado de tu tarjeta",
        "query_balance_limit": "consultar tu saldo o límite",
        "query_movements": "consultar tus movimientos",
        "register_unrecognized_charge": "reportar un cargo que no reconoces",
    },
    "pt": {
        "block_lost_stolen": "bloquear seu cartão por perda ou roubo",
        "pause_card": "pausar seu cartão temporariamente",
        "reactivate_card": "reativar um cartão pausado",
        "activate_card": "ativar um cartão novo",
        "request_replacement": "solicitar uma substituição",
        "query_card_status": "consultar o status do seu cartão",
        "query_balance_limit": "consultar seu saldo ou limite",
        "query_movements": "consultar suas movimentações",
        "register_unrecognized_charge": "registrar uma cobrança que você não reconhece",
    },
}
