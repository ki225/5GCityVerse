"""Staged live official-N5 capability harness; no automatic UE/module mutation.

observe is read-only. measure/create/delete require --execute; create additionally
requires --maintenance-approved and actual fixed-module/QoS readback. This tool
records evidence, never declares a complete QoS capability pass on API acceptance.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import quote
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from archive.legacy.policy.policy import OfficialN5Adapter, PolicyJournal, http_request
from backend.onprem.policy.errors import PolicyBlocked
from backend.onprem.traffic.capability import IperfCapabilityTraffic, native_qos_enabled


def get(url):
    with urlopen(url, timeout=5) as response:
        return json.load(response)


def native(config):
    """Actual AMF full-ref -> SMF SUPI/IP -> installed PDR/QER correlation."""
    binding = config["binding"]
    amf = get(config["amfUrl"].rstrip("/") + "/namf-oam/v1/registered-ue-context") or []
    matches = [row for row in amf if row.get("Supi") == binding["supi"]]
    if len(matches) != 1 or len(matches[0].get("PduSessions") or []) != 1:
        raise PolicyBlocked("Exactly one native AMF session required")
    session = matches[0]["PduSessions"][0]
    if session["SmContextRef"] != binding["smContextRef"]:
        raise PolicyBlocked("AMF SMF reference changed")
    smf = get(config["smfUrl"].rstrip("/") + "/nsmf-oam/v1/ue-pdu-session-info/" + quote(binding["smContextRef"], safe=""))
    if (smf.get("Supi") != binding["supi"] or smf.get("PDUAddress") != binding["ip"]
            or smf.get("Dnn") != binding["dnn"] or str(smf.get("Sst")) != str(binding["sst"])
            or smf.get("Sd") != binding["sd"] or smf.get("UpCnxState") != "ACTIVATED"
            or str(smf.get("PDUSessionID")) != str(session["PduSessionId"])):
        raise PolicyBlocked("Native session identity is not stable")
    upf = get(config["upfUrl"].rstrip("/") + "/v1/upf-state")
    observed = datetime.fromisoformat(upf["observedAt"].replace("Z", "+00:00")).timestamp()
    if upf.get("available") is not True or not -1 <= time.time() - observed <= 15:
        raise PolicyBlocked("Native QER source unavailable/stale")
    pdrs = [row for row in upf.get("pdrs", []) if row.get("ip") == binding["ip"]]
    seids = {row["seid"] for row in pdrs}
    if len(seids) != 1 or not pdrs:
        raise PolicyBlocked("PDU must correlate to exactly one installed PFCP session")
    qer_ids = {qid for row in pdrs for qid in row.get("qerIds", [])}
    qers = [q for q in upf.get("qers", []) if q["seid"] in seids and q["id"] in qer_ids]
    if not qers or {q["id"] for q in qers} != qer_ids:
        raise PolicyBlocked("Installed PDR/QER links incomplete")
    return {"amf": matches[0], "smf": smf, "upf": upf, "ownedPdrs": pdrs, "ownedQers": qers,
            "observedAt": datetime.now(timezone.utc).isoformat(), "binding": binding}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["observe", "measure", "create", "delete"])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--maintenance-approved", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Evidence output already exists; choose a new artifact path")
    if args.operation != "observe" and not args.execute:
        parser.error("Mutation/traffic requires coordinated --execute")
    config = json.loads(args.config.read_text())
    traffic = IperfCapabilityTraffic(config["kubectl"], config["kubeconfig"], config["context"], config.get("namespace", "free5gc-onprem"))
    binding = config["binding"]
    output = {"operation": args.operation, "startedAt": time.time(), "completeCapabilityPassed": False}
    def guard(_):
        current = get(config["backendUrl"].rstrip("/") + "/api/onprem/ue")
        if current["generation"] != binding["generation"] or not 1 <= current["requested"] <= 50:
            raise PolicyBlocked("UE intent changed during policy operation")
        native(config)
        traffic.pod(config["uePod"], binding["podUid"])
        return True
    try:
        output["nativeBefore"] = native(config)
        if args.operation == "measure":
            output["measurements"] = [traffic.sample(binding, config["uePod"], config["receiverPod"],
                config["receiverPodUid"], config["profile"]["receiverIp"], config["profile"]["receiverPort"],
                direction=direction, offered_mbps=config.get("offeredMbps", 4), duration=config.get("durationSeconds", 10), guard=guard)
                for direction in ("ul", "dl")]
        elif args.operation in {"create", "delete"}:
            journal = PolicyJournal(Path(config["journal"]))
            def register(action, fingerprint, uri):
                admin = os.environ.get("CITYVERSE_AF_ADMIN_TOKEN", "")
                if len(admin) < 32:
                    raise PolicyBlocked("AF management credential unavailable")
                code, _, raw = http_request("POST", config["callbackAdminBase"].rstrip("/") + "/admin/actions/" + action,
                                            {"fingerprint": fingerprint, "uri": uri}, admin)
                if code != 200:
                    raise PolicyBlocked("AF callback registration failed")
                return json.loads(raw)["path"]
            adapter = OfficialN5Adapter(journal, config["pcfTransportBase"], config["callbackPublicBase"],
                resource_base=config["pcfResourceBase"], token=os.environ.get("CITYVERSE_PCF_TOKEN", ""),
                oauth_required=config.get("oauthRequired", False), capability_enabled=True, callback_registrar=register)
            if args.operation == "create":
                if not args.maintenance_approved or output["nativeBefore"]["upf"].get("moduleVersion") != "0.10.1":
                    raise PolicyBlocked("Approved fixed-module maintenance proof required")
                upf = json.loads(traffic._run(["get", "pod", config["upfPod"], "-o", "json"]))
                if upf["metadata"]["uid"] != config["upfPodUid"]:
                    raise PolicyBlocked("UPF Pod changed")
                container = config["upfContainer"]
                if container not in {row["name"] for row in upf["spec"]["containers"]}:
                    raise PolicyBlocked("Expected UPF container is absent")
                qos = traffic._run(["exec", config["upfPod"], "-c", container, "--", "cat", "/proc/gtp5g/qos"]).strip()
                if not native_qos_enabled(qos):
                    raise PolicyBlocked("Native gtp5g QoS is not enabled")
                status = get(config["backendUrl"].rstrip("/") + "/api/onprem/ue")
                if status["generation"] != binding["generation"] or status["status"] != "established":
                    raise PolicyBlocked("Fresh established generation required before policy create")
                snapshot = {"sequence": config["sequence"], "policyRevision": config["policyRevision"],
                            "generation": status["generation"], "observedAt": status["observedAt"], "totalUes": status["requested"]}
                baseline = json.loads(Path(config["baselineArtifact"]).read_text())
                if (baseline.get("operation") != "measure" or baseline.get("status") != "recorded_not_capability_passed"
                        or baseline.get("nativeBefore", {}).get("binding") != binding
                        or not 0 <= time.time() - baseline.get("finishedAt", 0) <= 120
                        or {m.get("direction") for m in baseline.get("measurements", [])} != {"ul", "dl"}):
                    raise PolicyBlocked("Actual identified baseline artifact required")
                adapter.create(config["actionId"], config["roundId"], binding, config["profile"], baseline, snapshot, guard)
            else:
                adapter.delete(config["actionId"], guard)
            output["action"] = journal.public(config["actionId"])
        output["nativeAfter"] = native(config)
        output["status"] = "recorded_not_capability_passed"
    except Exception as error:
        output["status"] = "blocked_or_failed"
        output["errorClass"] = type(error).__name__  # Do not expose callback/token-bearing URLs.
        raise
    finally:
        output["finishedAt"] = time.time()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(output, stream, indent=2)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Policy capability blocked:", type(error).__name__, file=sys.stderr)
        sys.exit(1)
