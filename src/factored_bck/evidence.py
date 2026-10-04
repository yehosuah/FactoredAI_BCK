"""Allowlisted committed simulator receipts shared by tools and handoffs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ActionEvidence(BaseModel):
    # Allowlist output fields; never pass through unexpected store metadata or credentials.
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    action_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    product_id: str = Field(min_length=1, max_length=100, pattern=r"\S")
    action: str
    status: Literal["succeeded"]
    outcome: Literal[
        "state_change_verified",
        "replacement_request_registered",
        "request_registered_for_human_review",
    ]
    simulator_state: Literal[
        "ACTIVE", "PAUSED", "PENDING_ACTIVATION", "BLOCKED", "CLOSED", "INELIGIBLE"
    ]
    simulated: bool
    source_kind: Literal["team_synthetic", "organizer_synthetic"]
    release_id: str = Field(min_length=1, max_length=200)
    request_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


def verified_action_evidence(action, product_id, outcome, result):
    """Validate the same action receipt contract for tools and confirmation execution."""
    evidence = ActionEvidence.model_validate(result)
    if (
        evidence.action != action
        or evidence.product_id != product_id
        or evidence.simulated is not True
        or evidence.outcome != outcome
        or (outcome != "state_change_verified" and evidence.request_id is None)
    ):
        raise ValueError("unverified_action_result")
    return evidence.model_dump(exclude_none=True)
