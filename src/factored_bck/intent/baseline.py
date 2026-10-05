"""Keyword rules: the simple system the trained model has to beat."""

from factored_bck.intent.datasets import normalize
from factored_bck.intent.taxonomy import UNCLEAR

# Checked in order; the first match wins. Loss and theft go first by the routing rule.
# Stems were written from general knowledge of es/pt, before any evaluation data was seen.
KEYWORDS = (
    (
        "block_lost_stolen",
        (
            "perdi",
            "robaron",
            "robo",
            "robad",
            "hurt",
            "extravi",
            "roub",
            "furt",
            "asalt",
            "assalt",
            "perda",
        ),
    ),
    (
        "register_unrecognized_charge",
        (
            "no reconozco",
            "nao reconhec",
            "desconozco",
            "no autorizad",
            "nao autorizad",
            "fraude",
            "clonar",
            "no hice",
            "nao fiz",
            "contestar",
            "disput",
        ),
    ),
    ("reactivate_card", ("reactiv", "reativ", "descongel", "reanud", "retom")),
    ("pause_card", ("paus", "congel", "suspend")),
    ("activate_card", ("activ", "ativ")),
    (
        "request_replacement",
        ("reposic", "reemplaz", "substitu", "segunda via", "duplicado", "rota", "roto", "quebr"),
    ),
    (
        "query_movements",
        ("movimient", "movimenta", "transac", "compras", "extrato", "historial", "historico"),
    ),
    ("query_balance_limit", ("saldo", "limite", "cupo")),
    ("query_card_status", ("estado", "status", "estatus")),
)


def predict_keyword(text):
    clean = normalize(text)
    for label, stems in KEYWORDS:
        if any(stem in clean for stem in stems):
            return label
    return UNCLEAR
