"""Local lab acceptance for N3 distance emulation and 5G protocol paths.

Uses host tc/tcpdump in fenced Pod network namespaces. No credentials are read.
Only an idle lab can be tested. Existing delay state is restored on exit; an
explicit --leave-city-delay leaves the rendered city plan after acceptance.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import shutil
import tempfile
import time

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "infrastructure/onprem/scripts/n3-delay.sh"


def local(*args, check=True):
    return subprocess.run(list(map(str, args)), text=True, capture_output=True, check=check, timeout=30)


def delay_us(city, gnb, site):
    model = city["transportDelay"]
    km = sum(abs(a - b) for a, b in zip(gnb["position"], site["position"])) * model["routeStretch"]
    import math
    return math.ceil(km * model["fibreUsPerKm"] + model["processingUs"][site["tier"]])


def validate_plan(city, plan):
    expected = {g["workload"]: {} for g in city["gnbs"]}
    expected.update({"upf-" + name: {} for name in city["slices"]})
    for gnb in city["gnbs"]:
        slice_ = city["slices"][gnb["slice"]]
        delay = delay_us(city, gnb, city["sites"][slice_["site"]])
        expected[gnb["workload"]][slice_["n3Subnet"] + ".10"] = delay
        expected["upf-" + gnb["slice"]][slice_["n3Subnet"] + "." + str(gnb["n3Host"])] = delay
    actual = {}
    for name, endpoint in plan.items():
        if endpoint["interface"] != "n3" or len({p["ip"] for p in endpoint["peers"]}) != len(endpoint["peers"]):
            raise ValueError("invalid N3 interface or duplicate peer")
        actual[name] = {p["ip"]: p["delayUs"] for p in endpoint["peers"]}
    if actual != expected:
        raise ValueError("rendered transport plan differs from current city values")


class TransportProbe:
    def __init__(self, call, get, city, gnb, output, plan_file, leave=False, city_only=False):
        self.call, self.get, self.city, self.gnb = call, get, city, gnb
        self.output, self.leave = output / "transport", leave
        self.city_only = city_only
        self.output.mkdir()
        self.plan = json.loads(plan_file.read_text())["endpoints"]
        validate_plan(city, self.plan)
        self.pods, self.before, self.touched, self.captures = {}, {}, [], []
        self.spool = tempfile.TemporaryDirectory(prefix="city-transport-")
        self.report = {"status": "running", "measurements": {}, "cleanup": "not_started",
                       "timingScope": "ping-reported guest RTT; functional delay acceptance, not a calibrated physical benchmark",
                       "N9": "not_configured: each slice uses one direct gNB-UPF path"}

    def save(self):
        (self.output / "result.json").write_text(json.dumps(self.report, indent=2))

    def fence(self, name):
        pod, pid = self.pods[name]
        current = self.get("pod", pod["metadata"]["name"])
        if current["metadata"]["uid"] != pod["metadata"]["uid"] or current["metadata"].get("deletionTimestamp"):
            raise RuntimeError("target Pod was replaced or is terminating")
        sandbox = self.sandbox(pod)
        if sandbox != pid:
            raise RuntimeError("target network namespace was replaced")
        return pid

    @staticmethod
    def sandbox(pod):
        cri = ("crictl", "--runtime-endpoint", "unix:///run/containerd/containerd.sock")
        items = json.loads(local(*cri, "pods", "-o", "json").stdout)["items"]
        found = [p for p in items if p["metadata"]["uid"] == pod["metadata"]["uid"] and p["state"] == "SANDBOX_READY"]
        if len(found) != 1:
            raise RuntimeError("expected one current local sandbox")
        return int(json.loads(local(*cri, "inspectp", found[0]["id"]).stdout)["info"]["pid"])

    def change(self, name, endpoint, action="apply"):
        pid = self.fence(name)
        file = self.output / (name + "-endpoint.json")
        file.write_text(json.dumps(endpoint))
        if name not in self.touched:
            self.touched.append(name)
        return json.loads(local("bash", SCRIPT, action, pid, file).stdout or "[]")

    def start(self):
        if os.geteuid() != 0:
            raise RuntimeError("transport acceptance requires local lab root")
        for module in ("sch_netem", "sch_prio", "cls_u32"):
            local("modprobe", module)
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        pods = self.get("pods")["items"]
        node_boots = {}
        # Test temporary zero/Edge/Central budgets only when no other UE exists.
        for pod in pods:
            if pod["metadata"].get("annotations", {}).get("cityverse.io/supis") not in (None, "[]"):
                raise RuntimeError("lab has UE Pods; delay experiment requires an idle lab")
            labels = pod["metadata"].get("labels", {})
            if labels.get("cityverse.io/component") == "gnb":
                info = self.call("exec", pod["metadata"]["name"], "-c", "gnb-evidence", "--", "python3", "-c",
                                 "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:40104/api/gnb/info').read().decode())")
                if json.loads(info.stdout)["gnbInfo"]["ranUeList"]:
                    raise RuntimeError("native gNB has active UE contexts")
        for name in self.plan:
            found = [p for p in pods if p["metadata"].get("labels", {}).get("app.kubernetes.io/name") == name]
            if len(found) != 1:
                raise RuntimeError("expected one Pod for " + name)
            pod = found[0]
            node_name = pod["spec"]["nodeName"]
            if node_name not in node_boots:
                node_boots[node_name] = self.get("node", node_name)["status"]["nodeInfo"]["bootID"]
            if node_boots[node_name] != boot:
                raise RuntimeError("host netns runner supports same-node lab only")
            pid = self.sandbox(pod)
            if name.startswith("upf-"):
                own_ip = self.city["slices"][name.removeprefix("upf-")]["n3Subnet"] + ".10"
            else:
                gnb = next(g for g in self.city["gnbs"] if g["workload"] == name)
                own_ip = self.city["slices"][gnb["slice"]]["n3Subnet"] + "." + str(gnb["n3Host"])
            addresses = json.loads(local("nsenter", "-t", pid, "-n", "--", "ip", "-j", "addr", "show", "dev", "n3").stdout)
            if own_ip not in {a["local"] for link in addresses for a in link.get("addr_info", [])}:
                raise RuntimeError("live N3 address differs from city plan: " + name)
            self.pods[name] = (pod, pid)
            qdiscs = json.loads(local("nsenter", "-t", pid, "-n", "--", "tc", "-j", "qdisc", "show", "dev", "n3").stdout)
            if any(q["kind"] != "noqueue" for q in qdiscs):
                raise RuntimeError("acceptance requires clean noqueue N3 interfaces; refusing to replace existing shaping")
            self.before[name] = qdiscs
        (self.output / "before.json").write_text(json.dumps(self.before, indent=2))
        for name, endpoint in self.plan.items():
            self.change(name, endpoint)
        slice_name = self.gnb["slice"]
        targets = [("n2", self.gnb["workload"], "n2", "sctp port 38412"),
                   ("n3-ran", self.gnb["workload"], "n3", "ip proto 17"),
                   ("n3", "upf-" + slice_name, "n3", "ip proto 17"),
                   ("n4", "upf-" + slice_name, "n4", "udp port 8805"),
                   ("n6", "upf-" + slice_name, "n6", "icmp or udp or tcp")]
        for label, name, interface, filter_ in targets:
            pid = self.fence(name)
            err = (self.output / (label + "-capture.log")).open("w")
            # Capture on Linux storage, not /mnt/<drive>: per-packet cross-OS
            # writes can perturb the very path under measurement.
            process = subprocess.Popen(["nsenter", "-t", str(pid), "-n", "--", "tcpdump", "-n", "-U", "-B", "4096", "-s", "0",
                                        "-i", interface, "-w", str(Path(self.spool.name) / (label + ".pcap")), filter_],
                                       stdout=subprocess.DEVNULL, stderr=err)
            self.captures.append((process, err))
        time.sleep(1)
        if any(p.poll() is not None for p, _ in self.captures):
            raise RuntimeError("capture failed to start")
        self.save()

    def measure(self, ue, target):
        name, upf = self.gnb["workload"], "upf-" + self.gnb["slice"]
        endpoints = {key: json.loads(json.dumps(self.plan[key])) for key in (name, upf)}
        peer = self.city["slices"][self.gnb["slice"]]["n3Subnet"] + "." + str(self.gnb["n3Host"])
        selected = self.city["sites"][self.city["slices"][self.gnb["slice"]]["site"]]
        edge = min((s for s in self.city["sites"].values() if s["tier"] == "edge"),
                   key=lambda s: sum(abs(a-b) for a,b in zip(self.gnb["position"], s["position"])))
        central = next(s for s in self.city["sites"].values() if s["tier"] == "central")
        distributed = next(s for s in self.city["sites"].values() if s["tier"] == "distributed") if selected["tier"] != "distributed" else selected
        profiles = {"baseline": 0, "edge": delay_us(self.city, self.gnb, edge),
                    "distributed": delay_us(self.city, self.gnb, distributed), "central": delay_us(self.city, self.gnb, central)}
        if self.city_only:
            profiles = {"city": self.plan[name]["peers"][0]["delayUs"]}
        for profile, delay in profiles.items():
            for key, endpoint in endpoints.items():
                for entry in endpoint["peers"]:
                    if key == name or entry["ip"] == peer:
                        entry["delayUs"] = delay
                self.change(key, endpoint)
            ping = self.call("exec", ue, "-c", "ue", "--", "ping", "-n", "-I", "ueTun0", "-c", "25", "-i", "0.1", "-W", "2", target)
            (self.output / (profile + "-ping.txt")).write_text(ping.stdout)
            samples = [float(v) for v in re.findall(r"time=([\d.]+) ms", ping.stdout)]
            if len(samples) != 25:
                raise RuntimeError("missing ping responses for " + profile)
            self.report["measurements"][profile] = {"oneWayInjectedUs": delay, "expectedAddedRttMs": 2*delay/1000,
                "medianRttMs": statistics.median(samples), "received": len(samples),
                "placement": "actual configured city site" if self.city_only else "counterfactual N3 budget on the same UPF; no NF migration"}
            self.report["measurements"][profile]["qdiscs"] = {key: self.change(key, self.plan[key], "show") for key in endpoints}
            for qdiscs in self.report["measurements"][profile]["qdiscs"].values():
                delayed = [q for q in qdiscs if q["kind"] == "netem"]
                if sum(q.get("packets", 0) for q in delayed) < 25 or any(q.get("drops", 0) for q in delayed):
                    raise RuntimeError("netem did not forward all probes without queue drops")
            self.save()
        if not self.city_only:
            base = self.report["measurements"]["baseline"]["medianRttMs"]
            for profile in ("edge", "distributed", "central"):
                row = self.report["measurements"][profile]
                row["observedAddedRttMs"] = round(row["medianRttMs"] - base, 3)
                if abs(row["observedAddedRttMs"] - row["expectedAddedRttMs"]) > max(0.8, row["expectedAddedRttMs"] * .35):
                    raise RuntimeError("RTT delta outside functional tolerance: " + profile)
        # Return to the actual city placement for the existing iperf smoke test.
        for key in endpoints:
            self.change(key, self.plan[key])
        self.report["actualCityPlacement"] = {"site": self.city["slices"][self.gnb["slice"]]["site"],
            "oneWayInjectedUs": self.plan[name]["peers"][0]["delayUs"]}
        self.report["qdiscCounters"] = {key: self.change(key, self.plan[key], "show") for key in endpoints}
        self.save()

    def finish(self):
        errors = []
        try:
            self.report["afterTrafficQdiscs"] = {key: self.change(key, self.plan[key], "show")
                for key in (self.gnb["workload"], "upf-" + self.gnb["slice"]) if key in self.touched}
        except Exception as error:
            errors.append(str(error))
        for process, file in self.captures:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    errors.append("capture did not stop normally")
            file.close()
        for capture in Path(self.spool.name).glob("*.pcap"):
            try:
                shutil.copyfile(capture, self.output / capture.name)
            except OSError as error:
                errors.append("capture copy: " + str(error))
        self.spool.cleanup()
        for name in reversed(self.touched):
            try:
                self.change(name, self.plan[name], "remove")
            except Exception as error:
                errors.append(str(error))
        if self.gnb["workload"] in self.pods:
            try:
                pod = self.pods[self.gnb["workload"]][0]
                info = self.call("exec", pod["metadata"]["name"], "-c", "gnb-evidence", "--", "python3", "-c",
                    "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:40104/api/gnb/info').read().decode())")
                residuals = json.loads(info.stdout)["gnbInfo"]["ranUeList"]
                self.report["nativeGnbContextsAfterCleanup"] = residuals
                if residuals:
                    errors.append("native gNB retains UE contexts after Pod deletion; native cleanup required")
            except Exception as error:
                errors.append("native gNB cleanup readback: " + str(error))
        self.report["cleanup"] = "failed" if errors else "noqueue_restored"
        self.report["cleanupErrors"] = errors
        self.save()
        if errors:
            raise RuntimeError("transport cleanup failed: " + "; ".join(errors))

    def keep_city_plan(self):
        if self.report.get("status") != "passed":
            raise RuntimeError("cannot retain delay before complete acceptance")
        try:
            retained = {}
            for name, endpoint in self.plan.items():
                retained[name] = self.change(name, endpoint)
            self.report["cleanup"] = "city_plan_left_applied"
            self.report["retainedQdiscs"] = retained
            self.save()
        except Exception:
            for name in self.touched:
                self.change(name, self.plan[name], "remove")
            self.report.update(status="failed", cleanup="noqueue_restored_after_persist_failure")
            self.save()
            raise

    def decode(self, pdu_ip):
        fields = {
            "n2": ["frame.number", "ip.src", "ip.dst", "ngap.procedureCode", "ngap.NAS_PDU", "ngap.gTP_TEID"],
            "n3": ["frame.number", "ip.src", "ip.dst", "gtp.teid", "icmp.type", "udp.dstport", "icmp.ident", "icmp.seq"],
            "n4": ["frame.number", "ip.src", "ip.dst", "pfcp.msg_type", "pfcp.seid", "pfcp.f_teid.teid"],
            "n6": ["frame.number", "ip.src", "ip.dst", "icmp.type", "udp.dstport", "icmp.ident", "icmp.seq"],
        }
        decoded = {}
        for label, columns in fields.items():
            args = ["tshark", "-r", self.output / (label + ".pcap"), "-T", "fields", "-E", "separator=\t"]
            for field in columns:
                args += ["-e", field]
            result = local(*args)
            (self.output / (label + "-decoded.tsv")).write_text("\t".join(columns) + "\n" + result.stdout)
            decoded[label] = [line.split("\t") for line in result.stdout.splitlines()]
        n2, n3, n4, n6 = (decoded[k] for k in ("n2", "n3", "n4", "n6"))
        codes = sorted({row[3] for row in n2 if row[3]})
        pfcp = sorted({row[3] for row in n4 if row[3]})
        nas = [row for row in n2 if row[4]]
        teids = sorted({row[3] for row in n3 if row[3]})
        wire_teids = {int(value, 16) for value in teids}
        ngap_teids = {int(value, 16) for row in n2 for value in row[5].split(",") if value}
        pfcp_teids = {int(value, 16) for row in n4 for value in row[5].split(",") if value}
        n3_echo = {(row[4],row[6],row[7]) for row in n3 if row[4] in ("0", "8")}
        n6_echo = {(row[3],row[5],row[6]) for row in n6 if row[3] in ("0", "8")}
        expected = {"initialUeMessage": "15" in codes, "nasTransport": "46" in codes and "4" in codes,
                    "pduSessionResourceSetup": "29" in codes, "nasPduCarriedInNgap": bool(nas),
                    "pfcpSessionEstablishment": "50" in pfcp and "51" in pfcp,
                    "gtpuCarriesPduIp": any(pdu_ip in (row[1] + "," + row[2]).split(",") and row[3] for row in n3),
                    "gtpuBothDirections": len(teids) >= 2,
                    "gtpuTeidsMatchNgapAndPfcp": wire_teids.issubset(ngap_teids) and bool(wire_teids & pfcp_teids),
                    "sameIcmpTraversesN3AndN6": bool(n3_echo) and n3_echo.issubset(n6_echo),
                    "n6RequestAndReply": {"0", "8"}.issubset({row[3] for row in n6})}
        capture_drops = {}
        for label in ("n2", "n3-ran", "n3", "n4", "n6"):
            log = self.output / (label + "-capture.log")
            if log.exists():
                match = re.search(r"(\d+) packets dropped by kernel", log.read_text())
                capture_drops[label] = int(match[1]) if match else None
        expected["captureNoKernelDrops"] = len(capture_drops) == 5 and all(v == 0 for v in capture_drops.values())
        traffic_qdiscs = self.report.get("afterTrafficQdiscs", {})
        expected["netemNoQueueDrops"] = len(traffic_qdiscs) == 2 and all(q.get("drops", 0) == 0 for qs in traffic_qdiscs.values() for q in qs)
        self.report["protocols"] = {"checks": expected, "ngapProcedureCodes": codes, "nasPduCount": len(nas),
            "pfcpMessageTypes": pfcp, "gtpuTeids": teids, "packets": {k: len(v) for k,v in decoded.items()},
            "N1": "logical NAS UE-AMF, observed NAS-PDU inside N2 NGAP; not a user-plane hop",
            "N4": "SMF-UPF session programming, not a user payload hop", "captureKernelDrops": capture_drops}
        self.report["status"] = "passed" if all(expected.values()) and self.report["measurements"] and not self.report.get("cleanupErrors") else "failed"
        self.save()
        if self.report["status"] != "passed":
            raise RuntimeError("transport protocol acceptance failed; inspect transport/result.json")
