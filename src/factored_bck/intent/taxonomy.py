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
