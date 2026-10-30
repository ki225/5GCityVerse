"""Shared contract and identity helpers for adaptive agents."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any

from backend.onprem.scenarios.contracts import EVENT_SLICES


class AgentLoopBlocked(RuntimeError):
    """A required identity, observation, action, or restoration was not proven."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _seconds(value: Any) -> float:
    if type(value) in (int, float) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.timestamp()
        except ValueError:
            pass
    raise ValueError("Observation timestamp is invalid")


@dataclass(frozen=True)
class ScenarioContract:
    """Predeclared experiment scope; the UE count is data, never a code constant."""

    scenario_id: str
    event_counts: dict[str, int]
    metric: str
    direction: str
    minimum_improvement_percent: float
    holdout_tolerance_percent: float
    profiles: dict[str, dict[str, int]]
    maximum_evidence_age_seconds: int = 120
    maximum_policy_changes: int = 3
    minimum_cycles_between_changes: int = 1
    baseline_ues: int = 0
    traffic_mode: str = "functional"
    service_requirements_revision: str | None = None

    def __post_init__(self) -> None:
        if self.service_requirements_revision is not None and (not isinstance(self.service_requirements_revision, str)
                or len(self.service_requirements_revision) != 64 or any(c not in '0123456789abcdef' for c in self.service_requirements_revision)):
            raise ValueError("Invalid service requirement revision")
        if (self.traffic_mode not in ("functional", "qos")
                or not self.scenario_id or set(self.event_counts) != set(EVENT_SLICES)
                or any(type(n) is not int or n < 0 for n in self.event_counts.values())
                or type(self.baseline_ues) is not int or self.baseline_ues < 0
                or not 1 <= self.total_ues <= 100
                or self.metric not in {"receivedUplinkMbps", "receivedDownlinkMbps", "rttMs", "lossPercent"}
                or self.direction not in {"increase", "decrease"}
                or type(self.minimum_improvement_percent) not in (int, float)
                or not math.isfinite(self.minimum_improvement_percent)
                or self.minimum_improvement_percent <= 0
                or type(self.holdout_tolerance_percent) not in (int, float)
                or not math.isfinite(self.holdout_tolerance_percent)
                or self.holdout_tolerance_percent < 0
                or not isinstance(self.profiles, dict) or not self.profiles
                or any(not isinstance(name, str) or not name or not isinstance(rates, dict)
                       or set(rates) != {"ul", "dl"}
                       or any(type(rate) is not int or rate <= 0 for rate in rates.values())
                       for name, rates in self.profiles.items())
                or type(self.maximum_evidence_age_seconds) is not int
                or not 1 <= self.maximum_evidence_age_seconds <= 120
                or type(self.maximum_policy_changes) is not int or self.maximum_policy_changes < 0
                or type(self.minimum_cycles_between_changes) is not int
                or self.minimum_cycles_between_changes < 1):
            raise ValueError("Invalid predeclared adaptive scenario contract")
        object.__setattr__(self, "event_counts", MappingProxyType(dict(self.event_counts)))
        object.__setattr__(self, "profiles", MappingProxyType({
            name: MappingProxyType(dict(rates)) for name, rates in self.profiles.items()}))

    @property
    def total_ues(self) -> int:
        return self.baseline_ues + sum(self.event_counts.values())

    @property
    def group_counts(self) -> dict[str, int]:
        return {"web_browsing": self.baseline_ues, **self.event_counts}

    @property
    def digest(self) -> str:
        return _digest({"scenarioId": self.scenario_id, "eventCounts": dict(self.event_counts),
                        "baselineUes": self.baseline_ues,
                        "metric": self.metric, "direction": self.direction,
                        "minimumImprovementPercent": self.minimum_improvement_percent,
                        "holdoutTolerancePercent": self.holdout_tolerance_percent,
                        "profiles": {name: dict(rates) for name, rates in self.profiles.items()},
                        "maximumEvidenceAgeSeconds": self.maximum_evidence_age_seconds,
                        **({"trafficMode": "qos"} if self.traffic_mode == "qos" else {}),
                        **({"serviceRequirementsRevision": self.service_requirements_revision} if self.service_requirements_revision else {})})
