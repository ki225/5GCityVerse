"""Evidence-gated, opt-in first-run bootstrap for the local kubeadm city.

The browser never supplies Kubernetes targets or manifests. An operator may
preconfigure one SHA256-pinned render for a completely empty owned stack.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import yaml

from backend.onprem.runtime.ue import EvidenceUnavailable, ran_connected, validate_counts


CORE_DEPLOYMENTS = (
    "cityverse-free5gc-amf-amf", "cityverse-free5gc-ausf-ausf",
    "cityverse-free5gc-nrf-nrf", "cityverse-free5gc-pcf-pcf",
    "cityverse-free5gc-udm-udm", "cityverse-free5gc-udr-udr",
    "nssf", "smf", "upf-embb", "upf-urllc", "upf-mmtc", "upf-v2x",
)
GNB_DEPLOYMENTS = tuple(f"gnb-{name}" for name in ("embb", "urllc", "mmtc", "v2x"))
OWNED_DEPLOYMENTS = frozenset((*CORE_DEPLOYMENTS, *GNB_DEPLOYMENTS))
BASELINE = {"embb": 1, "urllc": 0, "mmtc": 0, "v2x": 0}
_RECONCILIATION_BLOCKER = "Current UE generation requires operator reconciliation before resize or retirement"


def _zero_verification_pending(current: dict, status: dict) -> bool:
    """Classify waiting only; a zero counter snapshot never proves release."""
    desired = current.get("desired")
    phase = status.get("status")
    return (type(current.get("generation")) is int and current["generation"] > 0
            and type(desired) is dict and set(desired) == set(BASELINE)
            and all(type(n) is int and n == 0 for n in desired.values())
            and current.get("allocation") == []
            and type(status.get("generation")) is int
            and status["generation"] == current["generation"]
            and phase in {"verification_unavailable", "stopped", "requested", "verifying_traffic"}
            and (phase == "verification_unavailable" or status.get("errors") == [])
            and all(type(status.get(key)) is int and status[key] == 0
                    for key in ("requested", "allocated", "registered", "pduEstablished", "trafficVerified"))
            and status.get("perUe") == [])


def _error(stage: str, code: str, detail: str, *, retryable: bool = False) -> dict:
    return {"state": "failed", "stage": stage, "message": detail,
            "error": {"code": code, "detail": detail, "retryable": retryable},
            "retryable": retryable}


def _phase(state: str, stage: str | None, message: str, **extra) -> dict:
    return {"state": state, "stage": stage, "message": message,
            "retryable": state in {"absent", "failed"}, **extra}


class CityBootstrap:
    def __init__(self, controller, *, driver=None, environ=None, guard=None, submit_baseline=None):
        self.controller = controller
        self.driver = driver or controller.driver or getattr(controller.adapter, "driver", None)
        self.environ = environ if environ is not None else os.environ
        self.guard = guard or (lambda: None)
        self.submit_baseline = submit_baseline or controller.request
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.attempt_id: str | None = None
        self.started_at = 0.0
        self.last_error: dict | None = None

    def desired(self) -> dict[str, int]:
        raw = self.environ.get("CITYVERSE_CITY_BASELINE_UE_COUNTS")
        counts = validate_counts(json.loads(raw)) if raw else dict(BASELINE)
        if not 1 <= sum(counts.values()) <= 50:
            raise ValueError("City baseline must contain 1–50 UEs")
        return counts

    def infrastructure(self) -> dict:
        if self.driver is None:
            raise EvidenceUnavailable("Native kubeadm evidence/mutation driver is not configured")
        reader = getattr(self.driver, '_metadata_pod_reader', None)
        namespace = (self.driver.kubectl("get", "namespace", self.driver.namespace, "-o", "json")
                     if reader is None else reader.namespace_record(deadline=time.monotonic() + 5))
        if namespace.get("metadata", {}).get("labels", {}).get("cityverse.io/platform") != "kubeadm":
            raise EvidenceUnavailable("Target namespace is not approved for kubeadm")
        deployments = (self.driver.kubectl("get", "deployments", "-o", "json") if reader is None
                       else reader.workloads('deployments', deadline=time.monotonic() + 5)).get("items", [])
        statefulsets = (self.driver.kubectl("get", "statefulsets", "-o", "json") if reader is None
                        else reader.workloads('statefulsets', deadline=time.monotonic() + 5)).get("items", [])
        found = {item.get("metadata", {}).get("name"): item for item in deployments}
        owned = OWNED_DEPLOYMENTS.intersection(found)
        mongo = next((item for item in statefulsets if item.get("metadata", {}).get("name") == "mongodb"), None)
        if not owned and mongo is None:
            return {"state": "absent", "stage": "core", "message": "Core network and gNB resources are absent"}
        if not set(CORE_DEPLOYMENTS).issubset(owned) or mongo is None:
            return _error("core", "CORE_PARTIAL_OR_DEGRADED",
                          "Core/gNB resources are partially present; operator reconciliation is required")
        if not set(GNB_DEPLOYMENTS).issubset(owned):
            return _error("gnb", "GNB_PARTIAL_OR_DEGRADED",
                          "gNB resources are partially present; operator reconciliation is required")
        def ready(item):
            spec = item.get("spec", {})
            status = item.get("status", {})
            replicas = spec.get("replicas", 1)
            return replicas > 0 and status.get("observedGeneration", 0) >= item.get("metadata", {}).get("generation", 1) and status.get("readyReplicas", 0) >= replicas
        if any(not ready(found[name]) for name in CORE_DEPLOYMENTS) or not ready(mongo):
            return _error("core", "CORE_PARTIAL_OR_DEGRADED",
                          "Core deployments or MongoDB are not fully Ready; inspect Kubernetes rollout", retryable=True)
        if any(not ready(found[name]) for name in GNB_DEPLOYMENTS):
            return _error("gnb", "GNB_NOT_READY", "One or more gNB deployments are not Ready", retryable=True)
        return _phase("ready", "gnb", "Core network and gNB workloads are Ready")

    def _ue_phase(self, status: dict, desired: dict) -> dict:
        current = self.controller.store.current()
        if current and current.get("desired") != desired:
            existing = current.get("desired") or {}
            existing_count = sum(existing.values()) if isinstance(existing, dict) else 0
            if (existing_count > 0 and status.get("status") == "established"
                    and status.get("registered", 0) >= existing_count
                    and status.get("pduEstablished", 0) >= existing_count
                    and status.get("trafficVerified", 0) >= existing_count
                    and ran_connected(status)):
                return _phase("ready", None, "Existing citizen UE fleet is verified")
            if existing_count == 0 and status.get("status") in {"stopped", "verification_unavailable"}:
                blocker = self.controller.mutation_blocker(desired)
                if status.get("status") == "stopped" and blocker is None:
                    return _phase("absent", "ue", "Citizen UE baseline is not active")
                if blocker in (None, _RECONCILIATION_BLOCKER):
                    # mutation_blocker reads status independently. Observation
                    # expiry/reconciliation can change it between these reads.
                    # Even a recovered third stopped sample is waiting here,
                    # never a substitute for the successful original gate.
                    latest = self.controller.status()
                    owner = self.controller.store.current()
                    if (isinstance(owner, dict) and owner.get("generation") == current.get("generation")
                            and owner.get("desired") == current.get("desired")
                            and _zero_verification_pending(owner, latest)):
                        return _error("ue", "UE_ZERO_SETTLING",
                                      "Zero-UE native verification is incomplete or changed between reads; "
                                      "wait for a fresh stopped observation; release is not yet confirmed",
                                      retryable=True)
            if (existing_count == 0 and status.get("generation") == current.get("generation")
                    and status.get("status") in {"requested", "verifying_traffic"}
                    and not status.get("errors")
                    and all(status.get(key) == 0 for key in ("requested", "registered", "pduEstablished", "trafficVerified"))):
                return _error("ue", "UE_ZERO_SETTLING",
                              "Previous zero-UE generation is still being verified; retry after it reaches stopped",
                              retryable=True)
            return _error("ue", "UE_GENERATION_CONFLICT",
                          "An existing UE generation has different desired counts; operator reconciliation is required")
        if current is None:
            return _phase("absent", "ue", "City citizen UE baseline has not been requested")
        requested = sum(desired.values())
        if status.get("status") in {"failed", "degraded", "verification_unavailable"}:
            return _error("ue", "UE_EVIDENCE_UNAVAILABLE", "; ".join(status.get("errors") or []) or
                          "UE registration evidence is unavailable", retryable=True)
        if status.get("registered", 0) < requested:
            return _phase("starting", "ue", "Citizen UEs are registering")
        if status.get("pduEstablished", 0) < requested:
            return _phase("starting", "pdu", "UE PDU sessions are being established")
        if status.get("status") != "established" or status.get("trafficVerified", 0) < requested:
            return _phase("starting", "pdu", "UE connectivity evidence is being verified")
        if not ran_connected(status):
            return _error("gnb", "GNB_ASSOCIATION_LOST", "Fresh gNB NGAP/SCTP association evidence is unavailable", retryable=True)
        return _phase("ready", None, "Core, gNB and citizen UEs are verified")

    async def status(self) -> dict:
        if self.environ.get("CITYVERSE_ONPREM_RUNTIME") != "kubeadm":
            return _error("core", "KUBEADM_RUNTIME_REQUIRED", "City bootstrap requires the on-prem kubeadm runtime")
        try:
            desired = self.desired()
            infra = await asyncio.to_thread(self.infrastructure)
            if infra["state"] == "ready":
                result = self._ue_phase(self.controller.status(), desired)
            else:
                result = infra
        except (EvidenceUnavailable, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            result = _error("core", "INFRASTRUCTURE_UNAVAILABLE", str(exc), retryable=True)
        except Exception:
            result = _error("core", "INFRASTRUCTURE_CHECK_FAILED",
                            "Infrastructure check stopped unexpectedly; inspect backend logs and retry", retryable=True)
        if self.task is not None and not self.task.done():
            if time.monotonic() - self.started_at > 900:
                result = _error(result.get("stage") or "core", "BOOTSTRAP_TIMEOUT",
                                "City initialization exceeded 15 minutes; current operation is still draining, then retry", retryable=False)
            elif result["state"] != "ready":
                result = _phase("starting", self.stage, self.message)
        elif self.last_error and result["state"] != "ready":
            result = self.last_error
        if self.attempt_id:
            result["attemptId"] = self.attempt_id
        return result

    async def start(self) -> tuple[dict, int]:
        async with self.lock:
            observed = await self.status()
            if observed["state"] == "ready":
                return observed, 200
            if self.task is not None and not self.task.done():
                return observed, 409 if observed["state"] == "failed" else 202
            blocked = self.guard()
            if blocked:
                result = _error("core", "BOOTSTRAP_ADMISSION_BLOCKED", str(blocked), retryable=True)
                return result, 409
            self.last_error = None
            self.attempt_id = str(uuid4())
            self.stage = observed.get("stage") or "core"
            self.message = "Checking city infrastructure"
            self.started_at = time.monotonic()
            self.task = asyncio.create_task(self._run())
            return {**_phase("starting", self.stage, self.message), "attemptId": self.attempt_id}, 202

    def _pinned_manifest(self) -> bytes:
        filename = self.environ.get("CITYVERSE_CITY_BOOTSTRAP_MANIFEST")
        expected = self.environ.get("CITYVERSE_CITY_BOOTSTRAP_SHA256", "")
        kubeconfig = self.environ.get("KUBECONFIG", "")
        if not filename or not Path(filename).is_absolute() or not Path(filename).is_file() or not kubeconfig or not Path(kubeconfig).is_absolute() or not Path(kubeconfig).is_file() or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            raise ValueError("Operator must configure an absolute manifest, SHA256 pin and KUBECONFIG")
        path = Path(filename)
        if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Pinned city manifest must be a regular file smaller than 16 MiB")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("Pinned city manifest is missing or its SHA256 does not match")
        try:
            documents = list(yaml.safe_load_all(raw.decode("utf-8")))
        except (OSError, UnicodeError, yaml.YAMLError):
            raise ValueError("Pinned city manifest is not readable YAML") from None
        if not documents or any(
            not isinstance(item, dict) or item.get("kind") not in {
                "ConfigMap", "Deployment", "Service", "Secret", "NetworkAttachmentDefinition",
                "NetworkPolicy", "PodDisruptionBudget", "ServiceAccount", "StatefulSet", "Job",
            } or item.get("metadata", {}).get("namespace") != self.driver.namespace
            for item in documents
        ):
            raise ValueError("Pinned city manifest contains unapproved kind or namespace")
        deployed = {item["metadata"].get("name") for item in documents if item["kind"] == "Deployment"}
        stores = {item["metadata"].get("name") for item in documents if item["kind"] == "StatefulSet"}
        if not OWNED_DEPLOYMENTS.issubset(deployed) or "mongodb" not in stores:
            raise ValueError("Pinned city manifest does not contain the complete Core/gNB stack")
        return raw

    def _apply_absent_stack(self) -> None:
        manifest = self._pinned_manifest()
        if self.infrastructure()["state"] != "absent":
            raise EvidenceUnavailable("Core/gNB resources appeared before apply; operator reconciliation is required")
        command = ["kubectl", "--kubeconfig", self.environ["KUBECONFIG"], "--context", self.driver.context,
                   "--namespace", self.driver.namespace, "apply", "-f", "-"]
        try:
            result = subprocess.run(command, input=manifest, capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise EvidenceUnavailable("Pinned Core/gNB manifest apply timed out or kubectl is unavailable") from None
        if result.returncode:
            raise EvidenceUnavailable("Pinned Core/gNB manifest apply failed; inspect operator Kubernetes events")

    async def _run(self) -> None:
        try:
            desired = self.desired()
            infra = await asyncio.to_thread(self.infrastructure)
            if infra["state"] == "absent":
                self.stage, self.message = "core", "Applying operator-pinned Core and gNB manifest"
                try:
                    await asyncio.to_thread(self._apply_absent_stack)
                except ValueError as exc:
                    raise BootstrapFailure("core", "CORE_PROVISIONING_NOT_CONFIGURED", str(exc)) from None
            elif infra["state"] == "failed":
                raise BootstrapFailure(infra["stage"], infra["error"]["code"], infra["message"])
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                infra = await asyncio.to_thread(self.infrastructure)
                if infra["state"] == "ready":
                    break
                self.stage = infra["stage"]
                self.message = "Waiting for Core and gNB rollouts"
                await asyncio.sleep(2)
            else:
                raise BootstrapFailure(self.stage, "INFRASTRUCTURE_ROLLOUT_TIMEOUT",
                                       "Core/gNB rollout did not complete within five minutes")
            self.stage, self.message = "gnb", "Waiting for fresh NGAP/SCTP associations"
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if ran_connected(self.controller.status()):
                    break
                await asyncio.sleep(2)
            else:
                raise BootstrapFailure("gnb", "GNB_ASSOCIATION_TIMEOUT",
                                       "gNB did not establish fresh NGAP/SCTP associations")
            self.stage, self.message = "ue", "Requesting citizen UE baseline"
            blocked = self.guard()
            if blocked:
                raise BootstrapFailure("ue", "BOOTSTRAP_ADMISSION_BLOCKED", str(blocked))
            current = self.controller.store.current()
            if current and self._ue_phase(self.controller.status(), desired)["state"] == "ready":
                return
            if current is None or current.get("desired") != desired:
                blocker = self.controller.mutation_blocker(desired)
                if blocker:
                    raise BootstrapFailure("ue", "UE_GENERATION_CONFLICT", blocker)
                await self.submit_baseline(desired)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                phase = self._ue_phase(self.controller.status(), desired)
                if phase["state"] == "ready":
                    self.last_error = None
                    return
                if (phase["state"] == "failed"
                        and phase["error"]["code"] not in {"UE_EVIDENCE_UNAVAILABLE", "GNB_ASSOCIATION_LOST"}):
                    raise BootstrapFailure(phase["stage"], phase["error"]["code"], phase["message"])
                self.stage, self.message = phase["stage"], phase["message"]
                await asyncio.sleep(2)
            if phase.get("error", {}).get("code") == "GNB_ASSOCIATION_LOST":
                raise BootstrapFailure("gnb", "GNB_ASSOCIATION_TIMEOUT",
                                       "Fresh gNB NGAP/SCTP association evidence did not recover within five minutes")
            raise BootstrapFailure(self.stage, "UE_BOOTSTRAP_TIMEOUT",
                                   "Citizen UE registration/PDU/connectivity was not verified within five minutes")
        except BootstrapFailure as exc:
            self.last_error = _error(exc.stage, exc.code, exc.detail, retryable=exc.code.endswith("TIMEOUT"))
        except (EvidenceUnavailable, ValueError, OSError) as exc:
            self.last_error = _error(self.stage, "BOOTSTRAP_OPERATION_FAILED", str(exc), retryable=True)
        except Exception:
            # A failed background task must never leave the UI polling forever.
            self.last_error = _error(self.stage, "BOOTSTRAP_INTERNAL_ERROR",
                                     "Bootstrap stopped unexpectedly; inspect backend logs and retry", retryable=True)


class BootstrapFailure(Exception):
    def __init__(self, stage: str, code: str, detail: str):
        self.stage, self.code, self.detail = stage, code, detail
        super().__init__(detail)
