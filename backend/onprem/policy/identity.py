"""Canonical policy identity and immutable fleet lease validation."""
from copy import deepcopy
import json

from backend.onprem.policy.errors import InvalidPolicyDecision

LEASE_IDENTITY_FIELDS = ("supi", "slice", "generation", "podUid", "ip", "sessionId", "interface", "upf",
                         "smContextRef", "pfcpSeid", "dnn", "sst", "sd", "upfEpoch")

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

def lease_identity(snapshot):
    """Return the immutable leased cohort, or None for the legacy exact-fleet path."""
    lease = snapshot.get("fleetLease")
    if lease is None:
        return None
    if (not isinstance(lease, dict)
            or set(lease) != {"generation", "fleetDigest", "supis", "counts", "identities"}
            or type(lease.get("generation")) is not int
            or lease["generation"] != snapshot.get("generation")
            or not isinstance(lease.get("fleetDigest"), str)
            or len(lease["fleetDigest"]) != 64
            or any(c not in "0123456789abcdef" for c in lease["fleetDigest"])
            or not isinstance(lease.get("supis"), list)
             or any(not isinstance(supi, str) or not supi for supi in lease["supis"])
            or lease["supis"] != sorted(lease["supis"])
            or not lease["supis"]
            or len(set(lease["supis"])) != len(lease["supis"])
            or not isinstance(lease.get("counts"), dict)
            or set(lease["counts"]) != {"embb", "urllc", "mmtc", "v2x"}
            or any(type(value) is not int or value < 0 for value in lease["counts"].values())
            or sum(lease["counts"].values()) != len(lease["supis"])
            or not isinstance(lease.get("identities"), list)):
        raise InvalidPolicyDecision("Invalid fleet lease identity")
    identities = lease["identities"]
    if (len(identities) != len(lease["supis"])
            or any(not isinstance(row, dict) or set(row) != set(LEASE_IDENTITY_FIELDS) for row in identities)
            or [row["supi"] for row in identities] != lease["supis"]
            or any(row["generation"] != lease["generation"] for row in identities)
            or {name: sum(row["slice"] == name for row in identities) for name in lease["counts"]} != lease["counts"]):
        raise InvalidPolicyDecision("Fleet lease cohort does not match its immutable identities")
    owned = {row.get("supi"): row for row in snapshot.get("perUe", [])}
    if set(owned) != set(lease["supis"]) or len(owned) != len(snapshot.get("perUe", [])):
        raise InvalidPolicyDecision("Round UE cohort differs from the fleet lease")
    for identity in identities:
        row = owned[identity["supi"]]
        if any(row.get(public) != identity[native] for public, native in
               (("supi", "supi"), ("slice", "slice"), ("podUid", "podUid"), ("ueIp", "ip"),
                ("sessionId", "sessionId"), ("tun", "interface"), ("upf", "upf"),
                ("smContextRef", "smContextRef"), ("pfcpSeid", "pfcpSeid"), ("dnn", "dnn"),
                ("sst", "sst"), ("sd", "sd"), ("upfEpoch", "upfEpoch"))):
            raise InvalidPolicyDecision("Round UE identity differs from the fleet lease")
    return {"generation": lease["generation"], "fleetDigest": lease["fleetDigest"],
            "counts": deepcopy(lease["counts"]), "supis": deepcopy(lease["supis"]),
            "identities": deepcopy(identities)}
