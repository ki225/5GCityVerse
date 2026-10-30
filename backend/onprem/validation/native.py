"""Pure replay of the original UPF QER/PDR/QoS state for a controlled fixture.

The provider obtains these dumps in the same native observation used by its
existing QER proof. This reducer does not grant an SMF write or infer packet
enforcement from installed kernel rules.
"""
from __future__ import annotations

from copy import deepcopy

from backend.onprem.validation import normal as common
from backend.onprem.policy.group_qer import GroupQerBlocked, _ambr_qer


class FixtureNativeBlocked(ValueError):
    pass


PHASES = frozenset({"original", "fixture_installed", "inner_restored", "original_restored"})
_RECEIPT_FIELDS = {"schema", "phase", "roundId", "generation", "fleetLeaseDigest",
                   "cohortSha256", "workloadContractDigest", "observedAt", "native"}


def same_rule_state(left, right):
    """Compare validated QER/PDR state; raw receipts retain every observation clock."""
    def state(value):
        if isinstance(value, dict):
            return {key: state(item) for key, item in value.items() if key != 'observedAt'}
        if isinstance(value, list):
            return [state(item) for item in value]
        return value
    return state(left) == state(right)


def _integer(value, *, minimum=0, maximum=500_000):
    return type(value) is int and minimum <= value <= maximum


def original_snapshot(snapshot, proof, dumps):
    """Pin every owned session's exact original QER, PDR and module QoS flag."""
    try:
        owners = snapshot["perUe"]
        owner_digest = common.cohort_digest(owners)
        expected = {row["supi"]: row for row in owners}
        if (type(proof) is not dict or proof.get("source") != "native-upf-qer"
                or type(proof.get("sessions")) is not list
                or len(proof["sessions"]) != len(expected) or type(dumps) is not dict
                or set(dumps) != {row["slice"] for row in owners}):
            raise FixtureNativeBlocked("Original native source is incomplete")
        summaries = {row["supi"]: row for row in proof["sessions"]}
        if len(summaries) != len(expected) or set(summaries) != set(expected):
            raise FixtureNativeBlocked("Original native cohort changed")
        qos_by_slice = {}
        for name, dump in dumps.items():
            qos = dump.get("qosObservation") if type(dump) is dict else None
            if (dump.get("available") is not True or dump.get("source") != "gtp5g-netlink"
                    or dump.get("schemaVersion") != "cityverse.upf/v1"
                    or dump.get("upf") != "upf-" + name or dump.get("errors") != []
                    or dump.get("residuals") != [] or type(dump.get("epoch")) is not str
                    or type(qos) is not dict or qos.get("available") is not True
                    or qos.get("errors") != [] or qos.get("epoch") != dump["epoch"]
                    or type(qos.get("enabled")) is not bool
                    or qos.get("readback") != "QoS Enable: " + str(int(qos["enabled"]))
                    or qos.get("observedAt") != dump.get("observedAt")):
                raise FixtureNativeBlocked("Original native QoS source is unavailable")
            qos_by_slice[name] = {"epoch": dump["epoch"], "enabled": qos["enabled"],
                                  "readback": qos["readback"],
                                  "observedAt": dump["observedAt"]}
        per_ue = []
        for supi, owner in sorted(expected.items()):
            name, seid = owner["slice"], owner["pfcpSeid"]
            dump, summary = dumps[name], summaries[supi]
            stable = ("supi", "sessionId", "smContextRef", "pfcpSeid", "upfEpoch",
                      "slice", "upf", "sst", "sd")
            if (any(summary.get(key) != owner[key] for key in stable)
                    or owner["upfEpoch"] != dump["epoch"]):
                raise FixtureNativeBlocked("Original native owner changed")
            qers = [q for q in dump["qers"] if type(q) is dict
                    and str(q.get("pfcpSeid")) == seid]
            pdrs = [p for p in dump["pdrs"] if type(p) is dict
                    and str(p.get("seid")) == seid]
            sessions = [session for session in dump.get("sessions", [])
                        if type(session) is dict and str(session.get("pfcpSeid")) == seid]
            if (not qers or not pdrs or len({q.get("id") for q in qers}) != len(qers)
                    or len({p.get("id") for p in pdrs}) != len(pdrs)
                    or len(sessions) != 1 or sessions[0].get("ip") != owner["ueIp"]
                    or sessions[0].get("upf") != owner["upf"]
                    or sessions[0].get("evidence") != "kernel-installed-pdr-far"):
                raise FixtureNativeBlocked("Original QER/PDR identity is ambiguous")
            if (sorted(({"id": q["id"], "pdrIds": q["pdrIds"], "mbrKbps": q["mbrKbps"]}
                        for q in qers), key=lambda q: q["id"])
                    != sorted(summary.get("qers", []), key=lambda q: q["id"])):
                raise FixtureNativeBlocked("Original QER differs from the native proof")
            for qer in qers:
                if (not _integer(qer.get("id"), maximum=2**32 - 1)
                        or not _integer(qer.get("gate"), maximum=255)
                        or not _integer(qer.get("qfi"), maximum=255)
                        or type(qer.get("mbrKbps")) is not dict
                        or type(qer.get("gbrKbps")) is not dict
                        or any(not _integer(qer[rate].get(direction))
                               for rate in ("mbrKbps", "gbrKbps")
                               for direction in ("ul", "dl"))):
                    raise FixtureNativeBlocked("Original QER fields are incomplete")
            qer_ids = {qer["id"] for qer in qers}
            pdr_ids = {pdr["id"] for pdr in pdrs}
            if (any(type(pdr.get("qerIds")) is not list or not pdr["qerIds"]
                    or set(pdr["qerIds"]) - qer_ids or not _integer(pdr.get("farId"), maximum=2**32 - 1)
                    for pdr in pdrs)
                    or any(type(qer.get("pdrIds")) is not list or not qer["pdrIds"]
                           or set(qer["pdrIds"]) - pdr_ids
                           or set(qer["pdrIds"]) != {pdr["id"] for pdr in pdrs
                                                       if qer["id"] in pdr["qerIds"]}
                           for qer in qers)):
                raise FixtureNativeBlocked("Original PDR to QER linkage changed")
            selected = _ambr_qer(qers)
            per_ue.append({"supi": supi, "slice": name, "pfcpSeid": seid,
                           "podUid": owner["podUid"], "upfEpoch": owner["upfEpoch"],
                           "qers": deepcopy(qers), "pdrs": deepcopy(pdrs),
                           "originalAmbrQerId": selected["id"],
                           "originalMbrKbps": selected["mbrKbps"],
                           "qosEnabled": qos_by_slice[name]["enabled"]})
        return {"schema": "cityverse.scenario-fixture-native-original/v1",
                "ownerSha256": owner_digest, "proofSha256": common._digest(proof),
                "sourceSha256": common._digest(dumps), "perUe": per_ue,
                "qosBySlice": qos_by_slice, "proof": deepcopy(proof),
                "sourceDumps": deepcopy(dumps)}
    except (KeyError, TypeError, ValueError, AttributeError, GroupQerBlocked) as exc:
        if isinstance(exc, FixtureNativeBlocked):
            raise
        raise FixtureNativeBlocked("Original native source is malformed") from None


def seal_native_receipt(snapshot, claim, proof, dumps, phase):
    """Attach exact round and workload pins to one independently collected raw dump."""
    if phase not in PHASES or type(claim) is not dict:
        raise FixtureNativeBlocked("Fixture native phase is invalid")
    native = original_snapshot(snapshot, proof, dumps)
    receipt = {"schema": "cityverse.scenario-fixture-native-receipt/v1",
               "phase": phase, "roundId": snapshot.get("roundId"),
               "generation": snapshot.get("generation"),
               "fleetLeaseDigest": common._digest(snapshot.get("fleetLease")),
               "cohortSha256": native["ownerSha256"],
               "workloadContractDigest": claim.get("workloadContractDigest"),
               "observedAt": proof.get("observedAt"), "native": native}
    return validate_native_receipt(receipt, snapshot, claim, phase)


def validate_native_receipt(receipt, snapshot, claim, phase):
    """Replay full raw source; required by both producer and final verifier."""
    if (phase not in PHASES or type(receipt) is not dict or set(receipt) != _RECEIPT_FIELDS
            or receipt.get("schema") != "cityverse.scenario-fixture-native-receipt/v1"
            or receipt.get("phase") != phase or type(snapshot) is not dict
            or type(claim) is not dict or receipt.get("roundId") != snapshot.get("roundId")
            or receipt.get("generation") != snapshot.get("generation")
            or receipt.get("fleetLeaseDigest") != common._digest(snapshot.get("fleetLease"))
            or receipt.get("cohortSha256") != claim.get("cohortSha256")
            or receipt.get("workloadContractDigest") != claim.get("workloadContractDigest")):
        raise FixtureNativeBlocked("Fixture native receipt differs from its owner")
    native = receipt.get("native")
    if type(native) is not dict or set(native) != {"schema", "ownerSha256", "proofSha256",
                                                  "sourceSha256", "perUe", "qosBySlice",
                                                  "proof", "sourceDumps"}:
        raise FixtureNativeBlocked("Fixture native raw archive is incomplete")
    expected = original_snapshot(snapshot, native["proof"], native["sourceDumps"])
    if native != expected or receipt["observedAt"] != native["proof"]["observedAt"]:
        raise FixtureNativeBlocked("Fixture native raw archive changed")
    if phase in {"fixture_installed", "inner_restored"}:
        if (any(row["originalMbrKbps"] != {"ul": claim.get("fixtureCapKbps"),
                                             "dl": claim.get("fixtureCapKbps")}
                for row in native["perUe"])
                or any(not item["enabled"] for item in native["qosBySlice"].values())):
            raise FixtureNativeBlocked("Fixture cap or kernel QoS flag is not installed")
    return receipt
