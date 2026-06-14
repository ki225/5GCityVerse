"""
Local development backend for 5GCityVerse.

This is not the production AWS backend. It lets the React UI exercise the same
REST/WebSocket contract locally while the agent path calls the official free5GC
MCP server configured in mcp.json.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from free5gc_mcp_probe import McpHttpClient, load_server_url  # noqa: E402


PORT = int(os.environ.get("LOCAL_BACKEND_PORT", "8090"))
AWS_PROFILE = os.environ.get("AWS_PROFILE", "")
MCP_CONFIG = ROOT / os.environ.get("MCP_CONFIG", "mcp.json")
MCP_SERVER = os.environ.get("MCP_SERVER", "free5gc-mcp")

EVENT_CONFIG = {
    "concert": {"slice_sst": 1, "slice_type": "eMBB", "ue_count": 50, "risk": "high", "score": 88, "imsi_suffix": "9001"},
    "typhoon": {"slice_sst": 3, "slice_type": "mMTC", "ue_count": 200, "risk": "critical", "score": 95, "imsi_suffix": "9002"},
    "accident": {"slice_sst": 4, "slice_type": "V2X", "ue_count": 20, "risk": "high", "score": 85, "imsi_suffix": "9003"},
    "medical": {"slice_sst": 2, "slice_type": "URLLC", "ue_count": 10, "risk": "critical", "score": 92, "imsi_suffix": "9004"},
    "iot_surge": {"slice_sst": 3, "slice_type": "mMTC", "ue_count": 500, "risk": "high", "score": 80, "imsi_suffix": "9005"},
}

FREE5GC_WEBUI_URL = os.environ.get(
    "FREE5GC_WEBUI_URL",
    "http://ab1be331f34b846dfa58d61963046526-1596480068.ap-northeast-1.elb.amazonaws.com:5000",
).rstrip("/")
FREE5GC_WEBUI_USERNAME = os.environ.get("FREE5GC_WEBUI_USERNAME", "admin")
FREE5GC_WEBUI_PASSWORD = os.environ.get("FREE5GC_WEBUI_PASSWORD", "free5gc")
FREE5GC_PLMN_ID = os.environ.get("FREE5GC_PLMN_ID", "20893")
FREE5GC_IMSI_PREFIX = os.environ.get("FREE5GC_IMSI_PREFIX", "20893000000")
EVENT_BY_IMSI_SUFFIX = {cfg["imsi_suffix"]: name for name, cfg in EVENT_CONFIG.items()}

connections: set[web.WebSocketResponse] = set()
executions: dict[str, dict[str, Any]] = {}


async def handle_trigger(request: web.Request) -> web.Response:
    body = await request.json()
    event_type = body.get("event_type")
    if event_type not in EVENT_CONFIG:
        return json_response({"error": f"Unknown event_type: {event_type}"}, status=400)

    execution_id = str(uuid.uuid4())
    cfg = EVENT_CONFIG[event_type]
    executions[execution_id] = {
        "executionId": execution_id,
        "eventType": event_type,
        "status": "STARTED",
        "config": cfg,
        "awsProfile": AWS_PROFILE,
    }

    await broadcast({"type": "event_started", "payload": {"executionId": execution_id, "eventType": event_type}})
    asyncio.create_task(run_agent_decision(execution_id, event_type, cfg))
    return json_response({"executionId": execution_id, "eventType": event_type})


async def handle_reset(_request: web.Request) -> web.Response:
    executions.clear()
    free5gc_reset = reset_free5gc_subscribers()
    await broadcast({"type": "slice_update", "payload": default_slices()})
    await broadcast({"type": "metrics_update", "payload": default_metrics()})
    free5gc_status = get_free5gc_status_payload()
    await broadcast({"type": "free5gc_status", "payload": free5gc_status})
    return json_response({"status": "reset", "free5gc": free5gc_reset})


async def handle_status(request: web.Request) -> web.Response:
    execution_id = request.match_info["execution_id"]
    item = executions.get(execution_id)
    if not item:
        return json_response({"error": "Execution not found"}, status=404)
    return json_response(item)


async def handle_slices(_request: web.Request) -> web.Response:
    return json_response(default_slices())


async def handle_metrics(_request: web.Request) -> web.Response:
    return json_response(default_metrics())


async def handle_free5gc_status(_request: web.Request) -> web.Response:
    return json_response(get_free5gc_status_payload())


async def handle_options(_request: web.Request) -> web.Response:
    return web.Response(
        status=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": "content-type",
        },
    )


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    connections.add(ws)
    await ws.send_json({"type": "metrics_update", "payload": default_metrics()})
    await ws.send_json({"type": "slice_update", "payload": default_slices()})

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("action") == "ping":
                    await ws.send_json({"type": "pong", "payload": {}})
    finally:
        connections.discard(ws)
    return ws


async def run_agent_decision(execution_id: str, event_type: str, cfg: dict[str, Any]) -> None:
    await asyncio.sleep(0.2)

    mcp_summary: dict[str, Any] = {"connected": False}
    free5gc_result = upsert_free5gc_subscriber(event_type, cfg, execution_id)
    free5gc_action_status = "success" if free5gc_result.get("status") == "success" else "failed"
    actions = [
        {
            "type": "free5gc_subscriber",
            "description": f"Create/update free5GC subscriber record for {event_type}",
            "api": "free5GC WebUI /api/subscriber",
            "status": free5gc_action_status,
            "httpStatus": free5gc_result.get("httpStatus", 0),
        },
        {
            "type": "k8s_hpa",
            "description": "Read free5GC NF status through official MCP",
            "api": "official-free5gc-mcp: local_free5gc_status",
            "status": "running",
        },
    ]

    try:
        mcp_url = load_server_url(MCP_CONFIG, MCP_SERVER)
        client = McpHttpClient(mcp_url)
        init_result = client.initialize()
        tools = client.list_tools()
        subscriber_result = client.call_tool("subscriber_list", {})
        status_result = client.call_tool("local_free5gc_status", {})
        mcp_summary = {
            "connected": True,
            "server": init_result.get("serverInfo", {}),
            "toolCount": len(tools.get("tools", [])),
            "subscriberList": subscriber_result,
            "status": status_result,
        }
        actions[1]["status"] = "success"
        actions[1]["httpStatus"] = 200
    except Exception as exc:
        mcp_summary = {"connected": False, "error": str(exc)}
        actions[1]["status"] = "failed"

    decision = {
        "agentName": "Supervisor Agent",
        "riskLevel": cfg["risk"],
        "decision": (
            f"{event_type} event accepted. Local backend used AWS_PROFILE={AWS_PROFILE or 'unset'} "
            f"and official free5GC MCP connected={mcp_summary['connected']}."
        ),
        "actions": actions,
        "expectedOutcome": "UI/backend/WebSocket path verified; official free5GC MCP tool path exercised.",
        "score": cfg["score"] if mcp_summary["connected"] else 40,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    executions[execution_id]["status"] = "AGENT_COMPLETE"
    executions[execution_id]["agentDecision"] = decision
    executions[execution_id]["mcp"] = mcp_summary
    executions[execution_id]["free5gc"] = free5gc_result

    await broadcast({"type": "agent_decision", "payload": decision})
    await broadcast({"type": "metrics_update", "payload": event_metrics(event_type)})
    await broadcast({"type": "slice_update", "payload": event_slices(event_type)})
    await broadcast({"type": "free5gc_status", "payload": get_free5gc_status_payload()})
    await broadcast(
        {
            "type": "pod_event",
            "payload": {
                "event": "ADDED",
                "pod": "upf-local-dev-2",
                "phase": "Running",
                "component": "UPF",
                "namespace": "free5gc",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        }
    )


async def broadcast(message: dict[str, Any]) -> None:
    stale: list[web.WebSocketResponse] = []
    for ws in connections:
        if ws.closed:
            stale.append(ws)
            continue
        await ws.send_json(message)
    for ws in stale:
        connections.discard(ws)


def default_metrics() -> dict[str, Any]:
    return {
        "upfCpuPercent": 18.5,
        "upfPodCount": 1,
        "amfPodCount": 1,
        "gtpPacketsPerSec": 0,
        "pduSessionCount": 0,
        "latencyMs": 8.0,
        "throughputMbps": 100.0,
        "timestamp": int(time.time() * 1000),
    }


def get_free5gc_status_payload() -> dict[str, Any]:
    started = time.time()
    if not FREE5GC_WEBUI_URL:
        return {
            "connected": False,
            "source": "free5GC WebUI API",
            "error": "FREE5GC_WEBUI_URL is not configured",
            "subscribers": [],
            "eventSubscribers": [],
            "metrics": default_metrics(),
            "slices": default_slices(),
            "checkedAt": now(),
        }

    try:
        subscribers = free5gc_list_subscribers()
        registered_ues = free5gc_registered_ues()
        profiles = free5gc_profiles()
        event_subscribers = [s for s in subscribers if event_type_from_ue_id(s.get("ueId", ""))]
        metrics = default_metrics()
        metrics.update(
            {
                "pduSessionCount": len(registered_ues),
                "throughputMbps": round(100 + len(event_subscribers) * 60, 1),
                "gtpPacketsPerSec": len(event_subscribers) * 120,
                "latencyMs": round((time.time() - started) * 1000, 1),
                "timestamp": int(time.time() * 1000),
            }
        )
        return {
            "connected": True,
            "source": "free5GC WebUI API /api/subscriber",
            "subscriberCount": len(subscribers),
            "eventSubscriberCount": len(event_subscribers),
            "registeredUeCount": len(registered_ues),
            "profileCount": len(profiles),
            "subscribers": subscribers,
            "eventSubscribers": event_subscribers,
            "registeredUes": registered_ues,
            "profiles": profiles,
            "metrics": metrics,
            "slices": slices_from_free5gc_subscribers(event_subscribers),
            "checkedAt": now(),
        }
    except Exception as exc:
        return {
            "connected": False,
            "source": "free5GC WebUI API /api/subscriber",
            "error": str(exc),
            "subscribers": [],
            "eventSubscribers": [],
            "registeredUes": [],
            "profiles": [],
            "metrics": default_metrics(),
            "slices": default_slices(),
            "checkedAt": now(),
        }


def upsert_free5gc_subscriber(event_type: str, cfg: dict[str, Any], execution_id: str) -> dict[str, Any]:
    if not FREE5GC_WEBUI_URL:
        return {"status": "skipped", "reason": "FREE5GC_WEBUI_URL is not configured"}

    ue_id = f"imsi-{FREE5GC_IMSI_PREFIX}{cfg['imsi_suffix']}"
    msisdn = f"msisdn-090000{cfg['imsi_suffix']}"
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
        return {
            "status": "success" if status < 300 else "error",
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


def free5gc_login() -> str:
    status, data = http_json(
        "POST",
        f"{FREE5GC_WEBUI_URL}/api/login",
        {"username": FREE5GC_WEBUI_USERNAME, "password": FREE5GC_WEBUI_PASSWORD},
    )
    if status >= 300 or not data.get("access_token"):
        raise RuntimeError(f"free5GC login failed: HTTP {status} {data}")
    return data["access_token"]


def free5gc_request(method: str, path: str, token: str, body: Any = None) -> tuple[int, Any]:
    return http_json(method, f"{FREE5GC_WEBUI_URL}{path}", body, {"Token": token})


def free5gc_list_subscribers() -> list[dict[str, Any]]:
    token = free5gc_login()
    status, data = free5gc_request("GET", "/api/subscriber", token)
    if status >= 300:
        raise RuntimeError(f"free5GC subscriber list failed: HTTP {status} {data}")
    return data if isinstance(data, list) else []


def free5gc_registered_ues() -> list[dict[str, Any]]:
    token = free5gc_login()
    status, data = free5gc_request("GET", "/api/registered-ue-context", token)
    if status >= 300:
        return []
    return data if isinstance(data, list) else []


def free5gc_profiles() -> list[str]:
    token = free5gc_login()
    status, data = free5gc_request("GET", "/api/profile", token)
    if status >= 300:
        return []
    return data if isinstance(data, list) else []


def upsert_free5gc_profile(token: str, event_type: str, cfg: dict[str, Any]) -> dict[str, Any]:
    profile_name = f"5GCityVerse-{event_type}"
    payload = build_free5gc_profile(profile_name, event_type, cfg)
    status, body = free5gc_request("POST", "/api/profile", token, payload)
    operation = "created"
    if status == 409:
        status, body = free5gc_request("PUT", f"/api/profile/{profile_name}", token, payload)
        operation = "updated"
    return {"name": profile_name, "operation": operation, "httpStatus": status, "response": body}


def reset_free5gc_subscribers() -> dict[str, Any]:
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
            if not event_type_from_ue_id(ue_id):
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


def http_json(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
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


def event_type_from_ue_id(ue_id: str) -> str | None:
    for suffix, event_type in EVENT_BY_IMSI_SUFFIX.items():
        if ue_id.endswith(suffix):
            return event_type
    return None


def slices_from_free5gc_subscribers(event_subscribers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    slices = default_slices()
    event_counts: dict[str, int] = {}
    for subscriber in event_subscribers:
        event_type = event_type_from_ue_id(subscriber.get("ueId", ""))
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


def build_free5gc_subscriber(event_type: str, cfg: dict[str, Any], ue_id: str, msisdn: str) -> dict[str, Any]:
    sst = cfg["slice_sst"]
    sd = f"{sst:06d}"
    snssai_key = f"{sst:02d}{sd}"
    five_qi = 1 if sst == 2 else 5 if sst == 4 else 9
    qos_ref = 0
    flow_filter = "1.1.1.1/32"
    return {
        "plmnID": FREE5GC_PLMN_ID,
        "ueId": ue_id,
        "AuthenticationSubscription": {
            "authenticationManagementField": "8000",
            "authenticationMethod": "5G_AKA",
            "milenage": {
                "op": {
                    "encryptionAlgorithm": 0,
                    "encryptionKey": 0,
                    "opValue": "",
                }
            },
            "opc": {"encryptionAlgorithm": 0, "encryptionKey": 0, "opcValue": "8e27b6af0e692e750f32667a3b14605d"},
            "permanentKey": {
                "encryptionAlgorithm": 0,
                "encryptionKey": 0,
                "permanentKeyValue": "8baf473f2f8fd09487cccbd7097c6862",
            },
            "sequenceNumber": "16f3b3f70fc2",
        },
        "AccessAndMobilitySubscriptionData": {
            "gpsis": [msisdn],
            "nssai": {
                "defaultSingleNssais": [{"sst": sst, "sd": sd, "isDefault": True}],
                "singleNssais": [],
            },
            "subscribedUeAmbr": {"downlink": "2 Gbps", "uplink": "1 Gbps"},
        },
        "SessionManagementSubscriptionData": [
            {
                "singleNssai": {"sst": sst, "sd": sd},
                "dnnConfigurations": {
                    "internet": {
                        "sscModes": {
                            "defaultSscMode": "SSC_MODE_1",
                            "allowedSscModes": ["SSC_MODE_2", "SSC_MODE_3"],
                        },
                        "pduSessionTypes": {
                            "defaultSessionType": "IPV4",
                            "allowedSessionTypes": ["IPV4"],
                        },
                        "sessionAmbr": {"uplink": "200 Mbps", "downlink": "100 Mbps"},
                        "5gQosProfile": {"5qi": five_qi, "arp": {"priorityLevel": 8}, "priorityLevel": 8},
                    }
                },
            }
        ],
        "SmfSelectionSubscriptionData": {
            "subscribedSnssaiInfos": {snssai_key: {"dnnInfos": [{"dnn": "internet"}]}}
        },
        "AmPolicyData": {"subscCats": ["5GCityVerse", event_type, cfg["slice_type"]]},
        "SmPolicyData": {
            "smPolicySnssaiData": {
                snssai_key: {
                    "snssai": {"sst": sst, "sd": sd},
                    "smPolicyDnnData": {"internet": {"dnn": "internet"}},
                }
            }
        },
        "FlowRules": [
            {
                "snssai": snssai_key,
                "dnn": "internet",
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
                "dnn": "internet",
                "gbrUL": "",
                "gbrDL": "",
                "mbrUL": "200 Mbps",
                "mbrDL": "100 Mbps",
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
                "dnn": "internet",
                "filter": flow_filter,
                "qosRef": qos_ref,
                "chargingMethod": "Offline",
                "quota": "100000",
                "unitCost": "1",
            },
        ],
    }


def build_free5gc_profile(profile_name: str, event_type: str, cfg: dict[str, Any]) -> dict[str, Any]:
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


def event_metrics(event_type: str) -> dict[str, Any]:
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


def default_slices() -> list[dict[str, Any]]:
    return [
        {"sst": 1, "type": "eMBB", "sd": "000001", "load": 20, "sessions": 120, "trend": "stable"},
        {"sst": 2, "type": "URLLC", "sd": "000002", "load": 10, "sessions": 34, "trend": "stable"},
        {"sst": 3, "type": "mMTC", "sd": "000003", "load": 15, "sessions": 2400, "trend": "stable"},
        {"sst": 4, "type": "V2X", "sd": "000004", "load": 5, "sessions": 18, "trend": "stable"},
    ]


def event_slices(event_type: str) -> list[dict[str, Any]]:
    slices = default_slices()
    target = EVENT_CONFIG[event_type]["slice_sst"]
    for item in slices:
        if item["sst"] == target:
            item["load"] = min(item["load"] + 55, 100)
            item["sessions"] += EVENT_CONFIG[event_type]["ue_count"]
            item["trend"] = "up"
    return slices


def json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(
        data,
        status=status,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": "content-type",
        },
    )


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_options("/{tail:.*}", handle_options)
    app.router.add_post("/api/events/trigger", handle_trigger)
    app.router.add_post("/api/events/reset", handle_reset)
    app.router.add_get("/api/events/status/{execution_id}", handle_status)
    app.router.add_get("/api/network/slices", handle_slices)
    app.router.add_get("/api/metrics/current", handle_metrics)
    app.router.add_get("/api/free5gc/status", handle_free5gc_status)
    app.router.add_get("/ws", websocket_handler)
    return app


if __name__ == "__main__":
    print(f"5GCityVerse local backend on http://127.0.0.1:{PORT}")
    print(f"AWS_PROFILE={AWS_PROFILE or 'unset'}")
    print(f"MCP config={MCP_CONFIG}")
    web.run_app(create_app(), host="127.0.0.1", port=PORT)
