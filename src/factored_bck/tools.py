"""Internal, provider-neutral tool interface over the authenticated Store contract."""

from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from factored_bck.evidence import ActionEvidence
from factored_bck.handoff import CreateHandoffArguments, GetHandoffArguments
from factored_bck.handoff_store import HandoffStore

ProductId = Annotated[str, Field(min_length=1, max_length=100, pattern=r"\S")]
DateString = Annotated[str, Field(min_length=10, max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$")]


class ExecutionContext(BaseModel):
    """Trusted transport supplies the credential separately from model-produced arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    session_token: SecretStr = Field(min_length=1, max_length=200, exclude=True, repr=False)


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class CardArguments(NoArguments):
    product_id: ProductId


class MovementArguments(CardArguments):
    limit: int = Field(default=50, ge=1, le=100)
    before_date: DateString | None = None

    @field_validator("before_date")
    @classmethod
    def valid_date(cls, value):
        if value is not None:
            date.fromisoformat(value)
        return value


class ActionArguments(CardArguments):
    idempotency_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")


class ChargeArguments(ActionArguments):
    transaction_id: str = Field(min_length=1, max_length=30, pattern=r"\S")
    process_date: DateString

    @field_validator("process_date")
    @classmethod
    def valid_date(cls, value):
        date.fromisoformat(value)
        return value


@dataclass(frozen=True)
class _Tool:
    arguments: type[BaseModel]
    description: str
    action: str | None = None
    outcome: str = "state_change_verified"


_TOOLS = MappingProxyType(
    {
        "create_handoff": _Tool(
            CreateHandoffArguments,
            "Persist triage and route to a human; queued or assigned is not accepted or resolved.",
        ),
        "get_handoff": _Tool(
            GetHandoffArguments, "Read an owned persisted handoff and its lifecycle."
        ),
        "get_cards": _Tool(NoArguments, "List the authenticated customer's historical cards."),
        "get_card": _Tool(CardArguments, "Read one owned card and its simulated state."),
        "get_movements": _Tool(
            MovementArguments, "Read bounded historical movements of an owned card."
        ),
        "block_card": _Tool(ActionArguments, "Simulate blocking an owned card.", "block"),
        "pause_card": _Tool(ActionArguments, "Simulate pausing an owned card.", "pause"),
        "reactivate_card": _Tool(
            ActionArguments, "Simulate reactivating an eligible owned card.", "reactivate"
        ),
        "activate_card": _Tool(
            ActionArguments, "Simulate activating an eligible owned card.", "activate"
        ),
        "request_replacement": _Tool(
            ActionArguments,
            "Register a simulated replacement request; no issuance or shipping.",
            "replacement",
            "replacement_request_registered",
        ),
        "register_unrecognized_charge": _Tool(
            ChargeArguments,
            "Register an owned historical charge for review; no refund or fraud decision.",
            "unrecognized-charge",
            "request_registered_for_human_review",
        ),
    }
)

_ERRORS = {
    "unauthenticated": "A valid backend session is required.",
    "invalid_tool": "Unknown tool.",
    "invalid_arguments": "Arguments do not match the tool contract.",
    "not_authorized": "Operation is not authorized.",
    "not_found": "Resource not found.",
    "conflict": "Operation conflicts with current state or idempotency evidence.",
    "rate_limited": "Request rate limit exceeded.",
    "unavailable": "Backend is unavailable; execution is not confirmed.",
    "backend_error": "Execution could not be confirmed.",
    "unverified_result": "Backend did not return matching verified action evidence.",
}


def _failure(code):
    return {"ok": False, "error": {"code": code, "message": _ERRORS[code]}}


class ToolDispatcher:
    """Fixed tool catalog; reauthenticate every invocation, including idempotent replays.

    Synchronous like Store: an async host must offload calls to its worker threadpool.
    The Store dependency is trusted backend code, never caller-supplied tool data.
    """

    def __init__(self, store, handoffs=None):
        self._store = store
        self._handoffs = handoffs if handoffs is not None else HandoffStore(store)

    def catalog(self):
        """Fresh provider-neutral JSON schemas, with no execution context or credentials."""
        return [
            {
                "name": name,
                "description": tool.description,
                "mutating": tool.action is not None or name == "create_handoff",
                "input_schema": tool.arguments.model_json_schema(),
            }
            for name, tool in _TOOLS.items()
        ]

    def execute(self, name: str, arguments: dict, *, context: ExecutionContext | None = None):
        if not isinstance(context, ExecutionContext):
            return _failure("unauthenticated")
        try:
            # Context never supplies a principal: customer scope always comes from Store.session.
            token = context.session_token.get_secret_value()
            if not isinstance(token, str) or not 1 <= len(token) <= 200:
                return _failure("unauthenticated")
            principal = self._store.session(token)
            if not isinstance(principal, dict) or not principal.get("customer_id"):
                return _failure("unauthenticated")
            if not isinstance(name, str) or len(name) > 64 or name not in _TOOLS:
                return _failure("invalid_tool")
            tool = _TOOLS[name]
            if type(arguments) is not dict or len(arguments) > 4:
                return _failure("invalid_arguments")
            try:
                inputs = tool.arguments.model_validate(arguments)
            except ValidationError:
                return _failure("invalid_arguments")
            if tool.action is not None:
                evidence = self._store.action(
                    principal,
                    inputs.product_id,
                    tool.action,
                    inputs.idempotency_key,
                    inputs.transaction_id if isinstance(inputs, ChargeArguments) else None,
                    date.fromisoformat(inputs.process_date)
                    if isinstance(inputs, ChargeArguments)
                    else None,
                )
                return self._verified_action(tool, inputs.product_id, evidence)
            if name == "create_handoff":
                data = self._handoffs.create(token, inputs)
            elif name == "get_handoff":
                data = self._handoffs.get(token, inputs.handoff_id)
            elif name == "get_cards":
                data = self._store.cards(principal)
            elif name == "get_card":
                data = self._store.card(principal, inputs.product_id)
            elif name == "get_movements":
                data = self._store.movements(
                    principal,
                    inputs.product_id,
                    inputs.limit,
                    date.fromisoformat(inputs.before_date) if inputs.before_date else None,
                )
            else:
                return _failure("invalid_tool")
            return {"ok": True, "data": data}
        except HTTPException as exc:
            code = {
                401: "unauthenticated",
                403: "not_authorized",
                404: "not_found",
                409: "conflict",
                422: "invalid_arguments",
                429: "rate_limited",
                503: "unavailable",
            }.get(exc.status_code, "backend_error")
            return _failure(code)
        except Exception:
            # Never log/re-raise exceptions containing arguments, tokens or database details.
            return _failure("backend_error")

    @staticmethod
    def _verified_action(tool, product_id, result):
        try:
            evidence = ActionEvidence.model_validate(result)
        except ValidationError:
            return _failure("unverified_result")
        if (
            evidence.action != tool.action
            or evidence.product_id != product_id
            or evidence.simulated is not True
            or evidence.outcome != tool.outcome
            or (tool.outcome != "state_change_verified" and evidence.request_id is None)
        ):
            return _failure("unverified_result")
        return {
            "ok": True,
            "data": {"verified": True, "evidence": evidence.model_dump(exclude_none=True)},
        }
