import json
import os
import base64
import ssl
import traceback
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal

import boto3
from botocore.signers import RequestSigner


dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["DYNAMODB_TABLE"])
apigw = boto3.client("apigatewaymanagementapi", endpoint_url=os.environ["APIGW_WS_ENDPOINT"])
aws_session = boto3.session.Session()


EVENT_CONFIG = {
    "concert": {
        "slice_sst": 1,
        "slice_sd": "000001",
        "slice_type": "eMBB",
        "ue_count": 1,
        "ue_ids": ["imsi-208930000000001"],
        "dnn": "internet",
        "risk": "high",
        "score": 88,
        "imsi_suffix": "9001",
        "five_qi": 9,
        "ue_ambr": {"uplink": "1 Gbps", "downlink": "1 Gbps"},
        "session_ambr": {"uplink": "500 Mbps", "downlink": "1 Gbps"},
        "mbr": {"uplink": "1 Gbps", "downlink": "1 Gbps"},
        "gbr": {"uplink": "", "downlink": ""},
        "traffic_profile": "iperf3 UDP 800M, 1400-byte packets",
    },
    "typhoon": {
        "slice_sst": 2,
        "slice_sd": "000003",
        "slice_type": "URLLC",
        "ue_count": 3,
        "ue_ids": [f"imsi-20893000000{i:04d}" for i in range(10, 13)],
        "dnn": "emergency",
        "risk": "critical",
        "score": 95,
        "imsi_suffix": "9002",
        "five_qi": 2,
        "ue_ambr": {"uplink": "20 Mbps", "downlink": "20 Mbps"},
        "session_ambr": {"uplink": "5 Mbps", "downlink": "5 Mbps"},
        "mbr": {"uplink": "5 Mbps", "downlink": "5 Mbps"},
        "gbr": {"uplink": "5 Mbps", "downlink": "5 Mbps"},
        "traffic_profile": "iperf3 UDP 5M, 200-byte packets",
    },
    "accident": {
        "slice_sst": 4,
        "slice_sd": "000005",
        "slice_type": "V2X",
        "ue_count": 1,
        "ue_ids": ["imsi-208930000000003"],
        "dnn": "internet",
        "risk": "high",
        "score": 85,
        "imsi_suffix": "9003",
        "five_qi": 75,
        "ue_ambr": {"uplink": "200 Mbps", "downlink": "200 Mbps"},
        "session_ambr": {"uplink": "200 Mbps", "downlink": "200 Mbps"},
        "mbr": {"uplink": "200 Mbps", "downlink": "200 Mbps"},
        "gbr": {"uplink": "", "downlink": ""},
        "traffic_profile": "iperf3 UDP 150M, 30s V2X burst",
    },
    "medical": {
        "slice_sst": 2,
        "slice_sd": "000002",
        "slice_type": "URLLC",
        "ue_count": 1,
        "ue_ids": ["imsi-208930000000002"],
        "dnn": "internet",
        "risk": "critical",
        "score": 92,
        "imsi_suffix": "9004",
        "five_qi": 1,
        "ue_ambr": {"uplink": "50 Mbps", "downlink": "50 Mbps"},
        "session_ambr": {"uplink": "50 Mbps", "downlink": "50 Mbps"},
        "mbr": {"uplink": "10 Mbps", "downlink": "10 Mbps"},
        "gbr": {"uplink": "10 Mbps", "downlink": "10 Mbps"},
        "traffic_profile": "iperf3 UDP 10M, 200-byte packets with RTT",
    },
    "iot_surge": {
        "slice_sst": 3,
        "slice_sd": "000004",
        "slice_type": "mMTC",
        "ue_count": 50,
        "ue_ids": [f"imsi-20893000000{i:04d}" for i in range(100, 150)],
        "dnn": "iot",
        "risk": "high",
        "score": 80,
        "imsi_suffix": "9005",
        "five_qi": 79,
        "ue_ambr": {"uplink": "10 Mbps", "downlink": "10 Mbps"},
        "session_ambr": {"uplink": "1 Mbps", "downlink": "1 Mbps"},
        "mbr": {"uplink": "1 Mbps", "downlink": "1 Mbps"},
        "gbr": {"uplink": "", "downlink": ""},
        "traffic_profile": "iperf3 UDP 200K x 50 parallel streams",
    },
}

FREE5GC_WEBUI_URL = os.environ.get("FREE5GC_WEBUI_URL", "").rstrip("/")
FREE5GC_WEBUI_USERNAME = os.environ.get("FREE5GC_WEBUI_USERNAME", "admin")
FREE5GC_WEBUI_PASSWORD = os.environ.get("FREE5GC_WEBUI_PASSWORD", "free5gc")
FREE5GC_PLMN_ID = os.environ.get("FREE5GC_PLMN_ID", "20893")
FREE5GC_IMSI_PREFIX = os.environ.get("FREE5GC_IMSI_PREFIX", "20893000000")
FREE5GC_SCENARIO_UE_ID = os.environ.get("FREE5GC_SCENARIO_UE_ID", "imsi-208930000000001")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "").rstrip("/")
EKS_CLUSTER_NAME = os.environ.get("EKS_CLUSTER_NAME", "")
FREE5GC_NAMESPACE = os.environ.get("FREE5GC_NAMESPACE", "free5gc")
UERANSIM_UE_DEPLOYMENT = os.environ.get("UERANSIM_UE_DEPLOYMENT", "ueransim-city-ue")
RUNTIME_SUBSCRIBER_UPSERT_LIMIT = int(os.environ.get("RUNTIME_SUBSCRIBER_UPSERT_LIMIT", "10"))
EVENT_BY_IMSI_SUFFIX = {cfg["imsi_suffix"]: name for name, cfg in EVENT_CONFIG.items()}
CITYVERSE_UE_IDS = {ue_id for cfg in EVENT_CONFIG.values() for ue_id in cfg["ue_ids"]}

UE_CONFIG_MAPS = {
    "concert": "ueransim-ue-config-embb",
    "medical": "ueransim-ue-config-urllc",
    "typhoon": "ueransim-ue-config-typhoon",
    "iot_surge": "ueransim-ue-config-mmtc",
    "accident": "ueransim-ue-config-v2x",
}

IPERF3_ARGS = {
    "concert": ["-u", "-b", "800M", "-t", "120", "-l", "1400", "--json"],
    "medical": ["-u", "-b", "10M", "-t", "120", "-l", "200", "--trip-times", "--json"],
    "typhoon": ["-u", "-b", "5M", "-t", "120", "-l", "60", "--trip-times", "--json"],
    "iot_surge": ["-u", "-b", "200K", "-P", "50", "-t", "120", "-l", "64", "--json"],
    "accident": ["-u", "-b", "150M", "-t", "30", "-l", "1400", "--json"],
}


def lambda_handler(event, _context):
    route_key = event.get("requestContext", {}).get("routeKey", "")
    if event.get("requestContext", {}).get("connectionId") and route_key in ("$connect", "$disconnect", "$default"):
        return handle_ws(event, route_key)

    method = event.get("requestContext", {}).get("http", {}).get("method", event.get("httpMethod", "GET"))
    path = event.get("rawPath", event.get("path", "/"))

    if method == "OPTIONS":
        return response(204, {})
    if method == "POST" and path.endswith("/events/trigger"):
        return handle_trigger(event)
    if method == "POST" and path.endswith("/events/reset"):
        free5gc_reset = reset_free5gc_subscribers()
        broadcast({"type": "slice_update", "payload": default_slices()})
        broadcast({"type": "metrics_update", "payload": default_metrics()})
        status_payload = get_free5gc_status_payload()
        broadcast({"type": "free5gc_status", "payload": status_payload})
        return response(200, {"status": "reset", "free5gc": free5gc_reset})
    if method == "GET" and "/events/status/" in path:
        execution_id = path.rsplit("/", 1)[-1]
        item = get_event(execution_id)
        if not item:
            return response(404, {"error": "Execution not found"})
        return response(200, item)
    if method == "GET" and path.endswith("/free5gc/status"):
        return response(200, get_free5gc_status_payload())
    if method == "GET" and path.endswith("/network/slices"):
        return response(200, get_current_slices())
    if method == "GET" and path.endswith("/metrics/current"):
        return response(200, get_current_metrics())
    return response(404, {"error": "Not found", "path": path})


def handle_ws(event, route_key):
    conn_id = event.get("requestContext", {}).get("connectionId", "")
    if route_key == "$connect":
        table.put_item(
            Item={
                "pk": "WS_CONNECTION",
                "sk": conn_id,
                "ttl": int(time.time()) + 7200,
                "connected_at": now(),
            }
        )
        return {"statusCode": 200}
    if route_key == "$disconnect":
        table.delete_item(Key={"pk": "WS_CONNECTION", "sk": conn_id})
        return {"statusCode": 200}

    try:
        body = json.loads(event.get("body") or "{}")
        if body.get("action") == "ping":
            post_to_connection(conn_id, {"type": "pong", "payload": {}})
    except Exception:
        pass
    return {"statusCode": 200}


def handle_trigger(event):
    body = json.loads(event.get("body") or "{}")
    event_type = body.get("event_type", "")
    if event_type not in EVENT_CONFIG:
        return response(400, {"error": f"Unknown event_type: {event_type}"})

    execution_id = str(uuid.uuid4())
    cfg = EVENT_CONFIG[event_type]
    free5gc_result = upsert_free5gc_subscribers(event_type, cfg, execution_id)
    environment_result = trigger_scenario_environment(event_type, cfg)
    decision = build_decision(event_type, cfg, free5gc_result, environment_result)
    item = {
        "pk": f"EVENT#{execution_id}",
        "sk": "STATUS",
        "executionId": execution_id,
        "eventType": event_type,
        "status": "AGENT_COMPLETE",
        "config": cfg,
        "agentDecision": decision,
        "started_at": now(),
        "mcp": {"terraform_mcp_used_for_iac": True, "free5gc_runtime_mode": "eks-webui-api"},
        "free5gc": free5gc_result,
        "environment": environment_result,
    }
    table.put_item(Item=to_dynamodb(item))

    broadcast({"type": "event_started", "payload": {"executionId": execution_id, "eventType": event_type}})
    broadcast({"type": "agent_decision", "payload": decision})
    broadcast({"type": "metrics_update", "payload": event_metrics(event_type)})
    broadcast({"type": "slice_update", "payload": event_slices(event_type)})
    broadcast({"type": "free5gc_status", "payload": get_free5gc_status_payload()})
    broadcast(
        {
            "type": "pod_event",
            "payload": {
                "event": "ADDED",
                "pod": "upf-aws-demo-2",
                "phase": "Running",
                "component": "UPF",
                "namespace": "free5gc",
                "timestamp": now(),
            },
        }
    )
    return response(200, {"executionId": execution_id, "eventType": event_type, "environment": environment_result})


def get_free5gc_status_payload():
    started = time.time()
    real_metrics = get_real_metrics()
    if not FREE5GC_WEBUI_URL:
        metrics = real_metrics or default_metrics()
        return {
            "connected": False,
            "source": "free5GC WebUI API",
            "error": "FREE5GC_WEBUI_URL is not configured",
            "subscribers": [],
            "eventSubscribers": [],
            "metrics": metrics,
            "slices": get_current_slices(metrics),
            "checkedAt": now(),
        }

    try:
        subscribers = free5gc_list_subscribers()
        registered_ues = free5gc_registered_ues()
        profiles = free5gc_profiles()
        event_subscribers = [s for s in subscribers if is_cityverse_subscriber(s)]
        metrics = real_metrics or estimated_metrics_from_free5gc(started, event_subscribers, registered_ues)
        slices = get_current_slices(metrics, event_subscribers)
        return {
            "connected": True,
            "source": "Prometheus + free5GC WebUI API" if real_metrics else "free5GC WebUI API /api/subscriber",
            "subscriberCount": len(subscribers),
            "eventSubscriberCount": len(event_subscribers),
            "registeredUeCount": metrics.get("registeredUeCount", len(registered_ues)),
            "profileCount": len(profiles),
            "subscribers": subscribers,
            "eventSubscribers": event_subscribers,
            "registeredUes": registered_ues,
            "profiles": profiles,
            "metrics": metrics,
            "slices": slices,
            "checkedAt": now(),
        }
    except Exception as exc:
        metrics = real_metrics or default_metrics()
        return {
            "connected": False,
            "source": "free5GC WebUI API /api/subscriber",
            "error": str(exc),
            "subscribers": [],
            "eventSubscribers": [],
            "registeredUes": [],
            "profiles": [],
            "metrics": metrics,
            "slices": get_current_slices(metrics),
            "checkedAt": now(),
        }


def upsert_free5gc_subscribers(event_type, cfg, execution_id):
    if len(cfg["ue_ids"]) > RUNTIME_SUBSCRIBER_UPSERT_LIMIT:
        return {
            "status": "success",
            "operation": "preseeded",
            "httpStatus": 200,
            "ueCount": len(cfg["ue_ids"]),
            "successCount": len(cfg["ue_ids"]),
            "errorCount": 0,
            "ueIds": cfg["ue_ids"],
            "eventType": event_type,
            "executionId": execution_id,
            "reason": "Subscribers are pre-seeded during deploy/start to keep frontend-triggered activation under API Gateway timeout.",
        }

    results = []
    for ue_id in cfg["ue_ids"]:
        results.append(upsert_free5gc_subscriber(event_type, cfg, execution_id, ue_id))

    success = [item for item in results if item.get("status") == "success"]
    errors = [item for item in results if item.get("status") != "success"]
    return {
        "status": "success" if not errors else "partial" if success else "error",
        "httpStatus": 200 if not errors else errors[0].get("httpStatus", 500),
        "ueCount": len(results),
        "successCount": len(success),
        "errorCount": len(errors),
        "ueIds": cfg["ue_ids"],
        "eventType": event_type,
        "executionId": execution_id,
        "results": results,
    }


def upsert_free5gc_subscriber(event_type, cfg, execution_id, ue_id):
    if not FREE5GC_WEBUI_URL:
        return {"status": "skipped", "reason": "FREE5GC_WEBUI_URL is not configured"}

    msisdn = f"msisdn-{ue_id[-10:]}"
    payload = build_free5gc_subscriber(event_type, cfg, ue_id, msisdn)
    path = f"/api/subscriber/{ue_id}/{FREE5GC_PLMN_ID}"

    try:
        token = free5gc_login()
        status, body = free5gc_request("POST", path, token, payload)
        operation = "created"
        if status >= 400:
            status, body = free5gc_request("PUT", path, token, payload)
            operation = "updated"
        profile_result = upsert_free5gc_profile(token, event_type, cfg)
        result = "success" if status < 300 else "error"
        return {
            "status": result,
            "operation": operation,
            "httpStatus": status,
            "ueId": ue_id,
            "plmnID": FREE5GC_PLMN_ID,
            "eventType": event_type,
            "executionId": execution_id,
            "profile": profile_result,
            "response": body,
        }
    except Exception as exc:
        return {
            "status": "error",
            "ueId": ue_id,
            "plmnID": FREE5GC_PLMN_ID,
            "eventType": event_type,
            "executionId": execution_id,
            "error": str(exc),
        }


def free5gc_login():
    body = {"username": FREE5GC_WEBUI_USERNAME, "password": FREE5GC_WEBUI_PASSWORD}
    status, data = http_json("POST", f"{FREE5GC_WEBUI_URL}/api/login", body)
    if status >= 300 or not data.get("access_token"):
        raise RuntimeError(f"free5GC login failed: HTTP {status} {data}")
    return data["access_token"]


def free5gc_request(method, path, token, body=None):
    return http_json(method, f"{FREE5GC_WEBUI_URL}{path}", body, {"Token": token})


def free5gc_list_subscribers():
    token = free5gc_login()
    status, data = free5gc_request("GET", "/api/subscriber", token)
    if status >= 300:
        raise RuntimeError(f"free5GC subscriber list failed: HTTP {status} {data}")
    return data if isinstance(data, list) else []


def free5gc_registered_ues():
    token = free5gc_login()
    status, data = free5gc_request("GET", "/api/registered-ue-context", token)
    if status >= 300:
        return []
    return data if isinstance(data, list) else []


def free5gc_profiles():
    token = free5gc_login()
    status, data = free5gc_request("GET", "/api/profile", token)
    if status >= 300:
        return []
    return data if isinstance(data, list) else []


def query_prometheus(promql):
    if not PROMETHEUS_URL:
        return None
    query = urllib.parse.urlencode({"query": promql})
    url = f"{PROMETHEUS_URL}/api/v1/query?{query}"
    try:
        with urllib.request.urlopen(url, timeout=3) as res:
            data = json.loads(res.read().decode("utf-8") or "{}")
            results = data.get("data", {}).get("result", [])
            if results:
                return float(results[0]["value"][1])
    except Exception as exc:
        print(f"Prometheus query failed: {promql}: {exc}")
    return None


def first_prometheus_value(*queries):
    for query in queries:
        value = query_prometheus(query)
        if value is not None:
            return value
    return None


def get_real_metrics():
    uplink_bytes = first_prometheus_value(
        'rate(free5gc_upf_bytes_total{direction="uplink"}[10s])',
        'sum(rate(container_network_receive_bytes_total{namespace="free5gc",pod=~".*upf.*"}[30s]))',
    )
    downlink_bytes = first_prometheus_value(
        'rate(free5gc_upf_bytes_total{direction="downlink"}[10s])',
        'sum(rate(container_network_transmit_bytes_total{namespace="free5gc",pod=~".*upf.*"}[30s]))',
    )
    active_sessions = first_prometheus_value(
        "free5gc_smf_pdu_session_active",
        "sum(free5gc_smf_pdu_session_count)",
    )
    registered_ues = first_prometheus_value(
        "free5gc_amf_registered_ue_total",
    )
    gtp_packets = first_prometheus_value(
        "rate(free5gc_upf_packets_total[10s])",
        "sum(rate(gtp5g_packet_count[30s]))",
    )
    upf_cpu = first_prometheus_value(
        'sum(rate(container_cpu_usage_seconds_total{namespace="free5gc",pod=~".*upf.*"}[30s])) * 100',
    )
    amf_cpu = first_prometheus_value(
        'sum(rate(container_cpu_usage_seconds_total{namespace="free5gc",pod=~".*amf.*"}[30s])) * 100',
    )

    values = [uplink_bytes, downlink_bytes, active_sessions, registered_ues, gtp_packets, upf_cpu, amf_cpu]
    if all(value is None for value in values):
        return None

    uplink_bytes = uplink_bytes or 0.0
    downlink_bytes = downlink_bytes or 0.0
    return {
        "upfCpuPercent": round(upf_cpu or 0.0, 1),
        "upfPodCount": int(first_prometheus_value('count(kube_pod_status_phase{namespace="free5gc",pod=~".*upf.*",phase="Running"})') or 1),
        "amfPodCount": int(first_prometheus_value('count(kube_pod_status_phase{namespace="free5gc",pod=~".*amf.*",phase="Running"})') or 1),
        "amfCpuPercent": round(amf_cpu or 0.0, 1),
        "registeredUeCount": int(registered_ues or 0),
        "gtpPacketsPerSec": int(gtp_packets or 0),
        "pduSessionCount": int(active_sessions or 0),
        "latencyMs": round(first_prometheus_value("avg(free5gc_upf_packet_latency_ms)") or 0.0, 1),
        "throughputMbps": round((uplink_bytes + downlink_bytes) * 8 / 1_000_000, 2),
        "uplinkMbps": round(uplink_bytes * 8 / 1_000_000, 2),
        "downlinkMbps": round(downlink_bytes * 8 / 1_000_000, 2),
        "timestamp": int(time.time() * 1000),
        "dataSource": "prometheus",
    }


def get_current_metrics():
    return get_real_metrics() or default_metrics()


def estimated_metrics_from_free5gc(started, event_subscribers, registered_ues):
    metrics = default_metrics()
    metrics.update(
        {
            "pduSessionCount": len(registered_ues),
            "registeredUeCount": len(registered_ues),
            "throughputMbps": round(len(event_subscribers) * 10, 1),
            "gtpPacketsPerSec": len(event_subscribers) * 20,
            "latencyMs": round((time.time() - started) * 1000, 1),
            "timestamp": int(time.time() * 1000),
            "dataSource": "estimated",
        }
    )
    return metrics


def upsert_free5gc_profile(token, event_type, cfg):
    profile_name = f"5GCityVerse-{event_type}"
    payload = build_free5gc_profile(profile_name, event_type, cfg)
    status, body = free5gc_request("POST", "/api/profile", token, payload)
    operation = "created"
    if status == 409:
        status, body = free5gc_request("PUT", f"/api/profile/{profile_name}", token, payload)
        operation = "updated"
    return {"name": profile_name, "operation": operation, "httpStatus": status, "response": body}


def trigger_scenario_environment(event_type, cfg):
    if not EKS_CLUSTER_NAME:
        return {"status": "skipped", "reason": "EKS_CLUSTER_NAME is not configured"}

    actions = []
    try:
        k8s = EksKubernetesClient(EKS_CLUSTER_NAME)
        ensure_iperf3_server(k8s)
        ensure_ueransim_gnb(k8s)
        apply_ue_configmap(k8s, event_type, cfg)

        if event_type in ("typhoon", "iot_surge"):
            scale_primary_ueransim(k8s, 0)
            delete_deployment(k8s, "ueransim-typhoon" if event_type == "iot_surge" else "ueransim-iot")
            create_or_patch_deployment(k8s, scenario_deployment_manifest(event_type))
            actions.append(f"started {event_type} UERANSIM deployment")
        else:
            delete_deployment(k8s, "ueransim-typhoon")
            delete_deployment(k8s, "ueransim-iot")
            scale_primary_ueransim(k8s, 1)
            patch_primary_ueransim_config(k8s, UE_CONFIG_MAPS[event_type])
            actions.append(f"patched {UERANSIM_UE_DEPLOYMENT} to {UE_CONFIG_MAPS[event_type]}")

        recreate_iperf3_job(k8s, event_type)
        actions.append(f"launched iperf3-{event_type.replace('_', '-')}")
        return {"status": "success", "actions": actions, "httpStatus": 200}
    except Exception as exc:
        error = traceback.format_exc()
        print(error)
        return {"status": "error", "error": str(exc), "trace": error[-2000:], "actions": actions, "httpStatus": 500}


class EksKubernetesClient:
    def __init__(self, cluster_name):
        self.cluster_name = cluster_name
        eks = boto3.client("eks")
        cluster = eks.describe_cluster(name=cluster_name)["cluster"]
        self.endpoint = cluster["endpoint"].rstrip("/")
        ca_path = f"/tmp/{cluster_name}-ca.crt"
        ca_data = base64.b64decode(cluster["certificateAuthority"]["data"])
        with open(ca_path, "wb") as ca_file:
            ca_file.write(ca_data)
        self.ssl_context = ssl.create_default_context(cafile=ca_path)
        self.token = eks_bearer_token(cluster_name)

    def request(self, method, path, body=None, content_type="application/json", ignore_404=False):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": content_type,
        }
        req = urllib.request.Request(f"{self.endpoint}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10, context=self.ssl_context) as res:
                raw = res.read().decode("utf-8")
                return res.status, json.loads(raw or "{}")
        except urllib.error.HTTPError as exc:
            if ignore_404 and exc.code == 404:
                return 404, {}
            raw = exc.read().decode("utf-8")
            try:
                parsed = json.loads(raw or "{}")
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            raise RuntimeError(f"Kubernetes API {method} {path} failed: HTTP {exc.code} {parsed}") from exc

    def create_or_patch(self, create_path, patch_path, manifest):
        try:
            return self.request("POST", create_path, manifest)
        except RuntimeError as exc:
            if "HTTP 409" not in str(exc):
                raise
            return self.request(
                "PATCH",
                patch_path,
                manifest,
                content_type="application/merge-patch+json",
            )

    def patch(self, path, body, ignore_404=False):
        return self.request(
            "PATCH",
            path,
            body,
            content_type="application/merge-patch+json",
            ignore_404=ignore_404,
        )

    def delete(self, path):
        return self.request("DELETE", path, {"propagationPolicy": "Foreground"}, ignore_404=True)


def eks_bearer_token(cluster_name):
    region = os.environ.get("AWS_REGION") or aws_session.region_name or "ap-northeast-1"
    credentials = aws_session.get_credentials()
    sts_client = aws_session.client("sts", region_name=region)
    signer = RequestSigner(
        sts_client.meta.service_model.service_id,
        region,
        "sts",
        "v4",
        credentials,
        aws_session.events,
    )
    params = {
        "method": "GET",
        "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "body": {},
        "headers": {"x-k8s-aws-id": cluster_name},
        "context": {},
    }
    signed_url = signer.generate_presigned_url(
        params,
        region_name=region,
        expires_in=60,
        operation_name="GetCallerIdentity",
    )
    token = base64.urlsafe_b64encode(signed_url.encode("utf-8")).decode("utf-8").rstrip("=")
    return f"k8s-aws-v1.{token}"


def ensure_iperf3_server(k8s):
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "iperf3-server", "namespace": FREE5GC_NAMESPACE},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "iperf3-server"}},
            "template": {
                "metadata": {"labels": {"app": "iperf3-server"}},
                "spec": {
                    "containers": [
                        {
                            "name": "iperf3",
                            "image": "networkstatic/iperf3:latest",
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["iperf3", "-s", "-p", "5201"],
                            "ports": [
                                {"name": "iperf3-tcp", "containerPort": 5201, "protocol": "TCP"},
                                {"name": "iperf3-udp", "containerPort": 5201, "protocol": "UDP"},
                            ],
                        }
                    ]
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "iperf3-server", "namespace": FREE5GC_NAMESPACE},
        "spec": {
            "selector": {"app": "iperf3-server"},
            "clusterIP": "None",
            "ports": [
                {"name": "iperf3-tcp", "port": 5201, "targetPort": 5201, "protocol": "TCP"},
                {"name": "iperf3-udp", "port": 5201, "targetPort": 5201, "protocol": "UDP"},
            ],
        },
    }
    k8s.create_or_patch(
        f"/apis/apps/v1/namespaces/{FREE5GC_NAMESPACE}/deployments",
        f"/apis/apps/v1/namespaces/{FREE5GC_NAMESPACE}/deployments/iperf3-server",
        deployment,
    )
    k8s.create_or_patch(
        f"/api/v1/namespaces/{FREE5GC_NAMESPACE}/services",
        f"/api/v1/namespaces/{FREE5GC_NAMESPACE}/services/iperf3-server",
        service,
    )


def ensure_ueransim_gnb(k8s):
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "ueransim-gnb-config", "namespace": FREE5GC_NAMESPACE},
        "data": {
            "gnb-template.yaml": """mcc: '208'
mnc: '93'
nci: '0x000000010'
idLength: 32
tac: 1
linkIp: POD_IP
ngapIp: POD_IP
gtpIp: POD_IP
amfConfigs:
  - address: free5gc-free5gc-amf-amf-n2
    port: 38412
slices:
  - sst: 0x01
    sd: 0x000001
  - sst: 0x02
    sd: 0x000002
  - sst: 0x02
    sd: 0x000003
  - sst: 0x03
    sd: 0x000004
  - sst: 0x04
    sd: 0x000005
ignoreStreamIds: true
"""
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "ueransim-gnb",
            "namespace": FREE5GC_NAMESPACE,
            "labels": {
                "app": "ueransim-gnb",
                "app.kubernetes.io/component": "gnb",
                "app.kubernetes.io/part-of": "5gcityverse",
            },
        },
        "spec": {
            "clusterIP": "None",
            "selector": {"app": "ueransim-gnb"},
        },
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "ueransim-gnb",
            "namespace": FREE5GC_NAMESPACE,
            "labels": {
                "app": "ueransim-gnb",
                "app.kubernetes.io/component": "gnb",
                "app.kubernetes.io/part-of": "5gcityverse",
            },
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "ueransim-gnb"}},
            "template": {
                "metadata": {
                    "labels": {
                        "app": "ueransim-gnb",
                        "app.kubernetes.io/component": "gnb",
                        "app.kubernetes.io/part-of": "5gcityverse",
                    }
                },
                "spec": {
                    "containers": [
                        {
                            "name": "ueransim-gnb",
                            "image": "free5gc/ueransim:v4.0.1",
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/bin/sh", "-c"],
                            "args": [
                                "sed \"s/POD_IP/${POD_IP}/g\" /etc/ueransim/gnb-template.yaml > /tmp/gnb.yaml && ./nr-gnb -c /tmp/gnb.yaml"
                            ],
                            "env": [
                                {
                                    "name": "POD_IP",
                                    "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
                                }
                            ],
                            "securityContext": {"capabilities": {"add": ["NET_ADMIN"]}},
                            "volumeMounts": [{"name": "gnb-config", "mountPath": "/etc/ueransim"}],
                        }
                    ],
                    "volumes": [{"name": "gnb-config", "configMap": {"name": "ueransim-gnb-config"}}],
                },
            },
        },
    }

    k8s.create_or_patch(
        f"/api/v1/namespaces/{FREE5GC_NAMESPACE}/configmaps",
        f"/api/v1/namespaces/{FREE5GC_NAMESPACE}/configmaps/ueransim-gnb-config",
        config_map,
    )
    k8s.create_or_patch(
        f"/api/v1/namespaces/{FREE5GC_NAMESPACE}/services",
        f"/api/v1/namespaces/{FREE5GC_NAMESPACE}/services/ueransim-gnb",
        service,
    )
    create_or_patch_deployment(k8s, deployment)


def apply_ue_configmap(k8s, event_type, cfg):
    name = UE_CONFIG_MAPS[event_type]
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": FREE5GC_NAMESPACE},
        "data": {"ue-config.yaml": ue_config_yaml(event_type, cfg)},
    }
    k8s.create_or_patch(
        f"/api/v1/namespaces/{FREE5GC_NAMESPACE}/configmaps",
        f"/api/v1/namespaces/{FREE5GC_NAMESPACE}/configmaps/{name}",
        manifest,
    )


def ue_config_yaml(event_type, cfg):
    if event_type == "concert":
        supi = "imsi-208930000000001"
        imei = "356938035643803"
        imeisv = "4370816125816151"
    elif event_type == "medical":
        supi = "imsi-208930000000002"
        imei = "356938035643804"
        imeisv = "4370816125816152"
    elif event_type == "accident":
        supi = "imsi-208930000000003"
        imei = "356938035643805"
        imeisv = "4370816125816153"
    elif event_type == "typhoon":
        supi = "imsi-208930000000010"
        imei = "356938035643810"
        imeisv = "4370816125816160"
    else:
        supi = "imsi-208930000000100"
        imei = "356938035643900"
        imeisv = "4370816125816200"

    return f"""supi: "{supi}"
mcc: "208"
mnc: "93"
key: "8baf473f2f8fd09487cccbd7097c6862"
op: "8e27b6af0e692e750f32667a3b14605d"
opType: "OPC"
amf: "8000"
imei: "{imei}"
imeiSv: "{imeisv}"
gnbSearchList:
  - ueransim-gnb
uacAic:
  mps: false
  mcs: false
uacAcc:
  normalClass: 0
  class11: false
  class12: false
  class13: false
  class14: false
  class15: false
sessions:
  - type: "IPv4"
    apn: "{cfg['dnn']}"
    slice:
      sst: 0x{cfg['slice_sst']:02x}
      sd: 0x{cfg['slice_sd']}
configured-nssai:
  - sst: 0x{cfg['slice_sst']:02x}
    sd: 0x{cfg['slice_sd']}
default-nssai:
  - sst: {cfg['slice_sst']}
    sd: {cfg['slice_sd']}
integrity:
  IA1: true
  IA2: true
  IA3: true
ciphering:
  EA1: true
  EA2: true
  EA3: true
integrityMaxRate:
  uplink: "full"
  downlink: "full"
"""


def scale_primary_ueransim(k8s, replicas):
    k8s.patch(
        f"/apis/apps/v1/namespaces/{FREE5GC_NAMESPACE}/deployments/{UERANSIM_UE_DEPLOYMENT}",
        {"spec": {"replicas": replicas}},
        ignore_404=True,
    )


def patch_primary_ueransim_config(k8s, config_map_name):
    k8s.patch(
        f"/apis/apps/v1/namespaces/{FREE5GC_NAMESPACE}/deployments/{UERANSIM_UE_DEPLOYMENT}",
        {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "5gcityverse.io/scenario-switched-at": now(),
                            "5gcityverse.io/ue-config": config_map_name,
                        }
                    },
                    "spec": {
                        "volumes": [
                            {
                                "name": "ue-volume",
                                "configMap": {
                                    "name": config_map_name,
                                    "items": [{"key": "ue-config.yaml", "path": "ue-config.yaml"}],
                                },
                            }
                        ]
                    },
                }
            }
        },
    )


def create_or_patch_deployment(k8s, manifest):
    name = manifest["metadata"]["name"]
    k8s.create_or_patch(
        f"/apis/apps/v1/namespaces/{FREE5GC_NAMESPACE}/deployments",
        f"/apis/apps/v1/namespaces/{FREE5GC_NAMESPACE}/deployments/{name}",
        manifest,
    )


def delete_deployment(k8s, name):
    k8s.delete(f"/apis/apps/v1/namespaces/{FREE5GC_NAMESPACE}/deployments/{name}")


def scenario_deployment_manifest(event_type):
    if event_type == "typhoon":
        name = "ueransim-typhoon"
        config_map = "ueransim-ue-config-typhoon"
        count = "3"
        resources = {}
    else:
        name = "ueransim-iot"
        config_map = "ueransim-ue-config-mmtc"
        count = "50"
        resources = {
            "requests": {"cpu": "500m", "memory": "512Mi"},
            "limits": {"cpu": "1", "memory": "1Gi"},
        }

    container = {
        "name": "ueransim-ue",
        "image": "free5gc/ueransim:v4.0.1",
        "imagePullPolicy": "IfNotPresent",
        "command": ["./nr-ue"],
        "args": ["-c", "/etc/ueransim/ue-config.yaml", "-n", count],
        "securityContext": {"capabilities": {"add": ["NET_ADMIN"]}},
        "volumeMounts": [{"name": "ue-config", "mountPath": "/etc/ueransim"}],
    }
    if resources:
        container["resources"] = resources

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": FREE5GC_NAMESPACE,
            "labels": {
                "app": name,
                "app.kubernetes.io/component": "ue",
                "app.kubernetes.io/part-of": "5gcityverse",
            },
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {
                    "annotations": {
                        "5gcityverse.io/scenario-switched-at": now(),
                        "5gcityverse.io/scenario": event_type,
                    },
                    "labels": {
                        "app": name,
                        "app.kubernetes.io/component": "ue",
                        "app.kubernetes.io/part-of": "5gcityverse",
                    }
                },
                "spec": {
                    "containers": [container],
                    "volumes": [
                        {
                            "name": "ue-config",
                            "configMap": {
                                "name": config_map,
                                "items": [{"key": "ue-config.yaml", "path": "ue-config.yaml"}],
                            },
                        }
                    ],
                },
            },
        },
    }


def recreate_iperf3_job(k8s, event_type):
    job_name = f"iperf3-{event_type.replace('_', '-')}-{uuid.uuid4().hex[:6]}"
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": FREE5GC_NAMESPACE,
            "labels": {
                "app.kubernetes.io/component": "iperf3",
                "app.kubernetes.io/part-of": "5gcityverse",
                "5gcityverse.io/scenario": event_type,
            },
        },
        "spec": {
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/component": "iperf3",
                        "app.kubernetes.io/part-of": "5gcityverse",
                    }
                },
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "iperf3-client",
                            "image": "networkstatic/iperf3:latest",
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["iperf3", "-c", "iperf3-server.free5gc.svc.cluster.local"] + IPERF3_ARGS[event_type],
                        }
                    ],
                },
            },
        },
    }
    k8s.request("POST", f"/apis/batch/v1/namespaces/{FREE5GC_NAMESPACE}/jobs", job)


def reset_free5gc_subscribers():
    if not FREE5GC_WEBUI_URL:
        return {"status": "skipped", "reason": "FREE5GC_WEBUI_URL is not configured"}

    try:
        token = free5gc_login()
        subscribers = free5gc_list_subscribers()
        deleted = []
        errors = []
        for subscriber in subscribers:
            ue_id = subscriber.get("ueId", "")
            plmn_id = subscriber.get("plmnID", FREE5GC_PLMN_ID)
            if not is_cityverse_ue_id(ue_id):
                continue
            status, data = free5gc_request("DELETE", f"/api/subscriber/{ue_id}/{plmn_id}", token)
            if status < 300:
                deleted.append(ue_id)
            else:
                errors.append({"ueId": ue_id, "httpStatus": status, "response": data})
        for event_type in EVENT_CONFIG:
            profile_name = f"5GCityVerse-{event_type}"
            status, data = free5gc_request("DELETE", f"/api/profile/{profile_name}", token)
            if status not in (200, 204, 404):
                errors.append({"profile": profile_name, "httpStatus": status, "response": data})
        return {"status": "success" if not errors else "partial", "deleted": deleted, "errors": errors}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def http_json(method, url, body=None, headers=None):
    data = None
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            raw = res.read().decode("utf-8")
            return res.status, json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return exc.code, parsed


def event_type_from_ue_id(ue_id):
    for event_type, cfg in EVENT_CONFIG.items():
        if ue_id in cfg["ue_ids"]:
            return event_type
    for suffix, event_type in EVENT_BY_IMSI_SUFFIX.items():
        if ue_id.endswith(suffix):
            return event_type
    return None


def is_cityverse_ue_id(ue_id):
    return ue_id in CITYVERSE_UE_IDS or bool(event_type_from_ue_id(ue_id))


def is_cityverse_subscriber(subscriber):
    ue_id = subscriber.get("ueId", "")
    return is_cityverse_ue_id(ue_id)


def event_type_from_subscriber(subscriber):
    am_policy = subscriber.get("AmPolicyData") or {}
    categories = am_policy.get("subscCats") or []
    for item in categories:
        if item in EVENT_CONFIG:
            return item
    return event_type_from_ue_id(subscriber.get("ueId", ""))


def slices_from_free5gc_subscribers(event_subscribers):
    slices = default_slices()
    event_counts = {}
    for subscriber in event_subscribers:
        event_type = event_type_from_subscriber(subscriber)
        if event_type:
            event_counts[event_type] = event_counts.get(event_type, 0) + 1

    for item in slices:
        item["sessions"] = 0
        item["load"] = 5
        item["trend"] = "stable"
        for event_type, count in event_counts.items():
            cfg = EVENT_CONFIG[event_type]
            if cfg["slice_sst"] == item["sst"]:
                item["sessions"] += count
                item["load"] = min(20 + count * 20, 100)
                item["trend"] = "up"
    return slices


def get_current_slices(metrics=None, event_subscribers=None):
    real_slices = get_real_slice_metrics()
    if real_slices:
        return real_slices
    if event_subscribers is not None:
        return slices_from_free5gc_subscribers(event_subscribers)
    return default_slices()


def get_real_slice_metrics():
    if not PROMETHEUS_URL:
        return None

    raw = {
        1: query_prometheus('rate(free5gc_upf_bytes_total{sst="1"}[10s])'),
        2: query_prometheus('rate(free5gc_upf_bytes_total{sst="2"}[10s])'),
        3: query_prometheus('rate(free5gc_upf_bytes_total{sst="3"}[10s])'),
        4: query_prometheus('rate(free5gc_upf_bytes_total{sst="4"}[10s])'),
    }
    if all(value is None for value in raw.values()):
        return None

    labels = {
        1: ("eMBB", "000001"),
        2: ("URLLC", "000002"),
        3: ("mMTC", "000004"),
        4: ("V2X", "000005"),
    }
    total = sum(value or 0.0 for value in raw.values()) or 1.0
    slices = []
    for sst, value in raw.items():
        value = value or 0.0
        slice_type, sd = labels[sst]
        load = round(value / total * 100)
        slices.append(
            {
                "sst": sst,
                "type": slice_type,
                "sd": sd,
                "load": min(max(load, 0), 100),
                "sessions": int(query_prometheus(f'free5gc_smf_pdu_session_active{{sst="{sst}"}}') or 0),
                "trend": "up" if value > 0 else "stable",
                "throughputMbps": round(value * 8 / 1_000_000, 2),
                "dataSource": "prometheus",
            }
        )
    return slices


def build_free5gc_subscriber(event_type, cfg, ue_id, msisdn):
    sst = cfg["slice_sst"]
    sd = cfg["slice_sd"]
    snssai_key = f"{sst:02d}{sd}"
    five_qi = cfg["five_qi"]
    qos_ref = 0
    dnn = cfg["dnn"]
    flow_filter = "permit out ip from any to any"
    ue_ambr = cfg["ue_ambr"]
    session_ambr = cfg["session_ambr"]
    gbr = cfg["gbr"]
    mbr = cfg["mbr"]
    priority = 1 if cfg["slice_type"] == "URLLC" else 15 if cfg["slice_type"] == "mMTC" else 8
    preempt_cap = "MAY_PREEMPT" if cfg["slice_type"] == "URLLC" else "NOT_PREEMPT"
    preempt_vuln = "PREEMPTABLE" if cfg["slice_type"] == "mMTC" else "NOT_PREEMPTABLE"
    dnn_config = {
        "sscModes": {
            "defaultSscMode": "SSC_MODE_1",
            "allowedSscModes": ["SSC_MODE_2", "SSC_MODE_3"],
        },
        "pduSessionTypes": {
            "defaultSessionType": "IPV4",
            "allowedSessionTypes": ["IPV4"],
        },
        "sessionAmbr": {"uplink": session_ambr["uplink"], "downlink": session_ambr["downlink"]},
        "5gQosProfile": {
            "5qi": five_qi,
            "arp": {
                "priorityLevel": priority,
                "preemptCap": preempt_cap,
                "preemptVuln": preempt_vuln,
            },
            "priorityLevel": priority,
        },
        "staticIpAddress": [],
    }
    if gbr["uplink"] and gbr["downlink"]:
        dnn_config["gbrQosFlowInfo"] = {
            "maxFbrUplink": mbr["uplink"],
            "maxFbrDownlink": mbr["downlink"],
            "guaranteedFbrUplink": gbr["uplink"],
            "guaranteedFbrDownlink": gbr["downlink"],
        }

    return {
        "plmnID": FREE5GC_PLMN_ID,
        "ueId": ue_id,
        "AuthenticationSubscription": {
            "authenticationManagementField": "8000",
            "authenticationMethod": "5G_AKA",
            "milenage": {"op": {"opValue": "8e27b6af0e692e750f32667a3b14605d"}},
            "permanentKey": {
                "encryptionAlgorithm": 0,
                "encryptionKey": 0,
                "permanentKeyValue": "8baf473f2f8fd09487cccbd7097c6862",
            },
            "sequenceNumber": "000000000023",
        },
        "AccessAndMobilitySubscriptionData": {
            "gpsis": [msisdn],
            "nssai": {
                "defaultSingleNssais": [{"sst": sst, "sd": sd, "isDefault": True}],
                "singleNssais": [{"sst": sst, "sd": sd, "isDefault": True}],
            },
            "subscribedUeAmbr": {"downlink": ue_ambr["downlink"], "uplink": ue_ambr["uplink"]},
        },
        "SessionManagementSubscriptionData": [
            {
                "singleNssai": {"sst": sst, "sd": sd},
                "dnnConfigurations": {dnn: dnn_config},
            }
        ],
        "SmfSelectionSubscriptionData": {
            "subscribedSnssaiInfos": {snssai_key: {"dnnInfos": [{"dnn": dnn}]}}
        },
        "AmPolicyData": {"subscCats": ["5GCityVerse", event_type, cfg["slice_type"]]},
        "SmPolicyData": {
            "smPolicySnssaiData": {
                snssai_key: {
                    "snssai": {"sst": sst, "sd": sd},
                    "smPolicyDnnData": {dnn: {"dnn": dnn}},
                }
            }
        },
        "FlowRules": [
            {
                "snssai": snssai_key,
                "dnn": dnn,
                "filter": flow_filter,
                "precedence": 128,
                "qosRef": qos_ref,
            }
        ],
        "QosFlows": [
            {
                "qosRef": qos_ref,
                "5qi": five_qi,
                "snssai": snssai_key,
                "dnn": dnn,
                "gbrUL": gbr["uplink"],
                "gbrDL": gbr["downlink"],
                "mbrUL": mbr["uplink"],
                "mbrDL": mbr["downlink"],
            }
        ],
        "ChargingDatas": [
            {
                "snssai": snssai_key,
                "dnn": "",
                "filter": "",
                "chargingMethod": "Offline",
                "quota": "100000",
                "unitCost": "1",
            },
            {
                "snssai": snssai_key,
                "dnn": dnn,
                "filter": flow_filter,
                "qosRef": qos_ref,
                "chargingMethod": "Offline",
                "quota": "100000",
                "unitCost": "1",
            },
        ],
    }


def build_free5gc_profile(profile_name, event_type, cfg):
    subscriber = build_free5gc_subscriber(event_type, cfg, "imsi-000000000000000", "msisdn-0000000000")
    return {
        "profileName": profile_name,
        "AccessAndMobilitySubscriptionData": subscriber["AccessAndMobilitySubscriptionData"],
        "SessionManagementSubscriptionData": subscriber["SessionManagementSubscriptionData"],
        "SmfSelectionSubscriptionData": subscriber["SmfSelectionSubscriptionData"],
        "AmPolicyData": subscriber["AmPolicyData"],
        "SmPolicyData": subscriber["SmPolicyData"],
        "FlowRules": subscriber["FlowRules"],
        "QosFlows": subscriber["QosFlows"],
        "ChargingDatas": subscriber["ChargingDatas"],
    }


def build_decision(event_type, cfg, free5gc_result, environment_result):
    free5gc_action_status = "success" if free5gc_result.get("status") == "success" else "failed"
    environment_action_status = "success" if environment_result.get("status") == "success" else "failed"
    return {
        "agentName": "Supervisor Agent",
        "riskLevel": cfg["risk"],
        "decision": (
            f"{event_type} accepted on AWS. The backend writes a real free5GC subscriber/profile "
            f"for {cfg['slice_type']} SST={cfg['slice_sst']} SD={cfg['slice_sd']} with 5QI={cfg['five_qi']}. "
            "Traffic behavior is driven by the matching UERANSIM slice and iperf3 profile when the EKS jobs are applied."
        ),
        "actions": [
            {
                "type": "free5gc_subscriber",
                "description": f"Create/update free5GC subscriber QoS/NSSAI for {event_type}",
                "api": "free5GC WebUI /api/subscriber",
                "status": free5gc_action_status,
                "httpStatus": free5gc_result.get("httpStatus", 0),
            },
            {
                "type": "ueransim",
                "description": f"Start {cfg['ue_count']} UE(s) and launch traffic profile: {cfg['traffic_profile']}",
                "api": "EKS Kubernetes API: UERANSIM + iperf3 Job",
                "status": environment_action_status,
                "httpStatus": environment_result.get("httpStatus", 500),
            },
            {
                "type": "prometheus",
                "description": "Dashboard metrics prefer Prometheus UPF/SMF/AMF counters",
                "api": "Prometheus /api/v1/query",
                "status": "success",
                "httpStatus": 200,
            },
        ],
        "expectedOutcome": "Subscriber QoS and NSSAI match the scenario; real traffic can be injected by the EKS iperf3 job and read back through Prometheus.",
        "score": cfg["score"],
        "startedAt": now(),
    }


def get_event(execution_id):
    res = table.get_item(Key={"pk": f"EVENT#{execution_id}", "sk": "STATUS"})
    item = res.get("Item")
    if not item:
        return None
    item.pop("pk", None)
    item.pop("sk", None)
    return from_dynamodb(item)


def broadcast(message):
    res = table.query(
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": "WS_CONNECTION"},
    )
    for conn in res.get("Items", []):
        conn_id = conn["sk"]
        try:
            post_to_connection(conn_id, message)
        except apigw.exceptions.GoneException:
            table.delete_item(Key={"pk": "WS_CONNECTION", "sk": conn_id})


def post_to_connection(conn_id, message):
    apigw.post_to_connection(ConnectionId=conn_id, Data=json.dumps(message).encode("utf-8"))


def default_metrics():
    return {
        "upfCpuPercent": 18.5,
        "upfPodCount": 1,
        "amfPodCount": 1,
        "gtpPacketsPerSec": 0,
        "pduSessionCount": 0,
        "latencyMs": 8.0,
        "throughputMbps": 100.0,
        "timestamp": int(time.time() * 1000),
        "dataSource": "simulated",
    }


def event_metrics(event_type):
    base = default_metrics()
    base.update(
        {
            "upfCpuPercent": 72.0 if event_type == "concert" else 46.0,
            "upfPodCount": 2,
            "amfPodCount": 2 if event_type in ("typhoon", "iot_surge") else 1,
            "gtpPacketsPerSec": 2200,
            "pduSessionCount": EVENT_CONFIG[event_type]["ue_count"],
            "throughputMbps": 820.0 if event_type == "concert" else 260.0,
            "timestamp": int(time.time() * 1000),
        }
    )
    return base


def default_slices():
    return [
        {"sst": 1, "type": "eMBB", "sd": "000001", "load": 20, "sessions": 120, "trend": "stable"},
        {"sst": 2, "type": "URLLC", "sd": "000002", "load": 10, "sessions": 34, "trend": "stable"},
        {"sst": 3, "type": "mMTC", "sd": "000004", "load": 15, "sessions": 2400, "trend": "stable"},
        {"sst": 4, "type": "V2X", "sd": "000005", "load": 5, "sessions": 18, "trend": "stable"},
    ]


def event_slices(event_type):
    slices = default_slices()
    target = EVENT_CONFIG[event_type]["slice_sst"]
    for item in slices:
        if item["sst"] == target:
            item["sd"] = EVENT_CONFIG[event_type]["slice_sd"]
            item["load"] = min(item["load"] + 55, 100)
            item["sessions"] += EVENT_CONFIG[event_type]["ue_count"]
            item["trend"] = "up"
    return slices


def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
            "access-control-allow-methods": "GET,POST,OPTIONS",
            "access-control-allow-headers": "content-type",
        },
        "body": json.dumps(from_dynamodb(body)),
    }


def to_dynamodb(value):
    return json.loads(json.dumps(value), parse_float=Decimal)


def from_dynamodb(value):
    if isinstance(value, list):
        return [from_dynamodb(v) for v in value]
    if isinstance(value, dict):
        return {k: from_dynamodb(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    return value


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
