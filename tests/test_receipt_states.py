"""Sprint R3: verified evidence must describe the state implied by its action."""

import pytest

from factored_bck.evidence import verified_action_evidence


@pytest.mark.parametrize(
    "action,expected",
    [
        ("pause", "PAUSED"),
        ("block", "BLOCKED"),
        ("activate", "ACTIVE"),
        ("reactivate", "ACTIVE"),
    ],
)
def test_state_changing_receipts_reject_contradictions_and_allow_valid_replays(action, expected):
    receipt = {
        "action_id": "a" * 32,
        "product_id": "team-card",
        "action": action,
        "status": "succeeded",
        "outcome": "state_change_verified",
        "simulator_state": expected,
        "simulated": True,
        "source_kind": "team_synthetic",
        "release_id": "team-release",
    }
    assert (
        verified_action_evidence(action, "team-card", "state_change_verified", receipt) == receipt
    )
    assert (
        verified_action_evidence(action, "team-card", "state_change_verified", receipt) == receipt
    )
    wrong = "ACTIVE" if expected != "ACTIVE" else "PAUSED"
    with pytest.raises(ValueError, match="unverified_action_result"):
        verified_action_evidence(
            action, "team-card", "state_change_verified", receipt | {"simulator_state": wrong}
        )
