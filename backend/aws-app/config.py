from __future__ import annotations

import os

from constants import EventType, RiskLevel, SliceType
from models import Bandwidth, EventConfig


class AppSettings:
    def __init__(self) -> None:
        self.dynamodb_table = os.environ["DYNAMODB_TABLE"]
        self.apigw_ws_endpoint = os.environ["APIGW_WS_ENDPOINT"]
        self.free5gc_webui_url = os.environ.get("FREE5GC_WEBUI_URL", "").rstrip("/")
        self.free5gc_webui_username = os.environ.get("FREE5GC_WEBUI_USERNAME", "admin")
        self.free5gc_webui_password = os.environ.get("FREE5GC_WEBUI_PASSWORD", "free5gc")
        self.free5gc_plmn_id = os.environ.get("FREE5GC_PLMN_ID", "20893")
        self.free5gc_imsi_prefix = os.environ.get("FREE5GC_IMSI_PREFIX", "20893000000")
        self.prometheus_url = os.environ.get("PROMETHEUS_URL", "").rstrip("/")
        self.eks_cluster_name = os.environ.get("EKS_CLUSTER_NAME", "")
        self.free5gc_namespace = os.environ.get("FREE5GC_NAMESPACE", "free5gc")
        self.ueransim_ue_deployment = os.environ.get("UERANSIM_UE_DEPLOYMENT", "ueransim-city-ue")
        self.runtime_subscriber_upsert_limit = int(os.environ.get("RUNTIME_SUBSCRIBER_UPSERT_LIMIT", "10"))


EVENT_CONFIG: dict[str, EventConfig] = {
    EventType.CONCERT.value: EventConfig(
        slice_sst=1,
        slice_sd="000001",
        slice_type=SliceType.EMBB,
        ue_count=1,
        ue_ids=["imsi-208930000000001"],
        dnn="internet",
        risk=RiskLevel.HIGH,
        score=88,
        imsi_suffix="9001",
        five_qi=9,
        ue_ambr=Bandwidth(uplink="1 Gbps", downlink="1 Gbps"),
        session_ambr=Bandwidth(uplink="500 Mbps", downlink="1 Gbps"),
        mbr=Bandwidth(uplink="1 Gbps", downlink="1 Gbps"),
        gbr=Bandwidth(),
        traffic_profile="iperf3 UDP 800M, 1400-byte packets",
    ),
    EventType.TYPHOON.value: EventConfig(
        slice_sst=2,
        slice_sd="000003",
        slice_type=SliceType.URLLC,
        ue_count=3,
        ue_ids=[f"imsi-20893000000{i:04d}" for i in range(10, 13)],
        dnn="emergency",
        risk=RiskLevel.CRITICAL,
        score=95,
        imsi_suffix="9002",
        five_qi=2,
        ue_ambr=Bandwidth(uplink="20 Mbps", downlink="20 Mbps"),
        session_ambr=Bandwidth(uplink="5 Mbps", downlink="5 Mbps"),
        mbr=Bandwidth(uplink="5 Mbps", downlink="5 Mbps"),
        gbr=Bandwidth(uplink="5 Mbps", downlink="5 Mbps"),
        traffic_profile="iperf3 UDP 5M, 200-byte packets",
    ),
    EventType.ACCIDENT.value: EventConfig(
        slice_sst=4,
        slice_sd="000005",
        slice_type=SliceType.V2X,
        ue_count=1,
        ue_ids=["imsi-208930000000003"],
        dnn="internet",
        risk=RiskLevel.HIGH,
        score=85,
        imsi_suffix="9003",
        five_qi=75,
        ue_ambr=Bandwidth(uplink="200 Mbps", downlink="200 Mbps"),
        session_ambr=Bandwidth(uplink="200 Mbps", downlink="200 Mbps"),
        mbr=Bandwidth(uplink="200 Mbps", downlink="200 Mbps"),
        gbr=Bandwidth(),
        traffic_profile="iperf3 UDP 150M, 30s V2X burst",
    ),
    EventType.MEDICAL.value: EventConfig(
        slice_sst=2,
        slice_sd="000002",
        slice_type=SliceType.URLLC,
        ue_count=1,
        ue_ids=["imsi-208930000000002"],
        dnn="internet",
        risk=RiskLevel.CRITICAL,
        score=92,
        imsi_suffix="9004",
        five_qi=1,
        ue_ambr=Bandwidth(uplink="50 Mbps", downlink="50 Mbps"),
        session_ambr=Bandwidth(uplink="50 Mbps", downlink="50 Mbps"),
        mbr=Bandwidth(uplink="10 Mbps", downlink="10 Mbps"),
        gbr=Bandwidth(uplink="10 Mbps", downlink="10 Mbps"),
        traffic_profile="iperf3 UDP 10M, 200-byte packets with RTT",
    ),
    EventType.IOT_SURGE.value: EventConfig(
        slice_sst=3,
        slice_sd="000004",
        slice_type=SliceType.MMTC,
        ue_count=50,
        ue_ids=[f"imsi-20893000000{i:04d}" for i in range(100, 150)],
        dnn="iot",
        risk=RiskLevel.HIGH,
        score=80,
        imsi_suffix="9005",
        five_qi=79,
        ue_ambr=Bandwidth(uplink="10 Mbps", downlink="10 Mbps"),
        session_ambr=Bandwidth(uplink="1 Mbps", downlink="1 Mbps"),
        mbr=Bandwidth(uplink="1 Mbps", downlink="1 Mbps"),
        gbr=Bandwidth(),
        traffic_profile="iperf3 UDP 200K x 50 parallel streams",
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
    EventType.CONCERT.value: ["-u", "-b", "800M", "-t", "120", "-l", "1400", "--json"],
    EventType.MEDICAL.value: ["-u", "-b", "10M", "-t", "120", "-l", "200", "--trip-times", "--json"],
    EventType.TYPHOON.value: ["-u", "-b", "5M", "-t", "120", "-l", "60", "--trip-times", "--json"],
    EventType.IOT_SURGE.value: ["-u", "-b", "200K", "-P", "50", "-t", "120", "-l", "64", "--json"],
    EventType.ACCIDENT.value: ["-u", "-b", "150M", "-t", "30", "-l", "1400", "--json"],
}
