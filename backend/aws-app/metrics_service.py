from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any

from constants import DataSource, EvidenceLevel
from slice_catalog import SliceCatalog
from time_utils import TimeUtils


class PrometheusMetricsService:
    def __init__(self, prometheus_url: str) -> None:
        self.prometheus_url = prometheus_url
        self.query_timeout = float(os.environ.get("PROMETHEUS_QUERY_TIMEOUT_SECONDS", "0.8"))

    def query(self, promql: str) -> float | None:
        if not self.prometheus_url:
            return None
        query = urllib.parse.urlencode({"query": promql})
        url = f"{self.prometheus_url}/api/v1/query?{query}"
        try:
            with urllib.request.urlopen(url, timeout=self.query_timeout) as res:
                data = json.loads(res.read().decode("utf-8") or "{}")
                results = data.get("data", {}).get("result", [])
                if results:
                    return float(results[0]["value"][1])
        except Exception as exc:
            print(f"Prometheus query failed: {promql}: {exc}")
        return None

    def first_value(self, *queries: str) -> float | None:
        for query in queries:
            value = self.query(query)
            if value is not None:
                return value
        return None

    def unavailable_metrics(self) -> dict[str, Any]:
        return {
            "upfCpuPercent": 0.0,
            "upfPodCount": 0,
            "amfPodCount": 0,
            "gtpPacketsPerSec": 0,
            "pduSessionCount": 0,
            "latencyMs": 0.0,
            "throughputMbps": 0.0,
            "timestamp": TimeUtils.epoch_millis(),
            "dataSource": DataSource.UNAVAILABLE.value,
            "evidenceLevel": EvidenceLevel.FALLBACK.value,
        }

    def get_real_metrics(self) -> dict[str, Any] | None:
        uplink_bytes = self.first_value(
            'rate(free5gc_upf_bytes_total{direction="uplink"}[10s])',
            'sum(rate(container_network_receive_bytes_total{namespace="free5gc",pod=~".*upf.*"}[30s]))',
        )
        downlink_bytes = self.first_value(
            'rate(free5gc_upf_bytes_total{direction="downlink"}[10s])',
            'sum(rate(container_network_transmit_bytes_total{namespace="free5gc",pod=~".*upf.*"}[30s]))',
        )
        active_sessions = self.first_value("free5gc_smf_pdu_session_active", "sum(free5gc_smf_pdu_session_count)")
        registered_ues = self.first_value("free5gc_amf_registered_ue_total")
        gtp_packets = self.first_value("rate(free5gc_upf_packets_total[10s])", "sum(rate(gtp5g_packet_count[30s]))")
        upf_cpu = self.first_value('sum(rate(container_cpu_usage_seconds_total{namespace="free5gc",pod=~".*upf.*"}[30s])) * 100')
        amf_cpu = self.first_value('sum(rate(container_cpu_usage_seconds_total{namespace="free5gc",pod=~".*amf.*"}[30s])) * 100')

        values = [uplink_bytes, downlink_bytes, active_sessions, registered_ues, gtp_packets, upf_cpu, amf_cpu]
        if all(value is None for value in values):
            return None

        uplink_bytes = uplink_bytes or 0.0
        downlink_bytes = downlink_bytes or 0.0
        upf_pods = self.first_value('count(kube_pod_status_phase{namespace="free5gc",pod=~".*upf.*",phase="Running"})')
        amf_pods = self.first_value('count(kube_pod_status_phase{namespace="free5gc",pod=~".*amf.*",phase="Running"})')
        return {
            "upfCpuPercent": round(upf_cpu or 0.0, 1),
            "upfPodCount": int(upf_pods or 0),
            "amfPodCount": int(amf_pods or 0),
            "amfCpuPercent": round(amf_cpu or 0.0, 1),
            "registeredUeCount": int(registered_ues or 0),
            "gtpPacketsPerSec": int(gtp_packets or 0),
            "pduSessionCount": int(active_sessions or 0),
            "latencyMs": round(self.first_value("avg(free5gc_upf_packet_latency_ms)") or 0.0, 1),
            "throughputMbps": round((uplink_bytes + downlink_bytes) * 8 / 1_000_000, 2),
            "uplinkMbps": round(uplink_bytes * 8 / 1_000_000, 2),
            "downlinkMbps": round(downlink_bytes * 8 / 1_000_000, 2),
            "timestamp": TimeUtils.epoch_millis(),
            "dataSource": DataSource.PROMETHEUS.value,
            "evidenceLevel": EvidenceLevel.MEASURED.value,
        }

    def current_metrics(self) -> dict[str, Any]:
        return self.get_real_metrics() or self.unavailable_metrics()

    def metrics_from_free5gc(self, registered_ues: list[dict[str, Any]]) -> dict[str, Any]:
        metrics = self.unavailable_metrics()
        pdu_sessions = 0
        for ue in registered_ues:
            sessions = ue.get("PduSessions") or ue.get("pduSessions") or []
            if isinstance(sessions, list):
                pdu_sessions += len(sessions)
        metrics.update(
            {
                "pduSessionCount": pdu_sessions,
                "registeredUeCount": len(registered_ues),
                "timestamp": TimeUtils.epoch_millis(),
                "dataSource": DataSource.FREE5GC.value,
                "evidenceLevel": EvidenceLevel.ESTIMATED.value,
            }
        )
        return metrics

    def real_slice_metrics(self) -> list[dict[str, Any]] | None:
        if not self.prometheus_url:
            return None
        raw = {
            1: self.query('rate(free5gc_upf_bytes_total{sst="1"}[10s])'),
            2: self.query('rate(free5gc_upf_bytes_total{sst="2"}[10s])'),
            3: self.query('rate(free5gc_upf_bytes_total{sst="3"}[10s])'),
            4: self.query('rate(free5gc_upf_bytes_total{sst="4"}[10s])'),
        }
        sessions = {
            sst: int(self.query(f'free5gc_smf_pdu_session_active{{sst="{sst}"}}') or 0)
            for sst in raw
        }
        return SliceCatalog.slices_from_prometheus(raw, sessions)
