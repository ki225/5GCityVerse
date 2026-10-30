"""Live fixed 10 Mbps UL/DL producer guarded by fresh native session evidence."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any, Mapping

from backend.onprem.evidence.collector import (NativeCollector,
    authoritative_inventory_options, verify_authoritative_smf_runtime)
from backend.onprem.integrations.oam import NativeOamAdapter
from backend.onprem.traffic.capability import IperfCapabilityTraffic, PolicyBlocked
from backend.onprem.runtime.free_ran_ue import FreeRanKubeadmDriver, UE_IMAGE
from backend.onprem.storage.readonly import ReadOnlyGenerationStore
from backend.onprem.runtime.kubeadm import INIT_IMAGE
from backend.onprem.runtime.ue import EvidenceUnavailable, EvidenceVerifier, SLICES


class FunctionalTrafficBlocked(ValueError):
    pass


_RUNNER = ("context", "kubeconfig", "namespace", "uePod", "ueUid", "receiverPod",
           "receiverUid", "supi", "pduSessionId", "ueIp", "tun", "endpoint", "port")
_ENV = ("CITYVERSE_NATIVE_SESSION_GENERATION", "CITYVERSE_ONPREM_AMF_OAM_URL",
        "CITYVERSE_ONPREM_SMF_OAM_URL", "CITYVERSE_ONPREM_UPF_EVIDENCE_URLS")


def validate_functional_traffic_config(config: Mapping[str, Any]) -> None:
    runner, environ = config.get("runner"), config.get("producerEnvironment")
    if not isinstance(runner, Mapping) or not isinstance(environ, Mapping):
        raise FunctionalTrafficBlocked("functional runner/producer environment missing")
    missing = [key for key in _RUNNER if runner.get(key) in (None, "")]
    missing += [key for key in _ENV if not isinstance(environ.get(key), str) or not environ[key]]
    ledger = environ.get("CITYVERSE_ONPREM_UE_LEDGER") or environ.get("CITYVERSE_ONPREM_UE_STATE_FILE")
    if not isinstance(ledger, str) or not ledger:
        missing.append("CITYVERSE_ONPREM_UE_LEDGER|CITYVERSE_ONPREM_UE_STATE_FILE")
    if missing:
        raise FunctionalTrafficBlocked("functional live inputs incomplete: " + ",".join(sorted(missing)))
    if type(runner.get("port")) is not int or not 1024 <= runner["port"] < 65535:
        raise FunctionalTrafficBlocked("functional port is invalid")
    if (environ.get("CITYVERSE_ONPREM_KUBE_CONTEXT", runner["context"]) != runner["context"]
            or environ.get("CITYVERSE_ONPREM_NAMESPACE", runner["namespace"]) != runner["namespace"]
            or environ.get("CITYVERSE_ONPREM_RAN_BACKEND", "free-ran-ue") != "free-ran-ue"):
        raise FunctionalTrafficBlocked("functional collector and traffic Kubernetes targets differ")
    try:
        generation = int(environ["CITYVERSE_NATIVE_SESSION_GENERATION"])
        urls = json.loads(environ["CITYVERSE_ONPREM_UPF_EVIDENCE_URLS"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FunctionalTrafficBlocked("functional native generation/UPF inventory invalid") from exc
    if generation < 0 or set(urls) != set(SLICES) or any(not isinstance(v, str) or not v for v in urls.values()):
        raise FunctionalTrafficBlocked("functional native generation/UPF inventory invalid")
    for label, value in (("kubeconfig", runner["kubeconfig"]), ("UE ledger", ledger)):
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            raise FunctionalTrafficBlocked(f"functional {label} must be an existing absolute file")


def _configured(config: Mapping[str, Any]):
    validate_functional_traffic_config(config)
    runner, environ = config["runner"], config["producerEnvironment"]
    ledger = environ.get("CITYVERSE_ONPREM_UE_LEDGER") or environ["CITYVERSE_ONPREM_UE_STATE_FILE"]
    store = ReadOnlyGenerationStore(Path(ledger))
    kubeconfig = str(Path(runner["kubeconfig"]))
    def kube_runner(command, **kwargs):
        argv = list(command)
        if argv and Path(str(argv[0])).name.lower() in {"kubectl", "kubectl.exe"} and "--kubeconfig" not in argv:
            argv[1:1] = ["--kubeconfig", kubeconfig]
        return subprocess.run(argv, **kwargs)
    driver = FreeRanKubeadmDriver(
        environ.get("CITYVERSE_ONPREM_KUBE_CONTEXT", runner["context"]),
        environ.get("CITYVERSE_ONPREM_NAMESPACE", runner["namespace"]),
        batch_size=int(environ.get("CITYVERSE_ONPREM_UE_BATCH_SIZE", "50")),
        batch_interval=float(environ.get("CITYVERSE_ONPREM_UE_BATCH_INTERVAL", "30")),
        tempo_ms=int(environ.get("CITYVERSE_ONPREM_UE_TEMPO_MS", "500")),
        image=environ.get("CITYVERSE_ONPREM_FREE_RAN_UE_IMAGE", UE_IMAGE),
        init_image=environ.get("CITYVERSE_ONPREM_UE_INIT_IMAGE", INIT_IMAGE), traffic_enabled=False,
        runner=kube_runner)
    smf_url = environ["CITYVERSE_ONPREM_SMF_OAM_URL"].rstrip("/")
    options = authoritative_inventory_options(environ, smf_url)
    image = options.pop("inventory_image", None)
    if image is not None:
        verify_authoritative_smf_runtime(driver, image)
    native = NativeOamAdapter(environ["CITYVERSE_ONPREM_AMF_OAM_URL"], smf_url,
        environ.get("CITYVERSE_ONPREM_OAM_TOKEN", ""), reference_db=None, **options)
    urls = json.loads(environ["CITYVERSE_ONPREM_UPF_EVIDENCE_URLS"])
    subscribers = SimpleNamespace(collect=lambda: {"verifiedCount": None,
        "credentialsMatched": None, "source": "not-required:functional-traffic"})
    collector = NativeCollector(store, driver, native, subscribers, urls, runner=kube_runner)
    traffic = IperfCapabilityTraffic("kubectl", Path(runner["kubeconfig"]),
                                     runner["context"], runner["namespace"])
    return store, collector, traffic


class NativeFunctionalTraffic:
    def __init__(self, store, collector, traffic, runner: Mapping[str, Any]):
        self.store, self.collector, self.traffic, self.runner = store, collector, traffic, dict(runner)
        self.generation = int(self.runner["generation"])

    def _status(self) -> tuple[dict[str, Any], dict[str, Any]]:
        generation = self.store.current()
        if not isinstance(generation, Mapping) or generation.get("generation") != self.generation:
            raise FunctionalTrafficBlocked("functional generation is no longer current")
        evidence = self.collector.observe(self.generation)
        status = EvidenceVerifier(stable_seconds=0).evaluate(dict(generation), evidence)
        if status.get("status") != "established":
            raise FunctionalTrafficBlocked("fresh native PDU/TUN evidence is not established")
        rows = [row for row in status.get("perUe", []) if row.get("supi") == self.runner["supi"]]
        if len(rows) != 1:
            raise FunctionalTrafficBlocked("unique functional SUPI is unavailable")
        return dict(rows[0]), evidence

    def _binding(self) -> dict[str, Any]:
        row, _ = self._status()
        expected = {"sessionId": str(self.runner["pduSessionId"]), "ip": self.runner["ueIp"],
                    "interface": self.runner["tun"], "podUid": self.runner["ueUid"]}
        if any(row.get(key) != value for key, value in expected.items()) or row.get("trafficVerified") is not True:
            raise FunctionalTrafficBlocked("functional runner identity differs from native evidence")
        required = ("slice", "upf", "pfcpSeid", "upfEpoch")
        if any(not row.get(key) for key in required):
            raise FunctionalTrafficBlocked("native UPF/PFCP binding is incomplete")
        return {"supi": self.runner["supi"], "scenario": "functional-traffic",
                "slice": row["slice"], "ueIp": row["ip"], "ip": row["ip"],
                "tun": row["interface"], "sessionId": row["sessionId"],
                "podUid": row["podUid"], "upf": row["upf"], "pfcpSeid": row["pfcpSeid"],
                "upfEpoch": row["upfEpoch"], "generation": self.generation,
                "smContextRef": row.get("smContextRef"), "dnn": row.get("dnn"),
                "sst": row.get("sst"), "sd": row.get("sd")}

    def guard(self, binding: Mapping[str, Any]) -> bool:
        try:
            current = self._binding()
        except (FunctionalTrafficBlocked, PolicyBlocked, EvidenceUnavailable,
                KeyError, TypeError, ValueError):
            return False
        return current == dict(binding)

    def run(self, *, run_id: str, epoch: str, identity: Mapping[str, Any]) -> dict[str, Any]:
        if (not run_id or not epoch or not isinstance(identity, Mapping)
                or identity.get("supi") != self.runner["supi"]
                or identity.get("pduSessionId") != self.runner["pduSessionId"]
                or identity.get("ueIp") != self.runner["ueIp"]
                or identity.get("tun") != self.runner["tun"]
                or identity.get("receiverId") != self.runner["receiverUid"]):
            raise FunctionalTrafficBlocked("functional run/session identity mismatch")
        binding = self._binding()
        reports = []
        for offset, direction in enumerate(("ul", "dl")):
            reports.append(self.traffic.sample(
                binding, self.runner["uePod"], self.runner["receiverPod"],
                self.runner["receiverUid"], self.runner["endpoint"], self.runner["port"] + offset,
                direction=direction, offered_mbps=10, duration=5, guard=self.guard))
        return {"status": "complete", "runId": run_id, "epoch": epoch,
                "identity": dict(identity), "reports": reports}


def run_functional_traffic(config: Mapping[str, Any], *, run_id: str, epoch: str,
                           identity: Mapping[str, Any]) -> dict[str, Any]:
    store, collector, traffic = _configured(config)
    runner = dict(config["runner"])
    runner["generation"] = int(config["producerEnvironment"]["CITYVERSE_NATIVE_SESSION_GENERATION"])
    return NativeFunctionalTraffic(store, collector, traffic, runner).run(
        run_id=run_id, epoch=epoch, identity=identity)
