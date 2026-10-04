"""Policy tests over explicit historical snapshots, without transport/database details."""

from itertools import permutations

import pytest
from pydantic import ValidationError

from factored_bck.handoff import LEVELS, Triage, route


def triage(**changes):
    return Triage.model_validate(
        {
            "reason": "card_support",
            "severity": "low",
            "required_specialty": None,
            "minimum_experience": "Junior",
            "language": "es",
            "summary": "Need help",
            **changes,
        }
    )


def agent(level="Junior", **changes):
    return {
        "agent_id": level,
        "agent_type": "Digital",
        "experience_level": level,
        "agent_status": "Active",
        "languages": "español, inglés",
        "specialty": None,
        "avg_csat": 4.2,
        **changes,
    }


@pytest.mark.parametrize(
    "severity,floor", list(zip(("low", "medium", "high", "critical"), LEVELS, strict=True))
)
@pytest.mark.parametrize("segment", ["Basic", "Student", "Plus", "Premium", None])
def test_severity_floor_and_soft_premium_uplift(severity, floor, segment):
    agents = [agent(level) for level in LEVELS]
    result = route(triage(severity=severity), segment, agents)
    expected = LEVELS[min(3, LEVELS.index(floor) + (segment == "Premium"))]
    assert result["required_level"] == floor
    assert result["selected_experience"] == expected
    assert result["service_priority"]["severity_rank"] == LEVELS.index(floor)
    assert result["service_priority"]["premium_uplift"] == (segment == "Premium")
    # Lack of the preferred uplift does not discard an otherwise safe candidate.
    assert route(triage(severity=severity), segment, [agent(floor)])["assigned_agent_id"] == floor


def test_explicit_minimum_is_never_lowered_and_critical_fallback_is_recorded():
    critical = triage(severity="critical")
    result = route(critical, "Basic", [agent("Senior")], ["card_support"])
    assert result["fallback_used"]
    assert result["required_level"] == "Specialist"
    assert result["selected_experience"] == "Senior"
    assert result["explanation"] == "explicit_senior_fallback"
    assert not route(critical, "Basic", [agent("Senior"), agent("Specialist")], ["card_support"])[
        "fallback_used"
    ]
    for policy in [(), ("replacement",)]:
        result = route(critical, "Basic", [agent("Senior")], policy)
        assert result["queue"] == "critical_review"
        assert result["manual_routing_required"]
        assert result["assigned_agent_id"] is None
    assert (
        route(
            triage(severity="critical", minimum_experience="Specialist"),
            "Premium",
            [agent("Senior")],
            ["card_support"],
        )["assigned_agent_id"]
        is None
    )
    assert (
        route(critical, "Premium", [agent("Junior")], ["card_support"])["assigned_agent_id"] is None
    )
    assert (
        route(triage(minimum_experience="Senior"), "Basic", [agent()])["assigned_agent_id"] is None
    )


@pytest.mark.parametrize(
    "change",
    [
        {"agent_status": "Inactive"},
        {"agent_status": "Vacation"},
        {"agent_status": "Leave"},
        {"agent_type": "Phone"},
        {"agent_type": "In-Person"},
        {"languages": "portugués"},
        {"experience_level": "Unknown"},
        {"specialty": None},
        {"specialty": "Ventas"},
    ],
)
def test_filters_exclude_unsafe_candidates(change):
    request = triage(reason="fraud", severity="low", required_specialty="Fraudes")
    result = route(request, "Premium", [agent("Specialist", specialty="Fraudes") | change])
    assert result["assigned_agent_id"] is None
    assert result["severity"] == "high"
    assert result["required_level"] == "Senior"


def test_exact_language_tokens_hybrid_and_no_specialty_requirement():
    assert (
        route(
            triage(language="pt"),
            None,
            [agent(agent_type="Hybrid", languages="español, português")],
        )["assigned_agent_id"]
        is None
    )
    assert (
        route(
            triage(language="pt"),
            None,
            [agent(agent_type="Hybrid", languages="español, portugués")],
        )["assigned_agent_id"]
        == "Junior"
    )
    assert route(triage(), None, [agent(specialty=None)])["assigned_agent_id"] == "Junior"


def test_deterministic_closest_level_csat_then_id_without_weights_or_monthly_load():
    agents = [
        agent(agent_id="c", avg_csat=None),
        agent(agent_id="b", avg_csat=4.8),
        agent(agent_id="a", avg_csat=4.8),
        agent("Senior", avg_csat=5),
    ]
    for order in permutations(agents):
        result = route(triage(), None, list(order))
        assert result["assigned_agent_id"] == "a"
    assert (
        route(
            triage(), None, [agent(agent_id="b", avg_csat=None), agent(agent_id="a", avg_csat=None)]
        )["assigned_agent_id"]
        == "a"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"severity": "urgent"},
        {"minimum_experience": "junior"},
        {"required_specialty": "banking"},
        {"required_specialty": ""},
        {"language": "fr"},
        {"summary": ""},
        {"summary": "x" * 2001},
        {"context": "x" * 2001},
        {"unresolved_questions": ["q"] * 11},
        {"unresolved_questions": ["q" * 501]},
        {"customer_id": "forged"},
        {"agent_id": "forged"},
        {"verified_evidence": {}},
        {"service_priority": 9},
        {"severity": 1},
        {"summary": True},
        {"reason": "fraud"},
        {"reason": "complaint"},
        {"reason": "technical_support"},
    ],
)
def test_bounded_strict_triage_and_mandatory_specialties(change):
    with pytest.raises(ValidationError):
        triage(**change)
