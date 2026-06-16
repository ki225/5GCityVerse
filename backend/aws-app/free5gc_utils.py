from __future__ import annotations

import time
from typing import Any

from config import CITYVERSE_UE_IDS, EVENT_BY_IMSI_SUFFIX, EVENT_CONFIG
from http_client import HttpJsonClient
from metrics_service import PrometheusMetricsService
from models import EventConfig
from slice_catalog import SliceCatalog
from time_utils import TimeUtils


class Free5gcClient:
    def __init__(
        self,
        webui_url: str,
        username: str,
        password: str,
        plmn_id: str,
        metrics: PrometheusMetricsService,
        http: HttpJsonClient | None = None,
    ) -> None:
        self.webui_url = webui_url
        self.username = username
        self.password = password
        self.plmn_id = plmn_id
        self.metrics = metrics
        self.http = http or HttpJsonClient()

    def login(self) -> str:
        status, data = self.http.request(
            "POST",
            f"{self.webui_url}/api/login",
            {"username": self.username, "password": self.password},
        )
        if status >= 300 or not data.get("access_token"):
            raise RuntimeError(f"free5GC login failed: HTTP {status} {data}")
        return data["access_token"]

    def request(self, method: str, path: str, token: str, body: Any = None) -> tuple[int, Any]:
        return self.http.request(method, f"{self.webui_url}{path}", body, {"Token": token})

    def list_subscribers(self) -> list[dict[str, Any]]:
        token = self.login()
        status, data = self.request("GET", "/api/subscriber", token)
        if status >= 300:
            raise RuntimeError(f"free5GC subscriber list failed: HTTP {status} {data}")
        return data if isinstance(data, list) else []

    def registered_ues(self) -> list[dict[str, Any]]:
        token = self.login()
        status, data = self.request("GET", "/api/registered-ue-context", token)
        return data if status < 300 and isinstance(data, list) else []

    def profiles(self) -> list[Any]:
        token = self.login()
        status, data = self.request("GET", "/api/profile", token)
        return data if status < 300 and isinstance(data, list) else []

    def status_payload(self) -> dict[str, Any]:
        started = time.time()
        real_metrics = self.metrics.get_real_metrics()
        if not self.webui_url:
            metrics = real_metrics or self.metrics.default_metrics()
            return {
                "connected": False,
                "source": "free5GC WebUI API",
                "error": "FREE5GC_WEBUI_URL is not configured",
                "subscribers": [],
                "eventSubscribers": [],
                "metrics": metrics,
                "slices": self.current_slices(metrics),
                "checkedAt": TimeUtils.now(),
            }

        try:
            subscribers = self.list_subscribers()
            registered_ues = self.registered_ues()
            profiles = self.profiles()
            event_subscribers = [s for s in subscribers if self.is_cityverse_subscriber(s)]
            metrics = real_metrics or self.metrics.estimated_from_free5gc(started, event_subscribers, registered_ues)
            slices = self.current_slices(metrics, event_subscribers)
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
                "checkedAt": TimeUtils.now(),
            }
        except Exception as exc:
            metrics = real_metrics or self.metrics.default_metrics()
            return {
                "connected": False,
                "source": "free5GC WebUI API /api/subscriber",
                "error": str(exc),
                "subscribers": [],
                "eventSubscribers": [],
                "registeredUes": [],
                "profiles": [],
                "metrics": metrics,
                "slices": self.current_slices(metrics),
                "checkedAt": TimeUtils.now(),
            }

    def upsert_subscribers(self, event_type: str, cfg: EventConfig, execution_id: str, limit: int) -> dict[str, Any]:
        if len(cfg.ue_ids) > limit:
            return {
                "status": "success",
                "operation": "preseeded",
                "httpStatus": 200,
                "ueCount": len(cfg.ue_ids),
                "successCount": len(cfg.ue_ids),
                "errorCount": 0,
                "ueIds": cfg.ue_ids,
                "eventType": event_type,
                "executionId": execution_id,
                "reason": "Subscribers are pre-seeded during deploy/start to keep frontend-triggered activation under API Gateway timeout.",
            }

        results = [self.upsert_subscriber(event_type, cfg, execution_id, ue_id) for ue_id in cfg.ue_ids]
        success = [item for item in results if item.get("status") == "success"]
        errors = [item for item in results if item.get("status") != "success"]
        return {
            "status": "success" if not errors else "partial" if success else "error",
            "httpStatus": 200 if not errors else errors[0].get("httpStatus", 500),
            "ueCount": len(results),
            "successCount": len(success),
            "errorCount": len(errors),
            "ueIds": cfg.ue_ids,
            "eventType": event_type,
            "executionId": execution_id,
            "results": results,
        }

    def upsert_subscriber(self, event_type: str, cfg: EventConfig, execution_id: str, ue_id: str) -> dict[str, Any]:
        if not self.webui_url:
            return {"status": "skipped", "reason": "FREE5GC_WEBUI_URL is not configured"}

        msisdn = f"msisdn-{ue_id[-10:]}"
        payload = self.build_subscriber(event_type, cfg, ue_id, msisdn)
        path = f"/api/subscriber/{ue_id}/{self.plmn_id}"

        try:
            token = self.login()
            status, body = self.request("POST", path, token, payload)
            operation = "created"
            if status >= 400:
                status, body = self.request("PUT", path, token, payload)
                operation = "updated"
            profile_result = self.upsert_profile(token, event_type, cfg)
            return {
                "status": "success" if status < 300 else "error",
                "operation": operation,
                "httpStatus": status,
                "ueId": ue_id,
                "plmnID": self.plmn_id,
                "eventType": event_type,
                "executionId": execution_id,
                "profile": profile_result,
                "response": body,
            }
        except Exception as exc:
            return {
                "status": "error",
                "ueId": ue_id,
                "plmnID": self.plmn_id,
                "eventType": event_type,
                "executionId": execution_id,
                "error": str(exc),
            }

    def upsert_profile(self, token: str, event_type: str, cfg: EventConfig) -> dict[str, Any]:
        profile_name = f"5GCityVerse-{event_type}"
        payload = self.build_profile(profile_name, event_type, cfg)
        status, body = self.request("POST", "/api/profile", token, payload)
        operation = "created"
        if status == 409:
            status, body = self.request("PUT", f"/api/profile/{profile_name}", token, payload)
            operation = "updated"
        return {"name": profile_name, "operation": operation, "httpStatus": status, "response": body}

    def reset_subscribers(self) -> dict[str, Any]:
        if not self.webui_url:
            return {"status": "skipped", "reason": "FREE5GC_WEBUI_URL is not configured"}

        try:
            token = self.login()
            subscribers = self.list_subscribers()
            deleted = []
            errors = []
            for subscriber in subscribers:
                ue_id = subscriber.get("ueId", "")
                plmn_id = subscriber.get("plmnID", self.plmn_id)
                if not self.is_cityverse_ue_id(ue_id):
                    continue
                status, data = self.request("DELETE", f"/api/subscriber/{ue_id}/{plmn_id}", token)
                if status < 300:
                    deleted.append(ue_id)
                else:
                    errors.append({"ueId": ue_id, "httpStatus": status, "response": data})
            for event_type in EVENT_CONFIG:
                profile_name = f"5GCityVerse-{event_type}"
                status, data = self.request("DELETE", f"/api/profile/{profile_name}", token)
                if status not in (200, 204, 404):
                    errors.append({"profile": profile_name, "httpStatus": status, "response": data})
            return {"status": "success" if not errors else "partial", "deleted": deleted, "errors": errors}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def current_slices(
        self,
        metrics: dict[str, Any] | None = None,
        event_subscribers: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        real_slices = self.metrics.real_slice_metrics()
        if real_slices:
            return real_slices
        if event_subscribers is not None:
            event_counts: dict[str, int] = {}
            for subscriber in event_subscribers:
                event_type = self.event_type_from_subscriber(subscriber)
                if event_type:
                    event_counts[event_type] = event_counts.get(event_type, 0) + 1
            return SliceCatalog.slices_from_event_counts(event_counts)
        return SliceCatalog.default_slices()

    def event_type_from_ue_id(self, ue_id: str) -> str | None:
        if ue_id in CITYVERSE_UE_IDS:
            for event_type, cfg in EVENT_CONFIG.items():
                if ue_id in cfg.ue_ids:
                    return event_type
        for suffix, event_type in EVENT_BY_IMSI_SUFFIX.items():
            if ue_id.endswith(suffix):
                return event_type
        return None

    def is_cityverse_ue_id(self, ue_id: str) -> bool:
        return ue_id in CITYVERSE_UE_IDS or bool(self.event_type_from_ue_id(ue_id))

    def is_cityverse_subscriber(self, subscriber: dict[str, Any]) -> bool:
        return self.is_cityverse_ue_id(subscriber.get("ueId", ""))

    def event_type_from_subscriber(self, subscriber: dict[str, Any]) -> str | None:
        categories = (subscriber.get("AmPolicyData") or {}).get("subscCats") or []
        for item in categories:
            if item in EVENT_CONFIG:
                return item
        return self.event_type_from_ue_id(subscriber.get("ueId", ""))

    def build_subscriber(self, event_type: str, cfg: EventConfig, ue_id: str, msisdn: str) -> dict[str, Any]:
        sst = cfg.slice_sst
        sd = cfg.slice_sd
        snssai_key = f"{sst:02d}{sd}"
        qos_ref = 0
        priority = 1 if cfg.slice_type == "URLLC" else 15 if cfg.slice_type == "mMTC" else 8
        preempt_cap = "MAY_PREEMPT" if cfg.slice_type == "URLLC" else "NOT_PREEMPT"
        preempt_vuln = "PREEMPTABLE" if cfg.slice_type == "mMTC" else "NOT_PREEMPTABLE"
        dnn_config = {
            "sscModes": {"defaultSscMode": "SSC_MODE_1", "allowedSscModes": ["SSC_MODE_2", "SSC_MODE_3"]},
            "pduSessionTypes": {"defaultSessionType": "IPV4", "allowedSessionTypes": ["IPV4"]},
            "sessionAmbr": {"uplink": cfg.session_ambr.uplink, "downlink": cfg.session_ambr.downlink},
            "5gQosProfile": {
                "5qi": cfg.five_qi,
                "arp": {"priorityLevel": priority, "preemptCap": preempt_cap, "preemptVuln": preempt_vuln},
                "priorityLevel": priority,
            },
            "staticIpAddress": [],
        }
        if cfg.gbr.uplink and cfg.gbr.downlink:
            dnn_config["gbrQosFlowInfo"] = {
                "maxFbrUplink": cfg.mbr.uplink,
                "maxFbrDownlink": cfg.mbr.downlink,
                "guaranteedFbrUplink": cfg.gbr.uplink,
                "guaranteedFbrDownlink": cfg.gbr.downlink,
            }

        flow_filter = "permit out ip from any to any"
        return {
            "plmnID": self.plmn_id,
            "ueId": ue_id,
            "AuthenticationSubscription": {
                "authenticationManagementField": "8000",
                "authenticationMethod": "5G_AKA",
                "milenage": {"op": {"opValue": "8e27b6af0e692e750f32667a3b14605d"}},
                "opc": {"opcValue": "8e27b6af0e692e750f32667a3b14605d"},
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
                "subscribedUeAmbr": {"downlink": cfg.ue_ambr.downlink, "uplink": cfg.ue_ambr.uplink},
            },
            "SessionManagementSubscriptionData": [{"singleNssai": {"sst": sst, "sd": sd}, "dnnConfigurations": {cfg.dnn: dnn_config}}],
            "SmfSelectionSubscriptionData": {"subscribedSnssaiInfos": {snssai_key: {"dnnInfos": [{"dnn": cfg.dnn}]}}},
            "AmPolicyData": {"subscCats": ["5GCityVerse", event_type, cfg.slice_type]},
            "SmPolicyData": {
                "smPolicySnssaiData": {
                    snssai_key: {"snssai": {"sst": sst, "sd": sd}, "smPolicyDnnData": {cfg.dnn: {"dnn": cfg.dnn}}}
                }
            },
            "FlowRules": [{"snssai": snssai_key, "dnn": cfg.dnn, "filter": flow_filter, "precedence": 128, "qosRef": qos_ref}],
            "QosFlows": [
                {
                    "qosRef": qos_ref,
                    "5qi": cfg.five_qi,
                    "snssai": snssai_key,
                    "dnn": cfg.dnn,
                    "gbrUL": cfg.gbr.uplink,
                    "gbrDL": cfg.gbr.downlink,
                    "mbrUL": cfg.mbr.uplink,
                    "mbrDL": cfg.mbr.downlink,
                }
            ],
            "ChargingDatas": [
                {"snssai": snssai_key, "dnn": "", "filter": "", "chargingMethod": "Offline", "quota": "100000", "unitCost": "1"},
                {
                    "snssai": snssai_key,
                    "dnn": cfg.dnn,
                    "filter": flow_filter,
                    "qosRef": qos_ref,
                    "chargingMethod": "Offline",
                    "quota": "100000",
                    "unitCost": "1",
                },
            ],
        }

    def build_profile(self, profile_name: str, event_type: str, cfg: EventConfig) -> dict[str, Any]:
        subscriber = self.build_subscriber(event_type, cfg, "imsi-000000000000000", "msisdn-0000000000")
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

