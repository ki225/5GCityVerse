"""Independent proposal, QER, and effect validation."""
from __future__ import annotations

import math
import time
from typing import Callable

from backend.onprem.agents.contracts import AgentLoopBlocked, ScenarioContract, _seconds, _digest
from backend.onprem.scenarios.contracts import SCENARIO_SLICES as EVENT_SLICES


class ValidatorAgent:
    """Checks proposal scope and independent before/after receiver evidence."""

    def __init__(self, contract: ScenarioContract, qer_reader,
                 *, clock: Callable[[], float] = time.time):
        if not callable(qer_reader):
            raise ValueError("Validator requires an independent native QER reader")
        self.contract, self.qer_reader, self.clock = contract, qer_reader, clock

    def observe(self, snapshot: dict) -> dict[str, dict]:
        try:
            age = self.clock() - _seconds(snapshot["observedAt"])
            rows = snapshot["perUe"]
            identity = (snapshot["roundId"], snapshot["generation"], snapshot["sequence"])
        except (KeyError, TypeError, ValueError):
            raise AgentLoopBlocked("Native observation identity is incomplete") from None
        if (not isinstance(identity[0], str) or not identity[0]
                or type(identity[1]) is not int or identity[1] < 0
                or type(identity[2]) is not int or identity[2] < 0
                or not -1 <= age <= self.contract.maximum_evidence_age_seconds
                or not isinstance(rows, list) or len(rows) != self.contract.total_ues):
            raise AgentLoopBlocked("Native observation is stale or incomplete")
        by_supi = {}
        counts = dict.fromkeys(EVENT_SLICES, 0)
        for row in rows:
            if not isinstance(row, dict):
                raise AgentLoopBlocked("Native UE row is invalid")
            supi, event = row.get("supi"), row.get("eventId")
            metric = row.get(self.contract.metric)
            offered = (row.get("offeredUplinkMbps"), row.get("offeredDownlinkMbps"))
            if (not isinstance(supi, str) or not supi or supi in by_supi
                    or event not in EVENT_SLICES or row.get("slice") != EVENT_SLICES[event]
                    or row.get("requestedSlice") not in set(EVENT_SLICES.values())
                    or type(metric) not in (int, float) or not math.isfinite(metric) or metric < 0
                    or any(type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0
                           for rate in offered)):
                raise AgentLoopBlocked("Native UE binding or receiver metric is invalid")
            by_supi[supi] = row
            counts[event] += 1
        if counts != self.contract.group_counts:
            raise AgentLoopBlocked("Native event membership differs from the contract")
        return by_supi

    def proposal(self, decision: dict, candidates: list[dict], rows: dict[str, dict]) -> dict | None:
        selected = next((item for item in candidates if item.get("id") == decision["candidateId"]), None)
        if decision["candidateId"] is None:
            return None
        if selected is None or set(selected) != {"id", "eventId", "profileId", "treatmentSupis", "holdoutSupis"}:
            raise AgentLoopBlocked("Candidate is not in the commissioned set")
        event = selected["eventId"]
        treatment, holdout = selected["treatmentSupis"], selected["holdoutSupis"]
        cohort = {supi for supi, row in rows.items() if row["eventId"] == event}
        if (event not in EVENT_SLICES or selected["profileId"] not in self.contract.profiles
                or not isinstance(treatment, list)
                or not isinstance(holdout, list) or not treatment
                or (not holdout and len(cohort) != 1)
                or len(treatment + holdout) != len(set(treatment + holdout))
                or set(treatment + holdout) != cohort):
            raise AgentLoopBlocked("Candidate cohort or profile differs from native identity")
        return selected

    def qer_inventory(self, snapshot: dict, *, sealed_rows=None) -> dict[str, dict]:
        proof = self.qer_reader(snapshot)
        try:
            age = self.clock() - _seconds(proof["observedAt"])
            sessions = proof["sessions"]
        except (KeyError, TypeError, ValueError):
            raise AgentLoopBlocked("Native QER evidence is incomplete") from None
        if (proof.get("source") != "native-upf-qer" or not -1 <= age <= self.contract.maximum_evidence_age_seconds
                or not isinstance(sessions, list)):
            raise AgentLoopBlocked("Native QER evidence is unavailable or stale")
        if sealed_rows is None:
            expected = self.observe(snapshot)
        else:
            # The graph already replayed the decision-time observation seal.
            # Its cohort may be historical; this native QER proof must be fresh.
            if (not isinstance(sealed_rows, dict) or len(sealed_rows) != self.contract.total_ues
                    or _digest(list(sealed_rows.values())) != _digest(snapshot.get('perUe'))
                    or any(key != row.get('supi') for key, row in sealed_rows.items())):
                raise AgentLoopBlocked('Sealed QER cohort differs from the accepted observation')
            expected = sealed_rows
        actual = {}
        for session in sessions:
            if not isinstance(session, dict):
                raise AgentLoopBlocked("Native QER session is invalid")
            supi = session.get("supi")
            qers = session.get("qers")
            if (supi not in expected or supi in actual or session.get("slice") != expected[supi]["slice"]
                    or any(not isinstance(session.get(key), str) or not session[key]
                           for key in ("sessionId", "pfcpSeid", "upf"))
                    or not isinstance(qers, list) or not qers
                    or len({q.get("id") for q in qers if isinstance(q, dict)}) != len(qers)
                    or any(not isinstance(q, dict) or type(q.get("id")) is not int
                           or not isinstance(q.get("pdrIds"), list) or not q["pdrIds"]
                           or any(type(pdr) is not int for pdr in q["pdrIds"])
                           or not isinstance(q.get("mbrKbps"), dict)
                           or set(q["mbrKbps"]) != {"ul", "dl"}
                           or any(type(rate) is not int or not 0 <= rate <= 500_000
                                  for rate in q["mbrKbps"].values())
                           for q in qers)):
                raise AgentLoopBlocked("Native QER identity or fields are invalid")
            bounded = [q for q in qers if any(q["mbrKbps"].values())]
            if len(bounded) != 1 or any(rate <= 0 for rate in bounded[0]["mbrKbps"].values()):
                raise AgentLoopBlocked("Native AMBR QER target is ambiguous")
            actual[supi] = session
        if set(actual) != set(expected):
            raise AgentLoopBlocked("Native QER cohort differs from the round")
        return actual

    def installation(self, receipt: dict, selected: dict,
                     before: dict[str, dict], after: dict[str, dict]) -> None:
        if (not isinstance(receipt, dict) or receipt.get("state") != "installed"
                or receipt.get("installed") is not True
                or receipt.get("eventId") != selected["eventId"]
                or receipt.get("profileId") != selected["profileId"]
                or receipt.get("treatmentSupis") != sorted(selected["treatmentSupis"])
                or receipt.get("holdoutSupis") != sorted(selected["holdoutSupis"])):
            raise AgentLoopBlocked("Independent QER installation is unverified")
        if set(before) != set(after):
            raise AgentLoopBlocked("Native QER cohort changed during installation")
        treatment = set(selected["treatmentSupis"])
        expected_rates = self.contract.profiles[selected["profileId"]]
        for supi, old in before.items():
            new = after[supi]
            if ({key: old.get(key) for key in ("supi", "slice", "sessionId", "pfcpSeid", "upf")}
                    != {key: new.get(key) for key in ("supi", "slice", "sessionId", "pfcpSeid", "upf")}):
                raise AgentLoopBlocked("Native QER session identity changed")
            original = sorted(old["qers"], key=lambda q: q["id"])
            installed = sorted(new["qers"], key=lambda q: q["id"])
            targets = [q["id"] for q in original if any(q["mbrKbps"].values())]
            if len(targets) != 1:
                raise AgentLoopBlocked("Native AMBR QER target is ambiguous")
            if ([(q["id"], q["pdrIds"]) for q in original]
                    != [(q["id"], q["pdrIds"]) for q in installed]
                    or any(q["mbrKbps"] != (expected_rates if supi in treatment and q["id"] == targets[0]
                                            else prior["mbrKbps"])
                           for prior, q in zip(original, installed))):
                raise AgentLoopBlocked("Independent native QER readback differs from the action")

    def effect(self, before: dict, after: dict, selected: dict) -> tuple[str, float | None]:
        return self._effect_rows(before, after, selected, self.observe(before))

    def _effect_rows(self, before: dict, after: dict, selected: dict, baseline: dict) -> tuple[str, float | None]:
        # Graph alone supplies rows from its immutable, freshly validated before seal.
        # This helper grants no execution authority; result freshness is always checked.
        result = self.observe(after)
        if ((before["roundId"], before["generation"]) != (after["roundId"], after["generation"])
                or after["sequence"] <= before["sequence"]
                or set(baseline) != set(result)
                or any((baseline[s]["eventId"], baseline[s]["slice"], baseline[s]["requestedSlice"])
                       != (result[s]["eventId"], result[s]["slice"], result[s]["requestedSlice"])
                       for s in baseline)
                or any(abs(result[s][direction] - baseline[s][direction])
                       > .05 * baseline[s][direction]
                       for s in baseline for direction in ("offeredUplinkMbps", "offeredDownlinkMbps"))):
            raise AgentLoopBlocked("Result does not belong to the baseline UE cohort")
        metric = self.contract.metric
        def improvement(group: list[str]) -> float:
            first = sum(baseline[s][metric] for s in group) / len(group)
            second = sum(result[s][metric] for s in group) / len(group)
            if first <= 0:
                raise AgentLoopBlocked("Relative effect requires a positive baseline")
            sign = 1 if self.contract.direction == "increase" else -1
            return sign * (second - first) / first * 100
        if not selected["holdoutSupis"]:
            return "no_holdout", None
        target = improvement(selected["treatmentSupis"])
        control = improvement(selected["holdoutSupis"])
        if control < -self.contract.holdout_tolerance_percent:
            return "regressed", target - control
        net = target - control
        if net >= self.contract.minimum_improvement_percent:
            return "effect_verified", net
        return "no_effect", net
