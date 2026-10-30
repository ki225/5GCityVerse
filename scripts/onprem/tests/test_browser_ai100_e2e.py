"""Offline AI100 admission tests; no browser or live round is started."""
import json

import pytest

from scripts.onprem.run_browser_ai100_e2e import case, main, preflight_errors


def baseline():
    return {"stage": "completed", "cleanupStatus": "verified",
            "releaseProof": {key: True for key in ("trafficStopped", "policiesAbsent",
                "rulesRestored", "uesReleased", "podsAbsent")},
            "capabilities": {"start": True, "verifiedMaxUes": 100,
                "admissionMaxUes": 100, "commissioningEnabled": False,
                "aiPolicy": True, "capacityStatus": "receipt_verified", "staticPolicy": False}}


def test_ai100_plan_is_non_mutating_and_exact(tmp_path):
    output = tmp_path / "plan"
    main(["--output", str(output), "--event", "video_streaming"])
    plan = json.loads((output / "plan.json").read_text())
    assert plan["status"] == "planned" and plan["verified"] is False
    assert plan["intent"] == case("video_streaming")
    assert plan["intent"]["baselineUes"] == 0
    assert sum(plan["intent"]["events"].values()) == 100


@pytest.mark.parametrize("field", ["aiPolicy", "verifiedMaxUes", "commissioningEnabled",
                                   "capacityStatus", "admissionMaxUes"])
def test_unverified_or_commissioning_capability_rejected(field):
    snapshot = baseline()
    snapshot["capabilities"][field] = None
    assert preflight_errors(snapshot)


def test_unreleased_previous_round_rejected():
    snapshot = baseline()
    snapshot["releaseProof"]["podsAbsent"] = False
    assert preflight_errors(snapshot)


def test_owned_active_round_checks_capability_without_previous_release():
    snapshot = baseline()
    snapshot['stage'] = 'registering'
    snapshot['releaseProof'] = None
    assert preflight_errors(snapshot)
    assert preflight_errors(snapshot, require_released=False) == []


def test_execute_requires_existing_private_journal_before_browser_or_post(tmp_path):
    output = tmp_path / "live"
    with pytest.raises(SystemExit):
        main(["--output", str(output), "--execute"])
    assert not output.exists()
