from __future__ import annotations

import os

from constants import EventType, RiskLevel, SliceType
from models import Bandwidth, EventConfig


class AppSettings:
    def __init__(self) -> None:
        self.dynamodb_table = os.environ.get("DYNAMODB_TABLE", "")
        self.apigw_ws_endpoint = os.environ.get("APIGW_WS_ENDPOINT", "")
        self.free5gc_webui_url = os.environ.get("FREE5GC_WEBUI_URL", "").rstrip("/")
        self.free5gc_webui_username = os.environ.get("FREE5GC_WEBUI_USERNAME", "admin")
        self.free5gc_webui_password = os.environ.get("FREE5GC_WEBUI_PASSWORD", "free5gc")
        self.free5gc_plmn_id = os.environ.get("FREE5GC_PLMN_ID", "20893")
        self.free5gc_imsi_prefix = os.environ.get("FREE5GC_IMSI_PREFIX", "20893000000")
        self.prometheus_url = os.environ.get("PROMETHEUS_URL", "").rstrip("/")
        self.eks_cluster_name = os.environ.get("EKS_CLUSTER_NAME", "")
        self.free5gc_namespace = os.environ.get("FREE5GC_NAMESPACE", "free5gc")
        self.ueransim_ue_deployment = os.environ.get("UERANSIM_UE_DEPLOYMENT", "ueransim-city-ue")
        self.ueransim_image = os.environ.get(
            "UERANSIM_IMAGE",
            "free5gc/ueransim@sha256:4a6745b0c9f0c60173833f8bef89816324e84636e220917bdc682555a299e8ba",
        )
        self.iperf3_image = os.environ.get(
            "IPERF3_IMAGE",
            "networkstatic/iperf3@sha256:c1e4a239a83d1d60975bce1c9b7661af5517e362bf335f66a2c5b6adaeb4f19f",
        )
        self.runtime_subscriber_upsert_limit = int(os.environ.get("RUNTIME_SUBSCRIBER_UPSERT_LIMIT", "10"))
        self.nef_qos_lambda_name = os.environ.get("NEF_QOS_LAMBDA_NAME", "")
        self.nef_traffic_influence_lambda_name = os.environ.get("NEF_TRAFFIC_INFLUENCE_LAMBDA_NAME", "")
        self.nef_pfd_lambda_name = os.environ.get("NEF_PFD_LAMBDA_NAME", "")
        self.hpa_update_lambda_name = os.environ.get("HPA_UPDATE_LAMBDA_NAME", "")
        self.status_include_eks = os.environ.get("STATUS_INCLUDE_EKS", "false").lower() in {"1", "true", "yes"}
        self.baseline_traffic_enabled = os.environ.get("BASELINE_TRAFFIC_ENABLED", "true").lower() in {"1", "true", "yes"}
        self.baseline_reconcile_interval_seconds = int(os.environ.get("BASELINE_RECONCILE_INTERVAL_SECONDS", "60"))


EVENT_CONFIG: dict[str, EventConfig] = {
    EventType.CONCERT.value: EventConfig(
        slice_sst=1,
        slice_sd="000001",
        slice_type=SliceType.EMBB,
        ue_count=1,
        # IMSI 001 is reserved for the permanent citizen eMBB baseline UE.
        # Reusing it here caused the event UE to replace the baseline SM context.
        ue_ids=["imsi-208930000000004"],
        dnn="citizen",
        risk=RiskLevel.HIGH,
        imsi_suffix="9001",
        five_qi=9,
        ue_ambr=Bandwidth(uplink="1 Gbps", downlink="1 Gbps"),
        session_ambr=Bandwidth(uplink="500 Mbps", downlink="1 Gbps"),
        mbr=Bandwidth(uplink="1 Gbps", downlink="1 Gbps"),
        gbr=Bandwidth(),
        traffic_profile="iperf3 UDP 80M, 1400-byte packets",
    ),
    EventType.TYPHOON.value: EventConfig(
        slice_sst=2,
        slice_sd="000003",
        slice_type=SliceType.URLLC,
        ue_count=3,
        ue_ids=[f"imsi-20893000000{i:04d}" for i in range(10, 13)],
        # Keep the URLLC slice distinction in S-NSSAI/5QI while using the
        # routable N6 data network.  The former `emergency` DNN established a
        # PDU session uses the isolated emergency DNN and its dedicated UPF.
        dnn="emergency",
        risk=RiskLevel.CRITICAL,
        imsi_suffix="9002",
        five_qi=2,
        ue_ambr=Bandwidth(uplink="20 Mbps", downlink="20 Mbps"),
        session_ambr=Bandwidth(uplink="5 Mbps", downlink="5 Mbps"),
        mbr=Bandwidth(uplink="5 Mbps", downlink="5 Mbps"),
        gbr=Bandwidth(uplink="5 Mbps", downlink="5 Mbps"),
        traffic_profile="iperf3 UDP 12M, 200-byte packets",
    ),
    EventType.ACCIDENT.value: EventConfig(
        slice_sst=4,
        slice_sd="000005",
        slice_type=SliceType.V2X,
        ue_count=1,
        ue_ids=["imsi-208930000000003"],
        dnn="v2x",
        risk=RiskLevel.HIGH,
        imsi_suffix="9003",
        five_qi=79,
        ue_ambr=Bandwidth(uplink="200 Mbps", downlink="200 Mbps"),
        session_ambr=Bandwidth(uplink="200 Mbps", downlink="200 Mbps"),
        mbr=Bandwidth(uplink="200 Mbps", downlink="200 Mbps"),
        gbr=Bandwidth(),
        traffic_profile="iperf3 UDP 36M, 1400-byte unicast V2X burst",
    ),
    EventType.MEDICAL.value: EventConfig(
        slice_sst=2,
        slice_sd="000002",
        slice_type=SliceType.URLLC,
        ue_count=1,
        ue_ids=["imsi-208930000000002"],
        dnn="emergency",
        risk=RiskLevel.CRITICAL,
        imsi_suffix="9004",
        five_qi=1,
        ue_ambr=Bandwidth(uplink="50 Mbps", downlink="50 Mbps"),
        session_ambr=Bandwidth(uplink="50 Mbps", downlink="50 Mbps"),
        mbr=Bandwidth(uplink="10 Mbps", downlink="10 Mbps"),
        gbr=Bandwidth(uplink="10 Mbps", downlink="10 Mbps"),
        traffic_profile="iperf3 UDP 13M, 200-byte packets with RTT",
    ),
    EventType.IOT_SURGE.value: EventConfig(
        slice_sst=3,
        slice_sd="000004",
        slice_type=SliceType.MMTC,
        ue_count=50,
        ue_ids=[f"imsi-20893000000{i:04d}" for i in range(100, 150)],
        # mMTC isolation is provided by S-NSSAI/5QI.  Use the verified N6 DNN
        # through the dedicated mMTC UPF and iot DNN.
        dnn="iot",
        risk=RiskLevel.HIGH,
        imsi_suffix="9005",
        five_qi=79,
        ue_ambr=Bandwidth(uplink="10 Mbps", downlink="10 Mbps"),
        session_ambr=Bandwidth(uplink="1 Mbps", downlink="1 Mbps"),
        mbr=Bandwidth(uplink="1 Mbps", downlink="1 Mbps"),
        gbr=Bandwidth(),
        traffic_profile="iperf3 UDP 417K x 12 parallel streams",
    ),
}

EVENT_BY_IMSI_SUFFIX = {cfg.imsi_suffix: name for name, cfg in EVENT_CONFIG.items()}
CITYVERSE_UE_IDS = {ue_id for cfg in EVENT_CONFIG.values() for ue_id in cfg.ue_ids}

UE_CONFIG_MAPS = {
    EventType.CONCERT.value: "ueransim-ue-config-embb",
    EventType.MEDICAL.value: "ueransim-ue-config-urllc",
    EventType.TYPHOON.value: "ueransim-ue-config-typhoon",
    EventType.IOT_SURGE.value: "ueransim-ue-config-mmtc",
    EventType.ACCIDENT.value: "ueransim-ue-config-v2x",
}

IPERF3_ARGS = {
    EventType.CONCERT.value: ["-u", "-b", "80M", "-t", "120", "-l", "1400"],
    EventType.MEDICAL.value: ["-u", "-b", "13M", "-t", "120", "-l", "200"],
    EventType.TYPHOON.value: ["-u", "-b", "12M", "-t", "120", "-l", "200"],
    EventType.IOT_SURGE.value: ["-u", "-b", "417K", "-P", "12", "-t", "120", "-l", "64"],
    EventType.ACCIDENT.value: ["-u", "-b", "36M", "-t", "30", "-l", "1400"],
}
