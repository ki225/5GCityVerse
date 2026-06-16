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


class DataSource(str, Enum):
    PROMETHEUS = "prometheus"
    ESTIMATED = "estimated"
    SIMULATED = "simulated"


class DynamoKeys(str, Enum):
    WS_CONNECTION = "WS_CONNECTION"
    STATUS = "STATUS"


DEFAULT_CORS_HEADERS = {
    "content-type": "application/json",
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET,POST,OPTIONS",
    "access-control-allow-headers": "content-type",
}

FREE5GC_NAMESPACE_DEFAULT = "free5gc"
TTL_SECONDS = 7200

