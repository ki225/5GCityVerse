from __future__ import annotations

import datetime as dt
import re
import time
import urllib.parse
from typing import Any, Callable

from agent_runtime.data_plane_evidence import DataPlaneEvidence
from eks_kubernetes_client import get_eks_client


class KubernetesDataPlaneEvidenceReader:
    """Read and correlate independently produced collector Job logs."""

    def __init__(
        self,
        cluster_name: str,
        namespace: str,
        client_factory: Callable[[str], Any] = get_eks_client,
        max_age_seconds: int = 180,
    ) -> None:
        self.cluster_name = cluster_name
        self.namespace = namespace
        self.client_factory = client_factory
        self.max_age_seconds = max_age_seconds

    def read(
        self,
        execution_id: str,
        sst: int,
        sd: str,
        dnn: str,
        not_before: str,
        expected_mbps: float,
        before_mbps: float,
    ) -> dict[str, Any]:
        if not self.cluster_name or not execution_id:
            return {}
        client = self.client_factory(self.cluster_name)
        path = f"/api/v1/namespaces/{urllib.parse.quote(self.namespace)}/pods?labelSelector=" + urllib.parse.quote(
            "app.kubernetes.io/name=pfcp-evidence-collector"
        )
        status, response = client.request("GET", path)
        if status >= 300 or not isinstance(response, dict):
            return {}
        pods = sorted(
            response.get("items") or [],
            key=lambda pod: str(((pod.get("status") or {}).get("startTime") or "")),
            reverse=True,
        )
        for pod in pods[:5]:
            name = str((pod.get("metadata") or {}).get("name") or "")
            if not name:
                continue
            log_path = f"/api/v1/namespaces/{urllib.parse.quote(self.namespace)}/pods/{urllib.parse.quote(name)}/log"
            log_status, text = client.request_text("GET", log_path)
            if log_status >= 300:
                continue
            artifact = self.correlate(
                text, execution_id, sst, sd, dnn, not_before, expected_mbps, before_mbps
            )
            if artifact:
                artifact["collectorPod"] = name
                artifact["reader"] = "kubernetes-job-log"
                return artifact
        return {}

    def read_until(
        self,
        execution_id: str,
        sst: int,
        sd: str,
        dnn: str,
        not_before: str,
        expected_mbps: float,
        before_mbps: float,
        *,
        wait_seconds: float = 90,
        poll_seconds: float = 5,
    ) -> dict[str, Any]:
        """Wait a bounded interval for the independent CronJob artifact."""
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            evidence = self.read(
                execution_id, sst, sd, dnn, not_before, expected_mbps, before_mbps
            )
            if evidence or time.monotonic() >= deadline:
                return evidence
            time.sleep(min(max(0.0, poll_seconds), max(0.0, deadline - time.monotonic())))

    def correlate(
        self,
        text: str,
        execution_id: str,
        sst: int,
        sd: str,
        dnn: str,
        not_before: str,
        expected_mbps: float,
        before_mbps: float,
    ) -> dict[str, Any]:
        for header, body in self.artifacts(text):
            metadata = dict(re.findall(r"([A-Za-z][A-Za-z0-9]*)=([^\s]+)", header))
            if metadata.get("executionId") != execution_id:
                continue
            if metadata.get("sst") != str(sst) or metadata.get("sd") != str(sd) or metadata.get("dnn") != dnn:
                continue
            if not self.in_time_window(metadata.get("collectedAt", ""), not_before):
                continue
            pfcp_text = self.section(body, "PFCP")
            kernel_text = self.section(body, "KERNEL")
            iperf_text = self.section(body, "IPERF")
            if not pfcp_text or not kernel_text or not iperf_text:
                continue
            if not self.pfcp_matches_slice(pfcp_text, sst, sd, dnn):
                continue
            pfcp = DataPlaneEvidence.parse_pfcp_log(pfcp_text)
            kernel = DataPlaneEvidence.parse_gtp5g_dump(kernel_text)
            shared_teids = sorted(set(pfcp.get("teids") or []) & set(kernel.get("teids") or []))
            shared_seids = sorted(set(pfcp.get("seids") or []) & set(kernel.get("seids") or []))
            throughput = self.iperf_throughput(iperf_text)
            provenance = re.search(r"^PROVENANCE\s+pod=([^\s]+)\s+source=iperf3$", iperf_text, re.MULTILINE)
            if not shared_teids or not shared_seids or not provenance:
                continue
            if not self.effect_matches_target(before_mbps, throughput, expected_mbps):
                continue
            return {
                "correlation": {
                    "executionId": execution_id,
                    "sst": sst,
                    "sd": sd,
                    "dnn": dnn,
                    "collectedAt": metadata["collectedAt"],
                    "sharedTeids": shared_teids,
                    "sharedSeids": shared_seids,
                },
                "pfcp": pfcp,
                "kernel": kernel,
                "effect": {
                    "measurementSource": "ue-tun-iperf3",
                    "beforeMbps": before_mbps,
                    "iperfPod": provenance.group(1),
                    "afterMbps": throughput,
                    "expectedMbps": expected_mbps,
                    "deltaMbps": round(throughput - before_mbps, 3),
                },
            }
        return {}

    @staticmethod
    def artifacts(text: str) -> list[tuple[str, str]]:
        return [
            (match.group(1), match.group(2))
            for match in re.finditer(r"^BEGIN_ARTIFACT\s+([^\n]+)\n(.*?)^END_ARTIFACT$", text, re.MULTILINE | re.DOTALL)
        ]

    @staticmethod
    def section(text: str, name: str) -> str:
        match = re.search(rf"^BEGIN_{name}$\n(.*?)^END_{name}$", text, re.MULTILINE | re.DOTALL)
        return match.group(1) if match else ""

    def in_time_window(self, collected_at: str, not_before: str) -> bool:
        try:
            collected = self.parse_time(collected_at)
            started = self.parse_time(not_before)
        except ValueError:
            return False
        now = dt.datetime.now(dt.timezone.utc)
        # A matching execution nonce is necessary but not sufficient: accepting
        # pre-actuation evidence would turn correlation into false causality.
        clock_skew = dt.timedelta(seconds=5)
        return (
            started - clock_skew <= collected <= now + clock_skew
            and (now - collected).total_seconds() <= self.max_age_seconds
        )

    @staticmethod
    def effect_matches_target(before_mbps: float, after_mbps: float, expected_mbps: float) -> bool:
        if before_mbps < 0 or after_mbps <= 0 or expected_mbps <= 0:
            return False
        tolerance = max(0.02, expected_mbps * 0.15)
        if abs(after_mbps - expected_mbps) > tolerance:
            return False
        minimum_delta = max(0.02, expected_mbps * 0.10)
        if expected_mbps > before_mbps:
            return after_mbps - before_mbps >= minimum_delta
        if expected_mbps < before_mbps:
            return before_mbps - after_mbps >= minimum_delta
        return False

    @staticmethod
    def parse_time(value: str) -> dt.datetime:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)

    @staticmethod
    def pfcp_matches_slice(text: str, sst: int, sd: str, dnn: str) -> bool:
        lowered = text.lower()
        sst_match = re.search(rf"\bsst\s*[:=]\s*{int(sst)}\b", lowered)
        sd_match = re.search(rf"\bsd\s*[:=]\s*0*{re.escape(str(sd).lstrip('0') or '0')}\b", lowered)
        dnn_match = re.search(rf"\bdnn\s*[:=]\s*{re.escape(dnn.lower())}\b", lowered)
        return bool(sst_match and sd_match and dnn_match)

    @staticmethod
    def iperf_throughput(text: str) -> float:
        values = [float(value) for value in re.findall(r"(\d+(?:\.\d+)?)\s+Mbits/sec", text)]
        return values[-1] if values else 0.0
