from __future__ import annotations

import os
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
        self.status_timeout = float(os.environ.get("FREE5GC_STATUS_HTTP_TIMEOUT_SECONDS", "2"))
        self.upsert_retries = int(os.environ.get("FREE5GC_UPSERT_RETRIES", "3"))
        self._cached_token: str | None = None
        self._token_at: float = 0.0
        self._token_ttl = float(os.environ.get("FREE5GC_TOKEN_TTL_SECONDS", "300"))

    def _do_login(self, timeout: float | None = None) -> str:
        status, data = self.http.request(
            "POST",
            f"{self.webui_url}/api/login",
            {"username": self.username, "password": self.password},
            timeout=timeout,
        )
        if status == 0:
            raise RuntimeError(data.get("error", "free5GC WebUI is unreachable"))
        if status >= 300 or not data.get("access_token"):
            raise RuntimeError(f"free5GC login failed: HTTP {status} {data}")
        return data["access_token"]

    def login(self, timeout: float | None = None) -> str:
        if self._cached_token and time.time() - self._token_at < self._token_ttl:
            return self._cached_token
        token = self._do_login(timeout)
        self._cached_token = token
        self._token_at = time.time()
        return token

    def _invalidate_token(self) -> None:
        self._cached_token = None
        self._token_at = 0.0

    def request(self, method: str, path: str, token: str, body: Any = None, timeout: float | None = None) -> tuple[int, Any]:
        return self.http.request(method, f"{self.webui_url}{path}", body, {"Token": token}, timeout=timeout)

    def request_with_reauth(self, method: str, path: str, body: Any = None, timeout: float | None = None) -> tuple[int, Any]:
        token = self.login(timeout)
        status, data = self.request(method, path, token, body, timeout)
        if status == 401:
            self._invalidate_token()
            token = self.login(timeout)
            status, data = self.request(method, path, token, body, timeout)
        return status, data

    def upsert_request(self, method: str, path: str, token: str, body: Any) -> tuple[int, Any]:
        attempts = max(1, self.upsert_retries)
        retry_statuses = {429, 500, 502, 503, 504}
        status = 0
        data: Any = {}
        for attempt in range(1, attempts + 1):
            status, data = self.request(method, path, token, body)
            if status == 401:
                self._invalidate_token()
                token = self.login()
                status, data = self.request(method, path, token, body)
            if status not in retry_statuses or attempt == attempts:
                return status, data
            time.sleep(min(2 ** (attempt - 1), 5))
        return status, data

    def list_subscribers(self) -> list[dict[str, Any]]:
        status, data = self.request_with_reauth("GET", "/api/subscriber")
        if status >= 300:
            raise RuntimeError(f"free5GC subscriber list failed: HTTP {status} {data}")
        return data if isinstance(data, list) else []

    def registered_ues(self) -> list[dict[str, Any]]:
        status, data = self.request_with_reauth("GET", "/api/registered-ue-context")
        return data if status < 300 and isinstance(data, list) else []

    def profiles(self) -> list[Any]:
        status, data = self.request_with_reauth("GET", "/api/profile")
        return data if status < 300 and isinstance(data, list) else []

    def status_payload(
        self,
        metrics: dict[str, Any] | None = None,
        slices: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        metrics = metrics or self.metrics.unavailable_metrics()
        slices = slices or SliceCatalog.default_slices()
        if not self.webui_url:
            return {
                "connected": False,
                "source": "free5GC WebUI API",
                "error": "FREE5GC_WEBUI_URL is not configured",
                "subscribers": [],
                "eventSubscribers": [],
                "metrics": metrics,
                "slices": slices,
                "checkedAt": TimeUtils.now(),
            }

        try:
            status, subscriber_data = self.request_with_reauth("GET", "/api/subscriber", timeout=self.status_timeout)
            if status >= 300:
                raise RuntimeError(f"free5GC subscriber list failed: HTTP {status} {subscriber_data}")
            subscribers = subscriber_data if isinstance(subscriber_data, list) else []
            status, registered_data = self.request_with_reauth("GET", "/api/registered-ue-context", timeout=self.status_timeout)
            status_warnings: list[str] = []
            registered_ues = []
            if status < 300 and isinstance(registered_data, list):
                registered_ues = registered_data
            elif status != 404:
                status_warnings.append(f"registered UE query degraded: HTTP {status} {registered_data}")
            status, profile_data = self.request_with_reauth("GET", "/api/profile", timeout=self.status_timeout)
            profiles = []
            if status < 300 and isinstance(profile_data, list):
                profiles = profile_data
            elif status != 404:
                status_warnings.append(f"profile query degraded: HTTP {status} {profile_data}")
            event_subscribers = [s for s in subscribers if self.is_cityverse_subscriber(s)]
            if metrics.get("dataSource") == "unavailable":
                metrics = self.metrics.metrics_from_free5gc(registered_ues)
            if not any((item.get("sessions") or item.get("load")) for item in slices):
                slices = SliceCatalog.slices_from_registered_ues(registered_ues)
            source = "Prometheus + free5GC WebUI API" if metrics.get("dataSource") == "prometheus" else "free5GC WebUI API"
            return {
                "connected": True,
                "source": source,
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
                "warning": "; ".join(status_warnings) if status_warnings else "",
                "checkedAt": TimeUtils.now(),
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
                "metrics": metrics,
                "slices": slices,
                "checkedAt": TimeUtils.now(),
            }

    def upsert_subscribers(self, event_type: str, cfg: EventConfig, execution_id: str, limit: int) -> dict[str, Any]:
        if len(cfg.ue_ids) > limit:
            profile_result: dict[str, Any] = {"status": "skipped", "reason": "FREE5GC_WEBUI_URL is not configured"}
            subscriber_results: list[dict[str, Any]] = []
            runtime_count = min(len(cfg.ue_ids), max(limit, 12 if event_type == "iot_surge" else limit))
            if self.webui_url:
                try:
                    token = self.login()
                    profile_result = self.upsert_profile(token, event_type, cfg)
                    subscriber_results = [
                        self.upsert_subscriber_with_token(token, event_type, cfg, execution_id, ue_id)
                        for ue_id in cfg.ue_ids[:runtime_count]
                    ]
                except Exception as exc:
                    profile_result = {"status": "error", "error": str(exc)}
            profile_ok = profile_result.get("httpStatus", 500) < 300
            subscriber_errors = [item for item in subscriber_results if item.get("status") != "success"]
            subscribers_ok = bool(subscriber_results) and not subscriber_errors
            transient_profile_error = (
                not profile_ok
                and isinstance(profile_result.get("error"), str)
                and "unreachable" in profile_result.get("error", "").lower()
            )
            profile_refresh_degraded = not profile_ok and (
                subscribers_ok
                or profile_result.get("httpStatus", 0) >= 500
                or profile_result.get("status") == "error"
            )
            status_ok = profile_ok and subscribers_ok
            can_continue = status_ok or transient_profile_error or profile_refresh_degraded
            return {
                "status": "success" if can_continue else "error",
                "operation": "preseeded",
                "httpStatus": 200 if status_ok else 202 if can_continue else profile_result.get("httpStatus", 500),
                "ueCount": len(cfg.ue_ids),
                "runtimeUpsertCount": len(subscriber_results),
                "successCount": len(subscriber_results) if subscribers_ok else len(cfg.ue_ids) if transient_profile_error else len(subscriber_results) - len(subscriber_errors),
                "errorCount": 0 if can_continue else max(len(subscriber_errors), len(cfg.ue_ids) - len(subscriber_results)),
                "ueIds": cfg.ue_ids,
                "runtimeUeIds": cfg.ue_ids[:runtime_count],
                "eventType": event_type,
                "executionId": execution_id,
                "profile": profile_result,
                "subscriberResults": subscriber_results,
                "reason": "Subscribers are pre-seeded during deploy/start to keep frontend-triggered activation under API Gateway timeout.",
                "warning": "free5GC WebUI profile refresh failed; continuing with subscriber/runtime activation and real UE verification" if profile_refresh_degraded else "free5GC WebUI profile refresh timed out; continuing with pre-seeded profile and real UE verification" if transient_profile_error else "",
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

        try:
            token = self.login()
            result = self.upsert_subscriber_with_token(token, event_type, cfg, execution_id, ue_id)
            profile = self.upsert_profile(token, event_type, cfg)
            result["profile"] = profile
            if result.get("status") == "success" and profile.get("httpStatus", 500) >= 500:
                result["httpStatus"] = 202
                result["warning"] = "free5GC WebUI profile refresh failed; subscriber upsert succeeded and runtime verification will use live UE metrics"
            return result
        except Exception as exc:
            return {
                "status": "error",
                "ueId": ue_id,
                "plmnID": self.plmn_id,
                "eventType": event_type,
                "executionId": execution_id,
                "error": str(exc),
            }

    def upsert_subscriber_with_token(
        self,
        token: str,
        event_type: str,
        cfg: EventConfig,
        execution_id: str,
        ue_id: str,
    ) -> dict[str, Any]:
        msisdn = f"msisdn-{ue_id[-10:]}"
        payload = self.build_subscriber(event_type, cfg, ue_id, msisdn)
        path = f"/api/subscriber/{ue_id}/{self.plmn_id}"

        status, body = self.upsert_request("POST", path, token, payload)
        operation = "created"
        if status >= 400:
            status, body = self.upsert_request("PUT", path, token, payload)
            operation = "updated"
        return {
            "status": "success" if status < 300 else "error",
            "operation": operation,
            "httpStatus": status,
            "ueId": ue_id,
            "plmnID": self.plmn_id,
            "eventType": event_type,
            "executionId": execution_id,
            "response": body,
        }

    def upsert_profile(self, token: str, event_type: str, cfg: EventConfig) -> dict[str, Any]:
        profile_name = f"5GCityVerse-{event_type}"
        payload = self.build_profile(profile_name, event_type, cfg)
        status, body = self.request_with_reauth("POST", "/api/profile", payload)
        operation = "created"
        if status == 409:
            status, body = self.request_with_reauth("PUT", f"/api/profile/{profile_name}", payload)
            operation = "updated"
        return {"name": profile_name, "operation": operation, "httpStatus": status, "response": body}

    def reset_subscribers(self) -> dict[str, Any]:
        if not self.webui_url:
            return {"status": "skipped", "reason": "FREE5GC_WEBUI_URL is not configured"}

        try:
            subscribers = self.list_subscribers()
            deleted = []
            errors = []
            for subscriber in subscribers:
                ue_id = subscriber.get("ueId", "")
                plmn_id = subscriber.get("plmnID", self.plmn_id)
                if not self.is_cityverse_ue_id(ue_id):
                    continue
                status, data = self.request_with_reauth("DELETE", f"/api/subscriber/{ue_id}/{plmn_id}")
                if status < 300:
                    deleted.append(ue_id)
                else:
                    errors.append({"ueId": ue_id, "httpStatus": status, "response": data})
            for event_type in EVENT_CONFIG:
                profile_name = f"5GCityVerse-{event_type}"
                status, data = self.request_with_reauth("DELETE", f"/api/profile/{profile_name}")
                if status not in (200, 204, 404):
                    errors.append({"profile": profile_name, "httpStatus": status, "response": data})
            return {"status": "success" if not errors else "partial", "deleted": deleted, "errors": errors}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def current_slices(
        self,
        metrics: dict[str, Any] | None = None,
        registered_ues: list[dict[str, Any]] | None = None,
        query_prometheus: bool = True,
    ) -> list[dict[str, Any]]:
        real_slices = self.metrics.real_slice_metrics() if query_prometheus else None
        if real_slices:
            return real_slices
        if registered_ues is not None:
            return SliceCatalog.slices_from_registered_ues(registered_ues)
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
        priority = 1 if cfg.slice_type == "URLLC" else 15 if cfg.slice_type == "mMTC" else 8
        preempt_cap = "MAY_PREEMPT" if cfg.slice_type == "URLLC" else "NOT_PREEMPT"
        preempt_vuln = "PREEMPTABLE" if cfg.slice_type == "mMTC" else "NOT_PREEMPTABLE"
        dnn_config = {
            "sscModes": {"defaultSscMode": "SSC_MODE_1", "allowedSscModes": ["SSC_MODE_2", "SSC_MODE_3"]},
            "pduSessionTypes": {"defaultSessionType": "IPV4", "allowedSessionTypes": ["IPV4"]},
            "sessionAmbr": {"uplink": self.bandwidth_value(cfg.session_ambr, "uplink"), "downlink": self.bandwidth_value(cfg.session_ambr, "downlink")},
            "5gQosProfile": {
                "5qi": cfg.five_qi,
                "arp": {"priorityLevel": priority, "preemptCap": preempt_cap, "preemptVuln": preempt_vuln},
                "priorityLevel": priority,
            },
            "staticIpAddress": [],
        }
        if self.bandwidth_value(cfg.gbr, "uplink") and self.bandwidth_value(cfg.gbr, "downlink"):
            dnn_config["gbrQosFlowInfo"] = {
                "maxFbrUplink": self.bandwidth_value(cfg.mbr, "uplink"),
                "maxFbrDownlink": self.bandwidth_value(cfg.mbr, "downlink"),
                "guaranteedFbrUplink": self.bandwidth_value(cfg.gbr, "uplink"),
                "guaranteedFbrDownlink": self.bandwidth_value(cfg.gbr, "downlink"),
            }

        return {
            "plmnID": self.plmn_id,
            "ueId": ue_id,
            "AuthenticationSubscription": {
                "authenticationManagementField": "8000",
                "authenticationMethod": "5G_AKA",
                "milenage": {"op": {"opValue": "8e27b6af0e692e750f32667a3b14605d"}},
                "opc": {"opcValue": "8e27b6af0e692e750f32667a3b14605d"},
                "permanentKey": {"permanentKeyValue": "8baf473f2f8fd09487cccbd7097c6862"},
                "sequenceNumber": "000000000023",
            },
            "AccessAndMobilitySubscriptionData": {
                "gpsis": [msisdn],
                "nssai": {
                    "defaultSingleNssais": [{"sst": sst, "sd": sd, "isDefault": True}],
                    "singleNssais": [{"sst": sst, "sd": sd, "isDefault": True}],
                },
                "subscribedUeAmbr": {"downlink": self.bandwidth_value(cfg.ue_ambr, "downlink"), "uplink": self.bandwidth_value(cfg.ue_ambr, "uplink")},
            },
            "SessionManagementSubscriptionData": [{"singleNssai": {"sst": sst, "sd": sd}, "dnnConfigurations": {cfg.dnn: dnn_config}}],
            "SmfSelectionSubscriptionData": {"subscribedSnssaiInfos": {snssai_key: {"dnnInfos": [{"dnn": cfg.dnn}]}}},
            "AmPolicyData": {"subscCats": ["5GCityVerse", event_type, cfg.slice_type]},
            "SmPolicyData": {
                "smPolicySnssaiData": {
                    snssai_key: {"snssai": {"sst": sst, "sd": sd}, "smPolicyDnnData": {cfg.dnn: {"dnn": cfg.dnn}}}
                }
            },
        }

    @staticmethod
    def bandwidth_value(value: Any, field: str) -> str:
        if isinstance(value, dict):
            return str(value.get(field) or "")
        return str(getattr(value, field, "") or "")

    def build_profile(self, profile_name: str, event_type: str, cfg: EventConfig) -> dict[str, Any]:
        subscriber = self.build_subscriber(event_type, cfg, "imsi-000000000000000", "msisdn-0000000000")
        return {
            "profileName": profile_name,
            "AccessAndMobilitySubscriptionData": subscriber["AccessAndMobilitySubscriptionData"],
            "SessionManagementSubscriptionData": subscriber["SessionManagementSubscriptionData"],
            "SmfSelectionSubscriptionData": subscriber["SmfSelectionSubscriptionData"],
            "AmPolicyData": subscriber["AmPolicyData"],
            "SmPolicyData": subscriber["SmPolicyData"],
        }
