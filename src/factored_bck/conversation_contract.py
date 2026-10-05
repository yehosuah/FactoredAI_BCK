"""Versioned engineering contract: adapter proposals carry no execution authority."""

from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)

from factored_bck.handoff import Triage
from factored_bck.tools import CardArguments, DateString, MovementArguments, NoArguments

CONTRACT_VERSION = "conversation-adapter-v1"
Language = Literal["es", "pt"]
Identifier = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Key = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")]
Text = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class CreateConversation(Contract):
    language: Language


class SubmitTurn(Contract):
    message: Text
    language: Language | None = None


class AdapterInfo(Contract):
    provider: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=100)
    mode: Literal["stub", "injected", "disabled"]


class ContextMessage(Contract):
    role: Literal["user", "assistant"]
    text: Text


class AdapterContext(Contract):
    contract_version: Literal["conversation-adapter-v1"] = CONTRACT_VERSION
    language: Language
    messages: tuple[ContextMessage, ...]
    history_truncated: bool


class Answer(Contract):
    kind: Literal["answer"]
    text: Text


class Clarification(Contract):
    kind: Literal["clarification"]
    question: Text


class ChargeProposal(CardArguments):
    transaction_id: str = Field(min_length=1, max_length=30, pattern=r"\S")
    process_date: DateString

    @field_validator("process_date")
    @classmethod
    def valid_date(cls, value):
        date.fromisoformat(value)
        return value


MUTATING_TOOLS = frozenset(
    (
        "pause_card",
        "block_card",
        "reactivate_card",
        "activate_card",
        "request_replacement",
        "register_unrecognized_charge",
    )
)
PROPOSAL_ARGUMENTS = {
    "get_cards": NoArguments,
    "get_card": CardArguments,
    "get_movements": MovementArguments,
    **{name: CardArguments for name in MUTATING_TOOLS},
    "register_unrecognized_charge": ChargeProposal,
}


class ToolRequest(Contract):
    kind: Literal["tool_request"]
    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, JsonValue] = Field(max_length=3)

    @model_validator(mode="after")
    def allowed_arguments(self):
        schema = PROPOSAL_ARGUMENTS.get(self.name)
        if schema is None:
            raise ValueError("unsupported_tool")
        schema.model_validate(self.arguments)
        return self


class HumanHandoff(Contract):
    kind: Literal["human_handoff"]
    triage: Triage


Proposal = Annotated[
    Answer | Clarification | ToolRequest | HumanHandoff, Field(discriminator="kind")
]
PROPOSAL = TypeAdapter(Proposal)


class ConversationEvent(Contract):
    adapter: AdapterInfo
    event_id: Identifier
    turn_id: Identifier
    sequence: int
    kind: Literal[
        "user_message",
        "answer",
        "clarification",
        "tool_result",
        "confirmation_prepared",
        "confirmation_status",
        "handoff_created",
        "handoff_status",
        "error",
    ]
    trust: Literal["untrusted", "backend"]
    data: dict[str, JsonValue]
    confirmation_id: Identifier | None
    handoff_id: Identifier | None
    created_at: str


class ConversationState(Contract):
    contract_version: Literal["conversation-adapter-v1"] = CONTRACT_VERSION
    conversation_id: Identifier
    language: Language
    created_at: str
    updated_at: str
    adapter: AdapterInfo
    submitted_turn_id: Identifier | None = None
    events: list[ConversationEvent]
    next_after: int | None
    last_sequence: int
