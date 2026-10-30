"""Default-off commissioning admission with strict trusted startup configuration.

Construct once from trusted operator configuration, never from an HTTP request.
An admitted candidate is an experiment, not evidence of verified capacity. This
module performs no automatic promotion and starts no workers or UE workloads.
"""
from dataclasses import dataclass

from backend.onprem.traffic.planning import PROFILE_ID
from backend.onprem.scenarios.contracts import MAX_ROUND_UES

CANDIDATE_LIMITS = frozenset((5, 10, 25, 50, 100, 250, 500))


def calibration_from_environment(environment):
    """Parse trusted process configuration once at startup, never HTTP input."""
    flag = environment.get("CITYVERSE_ONPREM_COMMISSIONING_ENABLED", "false")
    candidate = environment.get("CITYVERSE_ONPREM_CANDIDATE_MAX_UES")
    if flag not in ("true", "false"):
        raise CalibrationBlocked("Commissioning operator flag must be exactly true or false")
    if flag == "false":
        if candidate is not None:
            raise CalibrationBlocked("Candidate configuration requires commissioning opt-in")
        return CalibrationAdmission()
    if candidate not in {str(limit) for limit in CANDIDATE_LIMITS}:
        raise CalibrationBlocked("Commissioning candidate configuration is missing or invalid")
    return CalibrationAdmission(True, int(candidate))


class CalibrationBlocked(ValueError):
    pass


@dataclass(frozen=True)
class CalibrationAdmission:
    """Operator-owned immutable limits, distinct from caller-supplied demand."""

    enabled: bool = False
    candidate_max_ues: int | None = None

    @property
    def verified_max_ues(self):
        # A commissioning candidate is unverified until its live receipts pass.
        return 0 if self.enabled else 1

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise CalibrationBlocked("Commissioning requires an explicit boolean operator flag")
        if self.enabled:
            if type(self.candidate_max_ues) is not int or self.candidate_max_ues not in CANDIDATE_LIMITS:
                raise CalibrationBlocked("Candidate limit must be an explicit operator choice: 5, 10, 25, 50, 100, 250 or 500")
        elif self.candidate_max_ues is not None:
            raise CalibrationBlocked("Candidate limit cannot be configured without commissioning opt-in")

    def capabilities(self):
        return {
            "applicationMaxUes": MAX_ROUND_UES,
            "verifiedMaxUes": self.verified_max_ues,
            "verifiedMaxUesScope": "scenario-offered-load-v1",
            "commissioningEnabled": self.enabled,
            "candidateMaxUes": self.candidate_max_ues,
            "admissionMaxUes": self.candidate_max_ues if self.enabled else self.verified_max_ues,
            "commissioningProfile": PROFILE_ID if self.enabled else None,
            "commissioningLabel": "Unverified commissioning" if self.enabled else None,
            "staticPolicy": False,
            "aiPolicy": False,
        }

    def admit(self, *, total_ues, strategy, profile_id):
        """Check validated demand against the fixed operator contract.

        The caller still must run the fixed planner and all native, ownership,
        freshness and cleanup gates. This receipt is NOT worker authorization
        or a native evidence record, and cannot promote verifiedMaxUes.
        """
        if type(total_ues) is not int or not 0 <= total_ues <= MAX_ROUND_UES:
            raise CalibrationBlocked("Actual shared UE demand must be an integer from 0 to 500")
        if strategy != "none":
            raise CalibrationBlocked("Commissioning admits NONE strategy only")
        if profile_id != PROFILE_ID:
            raise CalibrationBlocked("Only the fixed scenario offered-load profile is admitted")
        limit = self.candidate_max_ues if self.enabled else self.verified_max_ues
        if total_ues > limit:
            raise CalibrationBlocked("Requested UE demand exceeds the operator admission limit")
        return {
            "totalUes": total_ues,
            "profileId": PROFILE_ID,
            "strategy": "none",
            "capacityStatus": "unverified_commissioning" if total_ues > self.verified_max_ues else "within_verified_limit",
            "verifiedMaxUes": self.verified_max_ues,
            "admissionMaxUes": limit,
            "nativeEvidenceRequired": True,
        }
