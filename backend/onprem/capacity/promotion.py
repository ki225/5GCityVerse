"""Derive a capacity grant from complete native receipts for the live runtime."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from backend.onprem.capacity.receipts import inspect_manifest
from backend.onprem.traffic.planning import PROFILE_ID


PROMOTION_SCOPE = "scenario-offered-load-v1/exact-runtime-and-receipt-set"


class CapacityPromotionRejected(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedCapacityGrant:
    verified_max_ues: int
    profile_id: str
    scope: str
    manifest_sha256: str
    runtime_fingerprint: str
    evidence_id: str


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CapacityPromotionRejected("Duplicate manifest field")
        result[key] = value
    return result


def verify_capacity_promotion(*, manifest_bytes, read_receipt,
                              expected_runtime_fingerprint):
    """Accept complete, capacity-qualified receipts for the live images.

    The manifest hashes every result, UI and cleanup receipt. The caller must
    re-read it before each admission; this function grants no lasting authority.
    """
    if not isinstance(manifest_bytes, bytes) or not 0 < len(manifest_bytes) <= 16 * 1024 * 1024:
        raise CapacityPromotionRejected("Capacity manifest bytes are invalid")
    if (not isinstance(expected_runtime_fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_runtime_fingerprint)):
        raise CapacityPromotionRejected("Live runtime digest is invalid")
    try:
        manifest = json.loads(manifest_bytes, object_pairs_hook=_unique,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        runtime = manifest["runtime"]
        runtime_fingerprint = hashlib.sha256(json.dumps(
            runtime, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    except (UnicodeError, ValueError, TypeError, KeyError, AttributeError):
        raise CapacityPromotionRejected("Capacity manifest is invalid") from None
    if runtime_fingerprint != expected_runtime_fingerprint:
        raise CapacityPromotionRejected("Capacity runtime scope does not match the live runtime")
    try:
        inspection = inspect_manifest(manifest_bytes, read_receipt)
    except Exception:
        raise CapacityPromotionRejected("Capacity receipt set is invalid") from None
    if (not inspection.integrity_verified or not inspection.coverage_complete
            or not inspection.capacity_criteria_checked):
        raise CapacityPromotionRejected("Capacity evidence is incomplete or out of scope")
    return VerifiedCapacityGrant(inspection.candidate_ceiling, PROFILE_ID, PROMOTION_SCOPE,
                                 inspection.manifest_sha256, runtime_fingerprint,
                                 inspection.manifest_sha256)
