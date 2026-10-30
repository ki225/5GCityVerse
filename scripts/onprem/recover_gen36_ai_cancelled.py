"""Exact, offline release of the cancelled, never-registered AI gen36 round.

Run only as WSL root with the normal backend stopped. The default is a read-only
preflight. --execute appends gen37 zero intent and closes the exact cancelled
round under the backend lifetime lock and both SQLite writer locks. No Pod is
deleted and no NF policy is changed by this program.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.onprem.start_native_backend import (  # noqa: E402
    FORWARDS, LAB, SMF_INVENTORY_IMAGE, UE_IMAGE, INIT_IMAGE,
    acquire_backend_lifetime_lock, read_smf_inventory_token,
    validate_wsl_policy_paths, apply_wsl_environment,
)
from backend.onprem.evidence.collector import configured_native_collector  # noqa: E402
from archive.legacy.operations.recovery.operator_reconcile import StateFileLock
from backend.onprem.policy.safety import policy_inventory_clear, policy_quarantine_clear
from archive.legacy.operations.recovery.missing_pod_cleanup_operator import _ue_exclusive
from backend.onprem.traffic.capability import IperfCapabilityTraffic
from backend.onprem.runtime.kubeadm import configured_driver  # noqa: E402
from backend.onprem.runtime.ue import GenerationStore, SLICES, blank_status, seconds  # noqa: E402
from backend.onprem.runtime.round_resources import NativeRoundResources, native_receiver_pods
from backend.onprem.storage.rounds import RoundLedger, maintenance_pending

ROUND_ID = "789c7095-c577-480b-acb9-02ed20455483"
ROUND_RAW_SHA = "00449726446aa80a711c902b4f01726837a250efcbd13e497a007445c0fb8499"
UE_STATUS_SHA = "128fc3ca5b5dcc597107abf0f3429aed1119047602ae0191d1bd72c4f27f2b1c"
POD_UIDS = {"96fb339e-a464-4c18-95bc-314a96fe2b4b",
            "8cf4183e-684a-4409-94f2-0a10c97967f2"}
SOURCE_GENERATION = 36
NEXT_GENERATION = 37
SCHEMA = "cityverse.gen36-cancelled-ai-operator-release/v1"
RUNTIME = Path("/home/kiki/.local/state/5gcityverse/onprem")
OUTPUT = Path("/var/lib/cityverse/gen36-ai-cancelled-20261014")
KUBECONFIG = Path("/etc/kubernetes/admin.conf")
CONTEXT = "kubernetes-admin@cityverse-wsl"
NAMESPACE = "free5gc-onprem"
SMF_IMAGE = "docker.io/cityverse/free5gc-smf@sha256:132a7b550153c78570096ad44d39d61c9d766727b44c900559cdaeb29d2caeaa"


class RecoveryBlocked(RuntimeError):
    pass


def need(ok, reason):
    if not ok:
        raise RecoveryBlocked(reason)


def digest(raw):
    return sha256(raw).hexdigest()


def authority(round_db, ue_db):
    row = round_db.execute("SELECT round_id,snapshot,released FROM rounds "
                           "ORDER BY ordinal DESC LIMIT 1").fetchone()
    need(row and row[0] == ROUND_ID and row[2] == 0 and
         digest(row[1].encode()) == ROUND_RAW_SHA, "Round raw authority changed")
    snapshot = json.loads(row[1])
    need(snapshot.get("sequence") == 2447 and snapshot.get("generation") == SOURCE_GENERATION
         and snapshot.get("stage") == "cleaning" and snapshot.get("executionOutcome") == "cancelled"
         and snapshot.get("cleanupStatus") == "blocked" and snapshot.get("perUe") == []
         and snapshot.get("fleetLease") is None and not maintenance_pending(snapshot)
         and snapshot.get("allocation") == {"baselineUes": 0, "total": 100,
             "events": {"video_streaming": 100,  "connected_traffic": 0,
                        "realtime_control": 0, "factory_iot": 0}}
         and snapshot.get("counts") == {"allocated": 100, "registered": 0,
                                         "pduEstablished": 0, "trafficVerified": 0}
         and [a.get("kind") for a in snapshot.get("actions", [])] ==
             ["ue_allocation", "execution_failure", "adaptive_qer_cleanup"]
         and snapshot["actions"][0].get("generation") == SOURCE_GENERATION
         and snapshot["actions"][2].get("cleanupVerified") is True
         and snapshot["actions"][2].get("policyActionIds") == [],
         "Round is not the exact cancelled pre-registration authority")
    ue = ue_db.execute("SELECT id,desired,allocation,created,status FROM generations "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    need(ue and ue[0] == SOURCE_GENERATION and digest(ue[4].encode()) == UE_STATUS_SHA,
         "UE status raw authority changed")
    desired, allocation, status = json.loads(ue[1]), json.loads(ue[2]), json.loads(ue[4])
    need(desired == {"embb": 100, "urllc": 0, "mmtc": 0, "v2x": 0}
         and isinstance(allocation, list) and len(allocation) == 100
         and len({a.get("supi") for a in allocation}) == 100
         and all(a.get("slice") == "embb" for a in allocation)
         and status.get("generation") == SOURCE_GENERATION
         and status.get("status") == "failed" and status.get("requested") == 100
         and status.get("allocated") == 100 and status.get("registered") == 0
         and status.get("pduEstablished") == 0 and status.get("trafficVerified") == 0
         and status.get("perUe") == []
         and "Reconciliation timed out; graceful UE shutdown requested; release remains independently verified"
             in status.get("errors", []),
         "UE source is not the exact unregistered timeout fleet")
    return snapshot, allocation, row[1]


def kubectl(driver, *args):
    return driver.kubectl(*args)


def inventory(driver):
    namespace = kubectl(driver, "get", "namespace", NAMESPACE, "-o", "json")
    metadata = namespace.get("metadata", {})
    need(metadata.get("name") == NAMESPACE and metadata.get("uid")
         and metadata.get("labels", {}).get("cityverse.io/platform") == "kubeadm",
         "Dedicated namespace identity changed")
    pods = kubectl(driver, "get", "pods", "-o", "json").get("items")
    need(isinstance(pods, list), "Kubernetes Pod inventory unavailable")
    owned = [p for p in pods if p.get("metadata", {}).get("labels", {}).get(
        "cityverse.io/managed-by") == "onprem-ue-controller"]
    need(owned == [] and not any(p.get("metadata", {}).get("uid") in POD_UIDS or
        p.get("metadata", {}).get("name", "").startswith("cv-ue-g36-") for p in pods),
        "Original or replacement UE Pod remains")
    cri = subprocess.run(["/usr/bin/crictl", "--runtime-endpoint",
        "unix:///run/containerd/containerd.sock", "pods", "--name", "cv-ue-g36", "-o", "json"],
        capture_output=True, text=True, timeout=15, check=True)
    need(json.loads(cri.stdout).get("items") == [], "Gen36 CRI sandbox remains")
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                command = proc.joinpath("cmdline").read_bytes().replace(b"\0", b" ")
            except (OSError, PermissionError):
                continue
            need(b"./free-ran-ue ue " not in command,
                 "A free-ran-ue UE process still runs")
    return metadata["uid"], pods


def pcf_absence(driver, allocation, pods):
    def owner(app):
        rows = [p for p in pods if p.get("metadata", {}).get("labels", {}).get(
            "app.kubernetes.io/name") == app]
        need(len(rows) == 1 and rows[0].get("status", {}).get("phase") == "Running"
             and not rows[0]["metadata"].get("deletionTimestamp"), "PCF owner unavailable")
        return rows[0]
    pcf, callback = owner("free5gc-pcf"), owner("cityverse-af-callback")
    supis = sorted(a["supi"] for a in allocation)
    paths = ["/npcf-oam/v1/am-policy/" + s for s in supis] + [
        "/npcf-smpolicycontrol/v1/sm-policies/" + s + "-1" for s in supis]
    code = ("import concurrent.futures,json,time,urllib.request,urllib.error,signal\n"
            "signal.alarm(40)\nBASE=" + repr("http://" + pcf["status"]["podIP"] + ":8080") +
            "\nPATHS=" + repr(paths) + "\n"
            "def read(path):\n"
            " try:\n  with urllib.request.urlopen(BASE+path,timeout=5) as r: "
            "return [path,r.status,json.loads(r.read(65537))]\n"
            " except urllib.error.HTTPError as e: return [path,e.code,json.loads(e.read(65537))]\n"
            "with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool: "
            "rows=list(pool.map(read,PATHS))\n"
            "print(json.dumps({'observedAt':time.time(),'rows':rows}))")
    result = subprocess.run(["/usr/bin/kubectl", "--kubeconfig", str(KUBECONFIG),
        "--context", CONTEXT, "-n", NAMESPACE, "exec", callback["metadata"]["name"],
        "-c", "callback", "--", "python", "-c", code],
        capture_output=True, text=True, timeout=50, check=True)
    evidence = json.loads(result.stdout)
    need([row[0] for row in evidence.get("rows", [])] == paths
         and all(row[1] == 404 and isinstance(row[2], dict)
                 and row[2].get("cause") == "CONTEXT_NOT_FOUND" for row in evidence["rows"]),
         "Known 100 PCF policy absences are incomplete")
    pcf_artifact = OUTPUT / "pcf-current-read.json"
    pcf_artifact.write_text(json.dumps(evidence, sort_keys=True) + "\n")
    os.chmod(pcf_artifact, 0o600)
    after = kubectl(driver, "get", "pods", "-o", "json").get("items")
    for before in (pcf, callback):
        matches = [p for p in after if p.get("metadata", {}).get("name") == before["metadata"]["name"]]
        need(len(matches) == 1 and matches[0]["metadata"]["uid"] == before["metadata"]["uid"]
             and matches[0]["status"]["containerStatuses"] == before["status"]["containerStatuses"],
             "PCF or callback incarnation changed")
    return {"observedAt": evidence["observedAt"], "amQueries": 100,
            "knownSmQueries": 100, "pcfPodUid": pcf["metadata"]["uid"],
            "callbackPodUid": callback["metadata"]["uid"],
            "rowsSha256": digest(json.dumps(evidence["rows"], sort_keys=True).encode())}


def physical_proof(store, driver, collector, allocation):
    namespace_uid, pods = inventory(driver)
    def policy_quiet():
        return (policy_inventory_clear(RUNTIME / "policy-inventory.json")
                and policy_quarantine_clear(RUNTIME / "policy-quarantine-registry.json"))
    need(policy_quiet() is True,
         "Policy inventory or quarantine is unresolved")
    traffic = IperfCapabilityTraffic("kubectl", KUBECONFIG, CONTEXT, NAMESPACE)
    resources = NativeRoundResources(SimpleNamespace(driver=driver), traffic,
                                     policy_quiet=lambda: True)
    need(resources._traffic_absent(pods) is True, "N6 iperf worker remains")
    pcf = pcf_absence(driver, allocation, pods)
    ran = driver.observe_gnb_contexts(parallel=True)
    need(ran.get("source") == "free-ran-ue-gnb-api-ue-count"
         and set(ran.get("readouts", {})) == set(SLICES)
         and all(row.get("ueCount") == 0 for row in ran["readouts"].values()),
         "Native RAN context remains or is unavailable")
    evidence = collector.observe_release(SOURCE_GENERATION)
    need(evidence.get("schemaVersion") == "cityverse.evidence/v1"
         and evidence.get("generation") == SOURCE_GENERATION and evidence.get("ues") == []
         and evidence.get("sources", {}).get("smf", {}).get("inventoryComplete") is True
         and all(evidence.get("sources", {}).get(name, {}).get("available") is True
                 for name in ("ue", "amf", "smf", "upf")),
         "Native AMF/SMF/UPF/UE zero proof is unavailable")
    native_artifact = OUTPUT / "native-current-read.json"
    native_artifact.write_text(json.dumps(evidence, sort_keys=True) + "\n")
    os.chmod(native_artifact, 0o600)
    after_uid, _ = inventory(driver)
    need(after_uid == namespace_uid and policy_quiet() is True,
         "Kubernetes or policy authority changed during collection")
    now = time.time()
    times = [seconds(evidence["observedAt"]), pcf["observedAt"],
             *(seconds(row["observedAt"]) for row in ran["readouts"].values()),
             *(seconds(evidence["sources"][name]["observedAt"])
               for name in ("ue", "amf", "smf", "upf"))]
    need(all(-1 <= now - t <= 30 for t in times), "Independent physical proof is stale")
    return {"schema": SCHEMA, "roundId": ROUND_ID, "sourceGeneration": SOURCE_GENERATION,
            "zeroGeneration": NEXT_GENERATION, "observedAt": min(times),
            "namespaceUid": namespace_uid, "pcf": pcf,
            "nativeArtifactSha256": digest(native_artifact.read_bytes()),
            "ranCounts": {k: v["ueCount"] for k, v in ran["readouts"].items()},
            "nativeSources": {k: evidence["sources"][k]["source"] for k in
                              ("ue", "amf", "smf", "upf")},
            "trafficStopped": True, "policiesAbsent": True, "rulesRestored": True,
            "uesReleased": True, "podsAbsent": True, "globalSmfInventoryComplete": True,
            "normalGracefulTeardown": False, "capacityPromoted": False,
            "scope": "operator-known-round-and-managed-runtime"}


def commit(ledger, store, proof, *, expected_round, expected_allocation):
    with ledger.exclusive_connection() as round_db:
        with _ue_exclusive(store.path) as ue_db:
            round_snapshot, allocation, raw = authority(round_db, ue_db)
            need(round_snapshot == expected_round and allocation == expected_allocation,
                 "Durable authority changed after physical readback")
            need(-1 <= time.time() - proof["observedAt"] <= 30,
                 "Physical proof expired before commit")
            zero = dict.fromkeys(SLICES, 0)
            created = time.time()
            cursor = ue_db.execute("INSERT INTO generations(desired,allocation,created,status) "
                                   "VALUES (?,?,?,?)", (json.dumps(zero), "[]", created, "{}"))
            need(cursor.lastrowid == NEXT_GENERATION, "UE zero successor changed")
            ue_db.execute("UPDATE generations SET status=? WHERE id=?",
                          (json.dumps(blank_status(NEXT_GENERATION, zero)), NEXT_GENERATION))
            receipt = {**proof, "sourceRoundRawSha256": ROUND_RAW_SHA,
                       "sourceUeStatusSha256": UE_STATUS_SHA,
                       "bundleDigest": digest(json.dumps(proof, sort_keys=True).encode()),
                       "committedAt": created}
            release = {**receipt, "pcfPolicyCoverage": {"knownRoundAbsent": True},
                       "operatorAbandon": {"schema": SCHEMA, "roundId": ROUND_ID,
                                           "bundleDigest": receipt["bundleDigest"],
                                           "capacityPromoted": False,
                                           "normalGracefulTeardown": False}}
            updated = deepcopy(round_snapshot)
            updated["actions"].append({"id": str(uuid4()), "kind": "operator_abandonment",
                "requested": False, "accepted": True, "installed": False,
                "effectVerified": True, "observedAt": proof["observedAt"]})
            updated.update(stage="completed", cleanupStatus="verified", sequence=2448,
                           releaseProof=release)
            changed = round_db.execute("UPDATE rounds SET snapshot=?,released=1 "
                "WHERE round_id=? AND snapshot=? AND released=0",
                (json.dumps(updated, allow_nan=False), ROUND_ID,
                 raw)).rowcount
            need(changed == 1, "Round release CAS changed")
            return receipt


def run(*, execute=False):
    need(os.name == "posix" and os.geteuid() == 0, "WSL root is required")
    need(OUTPUT.is_dir() and not OUTPUT.is_symlink()
         and OUTPUT.stat().st_mode & 0o077 == 0, "Private output directory unavailable")
    inventory_paths = validate_wsl_policy_paths(
        RUNTIME / "policy-inventory.json", RUNTIME / "policy-quarantine-registry.json")
    os.environ.update({"KUBECONFIG": str(KUBECONFIG),
        "CITYVERSE_ONPREM_RUNTIME": "kubeadm", "CITYVERSE_ONPREM_PROFILE": "wsl-kubeadm",
        "CITYVERSE_ONPREM_RAN_BACKEND": "free-ran-ue", "CITYVERSE_ONPREM_EVIDENCE_MODE": "native",
        "CITYVERSE_ONPREM_UE_MUTATIONS_ENABLED": "true",
        "CITYVERSE_ONPREM_TRAFFIC_ENABLED": "true",
        "CITYVERSE_ONPREM_KUBE_CONTEXT": CONTEXT, "CITYVERSE_ONPREM_NAMESPACE": NAMESPACE,
        "CITYVERSE_ONPREM_FREE_RAN_UE_IMAGE": UE_IMAGE,
        "CITYVERSE_ONPREM_UE_INIT_IMAGE": INIT_IMAGE,
        "CITYVERSE_ONPREM_UE_STATE_FILE": str(RUNTIME / "ue-state.sqlite3"),
        "CITYVERSE_ONPREM_CREDENTIALS_FILE": str(LAB / "groups.json"),
        "CITYVERSE_ONPREM_AMF_OAM_URL": "http://127.0.0.1:18080",
        "CITYVERSE_ONPREM_SMF_OAM_URL": "http://127.0.0.1:18081",
        "CITYVERSE_ONPREM_MONGO_URI": "mongodb://127.0.0.1:27018",
        "CITYVERSE_ONPREM_MONGO_DATABASE": "free5gc",
        "CITYVERSE_ONPREM_UPF_EVIDENCE_URLS": json.dumps({name:
            "http://127.0.0.1:" + str(FORWARDS[name][2]) for name in SLICES}),
        "CITYVERSE_ONPREM_SMF_INVENTORY_ENABLED": "true",
        "CITYVERSE_ONPREM_SMF_INVENTORY_IMAGE": SMF_IMAGE,
        "CITYVERSE_ONPREM_SMF_INVENTORY_URL": "http://127.0.0.1:18081/nsmf-oam/v1/sm-contexts"})
    apply_wsl_environment(os.environ, inventory_paths, RUNTIME)
    command = ["/usr/bin/kubectl", "--kubeconfig", str(KUBECONFIG),
               "--context", CONTEXT, "-n", NAMESPACE]
    os.environ["CITYVERSE_ONPREM_SMF_INVENTORY_TOKEN"] = read_smf_inventory_token(command, os.environ)
    lifetime = acquire_backend_lifetime_lock(RUNTIME, profile="wsl-kubeadm")
    with ExitStack() as stack:
        stack.callback(lifetime.close)
        lock = StateFileLock(RUNTIME / "ue-state.sqlite3")
        lock.acquire(); stack.callback(lock.release)
        children = []
        for name, (service, remote, local) in FORWARDS.items():
            log = (OUTPUT / ("forward-" + name + ".log")).open("a")
            stack.callback(log.close)
            child = subprocess.Popen([*command, "port-forward", "--address", "127.0.0.1",
                "service/" + service, f"{local}:{remote}"], stdout=log, stderr=log)
            children.append(child)
        def close_children():
            for child in children:
                if child.poll() is None: child.terminate()
            for child in children:
                try: child.wait(timeout=5)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
        stack.callback(close_children)
        deadline = time.monotonic() + 45
        for _, _, local in FORWARDS.values():
            while True:
                need(all(child.poll() is None for child in children), "Evidence port forward exited")
                try:
                    with socket.create_connection(("127.0.0.1", local), timeout=.2): break
                except OSError:
                    need(time.monotonic() < deadline, "Evidence port forward timed out")
                    time.sleep(.2)
        store = GenerationStore(RUNTIME / "ue-state.sqlite3")
        ledger = RoundLedger.open_existing(RUNTIME / "onprem-rounds.sqlite3")
        with sqlite3.connect(f"file:{ledger.path}?mode=ro", uri=True) as round_db, \
             sqlite3.connect(f"file:{store.path}?mode=ro", uri=True) as ue_db:
            snapshot, allocation, _ = authority(round_db, ue_db)
        driver = configured_driver()
        need(driver is not None and driver.context == CONTEXT and driver.namespace == NAMESPACE,
             "Native target changed")
        collector = configured_native_collector(store, driver)
        need(collector is not None, "Native collector unavailable")
        proof = physical_proof(store, driver, collector, allocation)
        summary = {"mode": "execute" if execute else "preflight", "roundId": ROUND_ID,
                   "sourceGeneration": SOURCE_GENERATION, "nextGeneration": NEXT_GENERATION,
                   "physicalProof": proof}
        if execute:
            before = OUTPUT / "execute-proof-before-commit.json"
            before.write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n")
            os.chmod(before, 0o600)
            receipt = commit(ledger, store, proof, expected_round=snapshot,
                             expected_allocation=allocation)
            summary["receipt"] = receipt
        output = OUTPUT / ("execute-result.json" if execute else "preflight-result.json")
        output.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n")
        os.chmod(output, 0o600)
        return {"mode": summary["mode"], "roundId": ROUND_ID,
                "generation": NEXT_GENERATION if execute else SOURCE_GENERATION,
                "proofSha256": digest(json.dumps(proof, sort_keys=True).encode())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(execute=args.execute)
    except (RecoveryBlocked, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
