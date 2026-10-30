"""Run one provisioned scenario UE through a city gNB and an isolated N6 target.

Requires an explicit kubeconfig and context. Creates only one owned UE
Pod, starts an owned one-shot iperf server in the existing N6 traffic container,
and normally deregisters/removes the UE afterwards. No subscriber keys are read
by this process or written to disk. This is functional acceptance, not a benchmark.
"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import yaml

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from backend.onprem.runtime.free_ran_ue import pod_manifest
from backend.onprem.runtime.traffic_manager import traffic_image


def merge_values(target, source):
    for key, value in source.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict):
            merge_values(target.setdefault(key, {}), value)
        else:
            target[key] = value
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--values", type=Path, default=ROOT / "infrastructure/onprem/values.yaml")
    parser.add_argument("--site-values", type=Path)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="free5gc-onprem")
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--scenario", default="video_streaming")
    parser.add_argument("--supi", required=True, help="An existing, idle, provisioned IMSI; no automatic subscriber writes")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--transport-plan", type=Path, help="Rendered n3-delay.json; enable local kernel/capture acceptance")
    parser.add_argument("--transport-city-only", action="store_true", help="Verify only actual city delay; skip counterfactual budgets")
    parser.add_argument("--leave-city-delay", action="store_true", help="Leave actual city N3 delay after successful verification")
    parser.add_argument("--traffic-mbps", type=float, help="Override the scenario's offered UDP load for functional acceptance")
    args = parser.parse_args()
    context, namespace = args.context, args.namespace
    if re.search(r"eks|prod|docker-desktop|kind-|minikube", context, re.I):
        raise ValueError("smoke test requires an explicit onprem lab context")
    values = yaml.safe_load(args.values.read_text(encoding="utf-8"))
    if args.site_values:
        merge_values(values, yaml.safe_load(args.site_values.read_text(encoding="utf-8")))
    config = values["city"]
    scenario = config["scenarios"][args.scenario]
    if args.traffic_mbps is not None:
        if not 0 < args.traffic_mbps <= 100:
            raise ValueError("traffic Mbps must be in (0,100]")
        scenario["trafficMbps"] = args.traffic_mbps
    gnb = next(g for g in config["gnbs"] if g["workload"] == scenario["gnb"])
    ipaddress.ip_address(scenario["pingTarget"])
    if scenario["ueCount"] != 1:
        raise ValueError("this functional smoke test supports exactly one UE")
    args.output.mkdir(parents=True, exist_ok=False)
    run_id = uuid.uuid4().hex[:12]
    report = {"schemaVersion": "cityverse.city-smoke/v1", "startedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "context": context, "namespace": namespace, "scenario": args.scenario,
              "building": scenario["buildingId"], "gnb": gnb["workload"], "mapObject": gnb["id"],
              "gnbPositionKm": gnb["position"], "upfSite": config["slices"][scenario["slice"]]["site"],
              "slice": scenario["slice"], "status": "running", "cleanup": "not_started"}
    command = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", context, "--namespace", namespace]

    def call(*parts, data=None, timeout=30, check=True):
        result = subprocess.run(command + list(parts), input=data, text=True, capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError("kubectl " + parts[0] + " failed: " + result.stderr[-1500:])
        return result

    def get(*parts):
        return json.loads(call("get", *parts, "-o", "json").stdout)

    def save():
        (args.output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    pod = pod_manifest(int(time.time()), 0, {"slice": scenario["slice"], "credentialGroup": scenario["credentialGroup"],
                       "supis": [args.supi]}, namespace, gnb_host=gnb["workload"])
    name = "city-smoke-" + run_id
    pod["metadata"]["name"] = name
    pod["metadata"]["labels"]["cityverse.io/managed-by"] = "city-infra-smoke"
    pod["metadata"]["labels"]["cityverse.io/smoke-run"] = run_id
    pod["metadata"]["annotations"]["cityverse.io/building"] = scenario["buildingId"]
    placement = config["placement"]
    pod["spec"]["nodeSelector"] = (placement["nodeSelector"] if placement["mode"] == "colocated"
                                   else gnb.get("nodeSelector", {"cityverse.io/role": "ran"}))
    if "REPLACE" in json.dumps(pod["spec"]["nodeSelector"]):
        raise ValueError("fill the site nodeSelector before running the test")
    pod["spec"]["containers"].append({"name": "traffic", "image": traffic_image(),
        "imagePullPolicy": "IfNotPresent", "command": ["/bin/sh", "-c", "trap 'exit 0' TERM INT; while :; do sleep 1 & wait $!; done"],
        "resources": {"requests": {"cpu": "25m", "memory": "64Mi"}, "limits": {"cpu": "1", "memory": "128Mi"}},
        "securityContext": {"runAsUser": 1337, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}})
    pod["spec"]["terminationGracePeriodSeconds"] = 45
    created = False
    server = None
    probe = None
    try:
        if get("namespace", namespace)["metadata"].get("labels", {}).get("cityverse.io/platform") != "kubeadm":
            raise ValueError("namespace is not owned by the onprem runtime")
        for other in get("pods")["items"]:
            if args.supi in json.loads(other["metadata"].get("annotations", {}).get("cityverse.io/supis", "[]")):
                raise ValueError("subscriber already has an owned UE Pod")
        deployment = get("deployment", gnb["workload"])
        if deployment["metadata"].get("annotations", {}).get("cityverse.io/map-object") != gnb["id"]:
            raise ValueError("city gNB configuration has not been deployed")
        # Fence against stale manifests or a different scenario slice/IP binding.
        deployed_config = get("configmap", gnb["workload"] + "-config")
        ran = json.loads(deployed_config["data"]["gnb.json"])["gnb"]
        selected_slice = config["slices"][scenario["slice"]]
        sst, sd = selected_slice["sst"], selected_slice["sd"]
        if ran["snssai"] != {"sst": str(sst), "sd": sd}:
            raise ValueError("deployed gNB slice differs from scenario")
        target = get("pods", "-l", "app.kubernetes.io/name=n6-" + scenario["slice"])["items"]
        if len(target) != 1 or not any(c["name"] == "traffic" for c in target[0]["spec"]["containers"]):
            raise ValueError("scenario needs one existing N6 traffic worker")
        receiver = target[0]["metadata"]["name"]
        report.update(uePod=name, receiverPod=receiver, upf="upf-" + scenario["slice"], expectedN3=ran["upfN3Ip"])
        (args.output / "ue-pod.json").write_text(json.dumps(pod, indent=2), encoding="utf-8")
        save()
        if args.transport_plan:
            from backend.onprem.operations.validation.transport_probe import TransportProbe
            probe = TransportProbe(call, get, config, gnb, args.output, args.transport_plan, args.leave_city_delay, args.transport_city_only)
            probe.start()
        call("create", "-f", "-", data=json.dumps(pod))
        created = True
        print("UE Pod created; waiting for registration and PDU IP", flush=True)
        call("wait", "--for=condition=Ready", "pod/" + name, "--timeout=100s", timeout=110)
        deadline, pdu_ip = time.monotonic() + 90, None
        while time.monotonic() < deadline:
            result = call("exec", name, "-c", "ue", "--", "ip", "-j", "addr", "show", "dev", "ueTun0", check=False)
            if result.returncode == 0:
                addresses = json.loads(result.stdout)
                pdu_ip = next((a["local"] for link in addresses for a in link.get("addr_info", []) if a["family"] == "inet"), None)
                if pdu_ip:
                    break
            time.sleep(2)
        if not pdu_ip or ipaddress.ip_address(pdu_ip) not in ipaddress.ip_network(selected_slice["uePool"]):
            raise RuntimeError("no PDU IP in the expected slice pool")
        report["pduIp"] = pdu_ip
        report["phase"] = "pdu_ip_observed"
        save()
        # Binding a socket's source IP alone does not select the TUN route.
        # This changes only the owned UE Pod's network namespace.
        call("exec", name, "-c", "ue", "--", "ip", "route", "replace", scenario["pingTarget"] + "/32",
             "dev", "ueTun0", "src", pdu_ip)
        route = call("exec", name, "-c", "ue", "--", "ip", "route", "get", scenario["pingTarget"], "from", pdu_ip)
        (args.output / "ue-route.txt").write_text(route.stdout, encoding="utf-8")
        if "dev ueTun0" not in route.stdout:
            raise RuntimeError("N6 traffic route does not select the UE TUN")
        ping = call("exec", name, "-c", "ue", "--", "ping", "-I", "ueTun0", "-c", "5", "-W", "2", scenario["pingTarget"])
        (args.output / "ping.txt").write_text(ping.stdout, encoding="utf-8")
        report.update(ping="passed", phase="ping_passed")
        save()
        if probe:
            probe.measure(name, scenario["pingTarget"])
        # A dedicated high port and --one-off server avoid touching other tests.
        port = 30000 + int(run_id[:4], 16) % 20000
        server = subprocess.Popen(command + ["exec", receiver, "-c", "traffic", "--", "timeout", str(scenario["durationSeconds"] + 45), "iperf3", "-s", "-1", "-B", scenario["pingTarget"], "-p", str(port), "-J"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(1)
        client = call("exec", name, "-c", "traffic", "--", "iperf3", "-c", scenario["pingTarget"], "-B", pdu_ip, "--connect-timeout", "5000",
                      "-p", str(port), "-u", "-b", str(scenario["trafficMbps"]) + "M", "-t", str(scenario["durationSeconds"]), "-J",
                      timeout=scenario["durationSeconds"] + 25, check=False)
        (args.output / "iperf-client.json").write_text(client.stdout, encoding="utf-8")
        if client.returncode:
            raise RuntimeError("UE iperf client failed: " + (client.stderr or client.stdout)[-1500:])
        server_out, server_error = server.communicate(timeout=20)
        (args.output / "iperf-client.json").write_text(client.stdout, encoding="utf-8")
        (args.output / "iperf-server.json").write_text(server_out, encoding="utf-8")
        if server.returncode:
            raise RuntimeError("N6 iperf server failed: " + server_error[-1000:])
        received = json.loads(server_out)
        if received.get("error"):
            raise RuntimeError("N6 iperf server reported an error")
        summary = received["end"].get("sum_received") or received["end"]["sum"]
        if summary["bytes"] <= 0:
            raise RuntimeError("N6 received no user-plane payload")
        report.update(status="passed", phase="traffic_received", ping="passed", receivedBytes=summary["bytes"],
                      receivedMbps=summary["bits_per_second"] / 1e6, lostPercent=summary.get("lost_percent"),
                      distanceDelay="kernel_n3_netem" if probe else "not_managed_by_this_run", scope="one UE functional registration/PDU/N6 uplink")
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        if created:
            log = call("logs", name, "-c", "ue", check=False)
            # AKA material is never intentionally logged; retain only procedure/IP lines.
            lines = [line for line in log.stdout.splitlines() if re.search(r"registration|registered|PDU|session|10\.10[1-4]\.", line, re.I)
                     and not re.search(r"key|opc|secret|authentication", line, re.I)]
            (args.output / "ue-procedures.log").write_text("\n".join(lines), encoding="utf-8")
            result = call("delete", "pod", name, "--wait=true", "--timeout=200s", timeout=210, check=False)
            report["cleanup"] = "pod_deleted" if not result.returncode else "failed"
        if server is not None and server.poll() is None:
            server.terminate()
            server.wait(timeout=10)
        if probe:
            try:
                # Leave persistent changes only when the complete traffic test passed.
                probe.leave = probe.leave and report["status"] == "passed"
                probe.finish()
                if report.get("pduIp"):
                    probe.decode(report["pduIp"])
                if probe.leave:
                    probe.keep_city_plan()
            except Exception as error:
                report.update(status="failed", transportError=str(error))
        report["finishedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save()
    if report["status"] != "passed":
        raise RuntimeError(report.get("transportError", "smoke failed"))
    print(json.dumps({k: report[k] for k in ("status", "pduIp", "receivedMbps", "receivedBytes", "lostPercent")}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
