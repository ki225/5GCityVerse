"""Adaptive agent with QER mutation authority."""
from __future__ import annotations

from backend.onprem.agents.contracts import AgentLoopBlocked


class ExecutorAgent:
    """Only this component can call the commissioned QER adapter."""

    def __init__(self, adapter):
        self.adapter = adapter

    def apply(self, snapshot: dict, selected: dict, *, model_response_id=None) -> dict:
        link = ({"model_response_id": model_response_id, "candidate_id": selected["id"]}
                if model_response_id is not None else {})
        return self.adapter.execute(snapshot.get("_actuatorSnapshot", snapshot), event_id=selected["eventId"],
                                    treatment_supis=selected["treatmentSupis"],
                                    holdout_supis=selected["holdoutSupis"],
                                    profile_id=selected["profileId"], **link)

    def restore(self, round_id: str) -> None:
        receipt = self.adapter.cleanup(round_id)
        if not isinstance(receipt, dict) or receipt.get("cleanupVerified") is not True:
            raise AgentLoopBlocked("QER restoration is not verified")
