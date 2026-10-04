"""Bounded triage and deterministic routing over a historical agent snapshot."""

from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

Reason = Literal[
    "fraud",
    "complaint",
    "technical_support",
    "card_activation",
    "replacement",
    "card_support",
    "other",
]
Severity = Literal["low", "medium", "high", "critical"]
Experience = Literal["Junior", "Mid-Senior", "Senior", "Specialist"]
Specialty = Literal[
    "Fraudes",
    "Retención",
    "Cobranza",
    "Soporte Técnico",
    "Créditos",
    "Inversiones",
    "Ventas",
    "Quejas y Reclamos",
]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")]
HandoffId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Text = Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]
LEVELS = get_args(Experience)
SEVERITIES = get_args(Severity)
REASONS = get_args(Reason)
MANDATORY_SPECIALTY = {
    "fraud": "Fraudes",
    "complaint": "Quejas y Reclamos",
    "technical_support": "Soporte Técnico",
}
LANGUAGES = {"es": "español", "en": "inglés", "pt": "portugués"}
LIMITATIONS = [
    "Simulator handoff; assignment does not establish agent acceptance or resolution",
    "Monthly agent snapshot: Active and shift do not establish live availability",
    "Historical interactions are not live queue load and are not used for ranking",
    "Premium uplift is a simulated policy, not an official SLA",
    "Model context is untrusted; only backend evidence establishes simulated facts",
    "No refund, reversal, card issuance, shipping or fraud decision is asserted",
]


class Triage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    reason: Reason
    severity: Severity
    required_specialty: Specialty | None  # Explicit null means no specialty requirement.
    minimum_experience: Experience
    language: Literal["es", "en", "pt"]
    summary: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    context: str = Field(default="", max_length=2000)
    unresolved_questions: list[Text] = Field(default_factory=list, max_length=10)
    product_id: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"\S")

    @model_validator(mode="after")
    def compatible_specialty(self):
        required = MANDATORY_SPECIALTY.get(self.reason)
        if required and self.required_specialty != required:
            raise ValueError("reason_requires_specialty")
        return self


class CreateHandoffArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    triage: Triage
    idempotency_key: IdempotencyKey


class GetHandoffArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    handoff_id: HandoffId


def route(triage: Triage, segment: str | None, agents: list[dict], senior_fallback=()):
    """Agents are provisioned accounts from one accepted release; never live presence."""
    severity = triage.severity
    if triage.reason == "fraud" and SEVERITIES.index(severity) < 2:
        severity = "high"
    minimum = LEVELS.index(triage.minimum_experience)
    floor = max(SEVERITIES.index(severity), minimum)
    preferred = min(3, floor + (segment == "Premium"))
    eligible = [
        agent
        for agent in agents
        if agent["agent_status"] == "Active"
        and agent["agent_type"] in ("Digital", "Hybrid")
        and LANGUAGES[triage.language]
        in {value.strip().casefold() for value in (agent["languages"] or "").split(",")}
        and (triage.required_specialty is None or agent["specialty"] == triage.required_specialty)
        and agent["experience_level"] in LEVELS
    ]
    candidates = [a for a in eligible if LEVELS.index(a["experience_level"]) >= floor]
    fallback = False
    if not candidates and severity == "critical" and minimum <= 2:
        if triage.reason in senior_fallback:
            candidates = [a for a in eligible if a["experience_level"] == "Senior"]
            fallback = bool(candidates)
    # Prefer uplift when possible; otherwise keep the safe floor without losing coverage.
    preferred_candidates = [
        a for a in candidates if LEVELS.index(a["experience_level"]) >= preferred
    ]
    ranked = sorted(
        preferred_candidates or candidates,
        key=lambda a: (
            LEVELS.index(a["experience_level"]),
            a["avg_csat"] is None,
            -a["avg_csat"] if a["avg_csat"] is not None else 0,
            a["agent_id"],
        ),
    )
    agent = ranked[0] if ranked else None
    return {
        "severity": severity,
        "required_level": LEVELS[floor],
        "preferred_level": LEVELS[preferred],
        "service_priority": {
            "severity_rank": SEVERITIES.index(severity),
            "premium_uplift": segment == "Premium",
            "simulated_policy": True,
        },
        "assigned_agent_id": agent["agent_id"] if agent else None,
        "assignment_status": "assigned" if agent else "unassigned",
        "queue": None
        if agent
        else "critical_review"
        if severity == "critical"
        else "manual_review",
        "manual_routing_required": agent is None,
        "fallback_used": fallback,
        "policy_version": "digital-handoff-v1",
        "explanation": "explicit_senior_fallback"
        if fallback
        else "eligible_snapshot_agent"
        if agent
        else "no_eligible_provisioned_agent",
        "ranking": [
            "preferred_experience_then_closest_suitable",
            "csat_desc_null_last",
            "agent_id",
        ],
        "selected_experience": agent["experience_level"] if agent else None,
        "critical_senior_fallback_reasons": sorted(senior_fallback),
    }
