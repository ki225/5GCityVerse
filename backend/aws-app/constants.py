from enum import Enum


class ApiRoute(str, Enum):
    WS_CONNECT = "$connect"
    WS_DISCONNECT = "$disconnect"
    WS_DEFAULT = "$default"


class EventType(str, Enum):
    CONCERT = "concert"
    TYPHOON = "typhoon"
    ACCIDENT = "accident"
    MEDICAL = "medical"
    IOT_SURGE = "iot_surge"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SliceType(str, Enum):
    EMBB = "eMBB"
    URLLC = "URLLC"
    MMTC = "mMTC"
    V2X = "V2X"


class SliceStrategy(str, Enum):
    NONE = "none"
    STATIC = "static"
    AI = "ai"


class DataSource(str, Enum):
    PROMETHEUS = "prometheus"
    EKS_PROMETHEUS = "eks+prometheus"
    EKS = "eks"
    FREE5GC_OAM = "free5gc-oam"
    EKS_UERANSIM_LOGS = "eks+ueransim-logs"
    FREE5GC = "free5gc"
    UNAVAILABLE = "unavailable"


class EvidenceLevel(str, Enum):
    MEASURED = "measured"
    ESTIMATED = "estimated"
    FALLBACK = "fallback"
    DEMO = "demo"


class DynamoKeys(str, Enum):
    WS_CONNECTION = "WS_CONNECTION"
    STATUS = "STATUS"
    NEF_HITS = "NEF_HITS"


class ApiErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    EVENT_BLOCKED = "EVENT_BLOCKED"
    EVENT_CANCELLED = "EVENT_CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class WsMessageType(str, Enum):
    EVENT_STARTED = "event_started"
    EVENT_BLOCKED = "event_blocked"
    EVENT_RESET = "event_reset"
    FREE5GC_STATUS = "free5gc_status"
    RUNTIME_PRIMING = "runtime_priming"
    RUNTIME_PRIMED = "runtime_primed"
    AGENT_DECISION = "agent_decision"
    METRICS_UPDATE = "metrics_update"
    SLICE_UPDATE = "slice_update"
    NETWORK_SNAPSHOT = "network_snapshot"


DEFAULT_CORS_HEADERS = {
    "content-type": "application/json",
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET,POST,OPTIONS",
    "access-control-allow-headers": "content-type",
}

FREE5GC_NAMESPACE_DEFAULT = "free5gc"
TTL_SECONDS = 7200
