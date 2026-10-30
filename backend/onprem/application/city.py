"""City event and observation application service; external work uses adapters."""
from __future__ import annotations
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import time
from typing import Any
from uuid import uuid4
from starlette.websockets import WebSocket, WebSocketDisconnect
from backend.onprem.settings import PROJECT_ROOT
from backend.onprem.application.views import utc_now, epoch_millis, unavailable_metrics, configured_slices, unavailable_free5gc_status, public_record
from backend.onprem.storage.executions import ExecutionRepository
from backend.onprem.integrations.openai.openai_client import ALLOWED_MODEL, OpenAIClient, OpenAIError
from backend.onprem.runtime.errors import LocalKubernetesError, ScenarioRuntimeError

class CityService:
    def __init__(self, state: ExecutionRepository, runtime: Any | None = None, *, ue_controller=None, daily_observer: Any | None = None,
                 daily_observation_ttl: float = 5.0, clock=time.time, monotonic=time.monotonic) -> None:
        self.state = state
        self.ue_controller = ue_controller
        enabled = os.environ.get("CITYVERSE_LOCAL_KUBERNETES_ENABLED", "false").lower() in {"1", "true", "yes"}
        self.kubernetes = runtime
        if self.kubernetes is None and os.environ.get("CITYVERSE_ONPREM_RUNTIME", "legacy") != "kubeadm":
            from archive.legacy.runtime.local_kubernetes import LocalKubernetesRuntime
            self.kubernetes = LocalKubernetesRuntime(PROJECT_ROOT, enabled=enabled)
        self.websockets: dict[WebSocket, str] = {}
        self.tasks: set[asyncio.Task[Any]] = set()
        self.traffic_rendered: dict[str, asyncio.Event] = {}
        self.resets: dict[str, dict[str, Any]] = {}
        self.daily_observer = daily_observer
        self.daily_observation_ttl = max(0.0, float(daily_observation_ttl))
        self._clock = clock
        self._monotonic = monotonic
        self._daily_observation_cache: dict[str, Any] | None = None
        self._daily_observation_cached_at = float("-inf")
        self._daily_observation_lock = asyncio.Lock()
        self._ai_verified_status: str | None = None

    def ai_runtime_status(self) -> dict[str, Any]:
        configured = bool(os.environ.get("OPENAI_API_KEY"))
        status = self._ai_verified_status or ("unverified" if configured else "not_configured")
        reason = {
            "unverified": "Credential is present but has not been validated by a successful API response",
            "authentication_failed": "OpenAI rejected the configured credential",
            "unavailable": "OpenAI request failed",
        }.get(status)
        return {"provider": "openai", "model": ALLOWED_MODEL,
                "configured": configured, "verified": status == "available", "status": status,
                **({"reason": reason} if reason else {})}

    @staticmethod
    def _daily_snapshot_id(observation: dict[str, Any]) -> str:
        canonical = json.dumps(observation, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return "daily-native-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def daily_native_observation(self, *, force: bool = False) -> dict[str, Any]:
        """Return one completed immutable observation; concurrent UI polls share one native read."""
        async with self._daily_observation_lock:
            now = self._monotonic()
            cached = self._daily_observation_cache
            if (not force and cached is not None
                    and now - self._daily_observation_cached_at <= self.daily_observation_ttl):
                return await asyncio.to_thread(deepcopy, cached)
            if self.daily_observer is None:
                observed = {
                    "schemaVersion": "cityverse.daily-native-observation/v1",
                    "available": False,
                    "reason": "daily native observation is not configured",
                    "observedAt": None,
                    "source": "unavailable",
                    "evidenceTrust": "lower-trust-native",
                    "criticalOperationEligible": False,
                    "strongBinding": False,
                    "independentNgapProvenance": False,
                    "mutationsPerformed": False,
                    "podIncarnations": {}, "ues": [], "slices": {},
                }
            else:
                observed = await asyncio.to_thread(self.daily_observer.read)
                if not isinstance(observed, dict):
                    raise LocalKubernetesError("Daily native observer returned an invalid result")
            def seal():
                value = deepcopy(observed)
                value["snapshotId"] = self._daily_snapshot_id(value)
                return value, deepcopy(value)
            observed, self._daily_observation_cache = await asyncio.to_thread(seal)
            self._daily_observation_cached_at = self._monotonic()
            return observed

    def daily_ai_summary(self, observation: dict[str, Any], *, require_fresh: bool = True) -> dict[str, Any]:
        """Bounded aggregate view; AI callers additionally require point-fresh evidence."""
        if observation.get("available") is not True:
            raise LocalKubernetesError("Daily native observation is unavailable")
        try:
            observed_at = str(observation["observedAt"])
            age = self._clock() - datetime.fromisoformat(observed_at.replace("Z", "+00:00")).timestamp()
        except (KeyError, TypeError, ValueError):
            raise LocalKubernetesError("Daily native observation timestamp is unavailable") from None
        if require_fresh and not -1 <= age <= 15:
            raise LocalKubernetesError("Daily native observation is stale or future-dated")
        slices = observation.get("slices")
        if not isinstance(slices, dict):
            raise LocalKubernetesError("Daily native slice summary is unavailable")
        generation = observation.get("generation")
        if type(generation) is not int or generation < 0:
            raise LocalKubernetesError("Daily native generation is unavailable")
        bounded_slices = {}
        for name in ("embb", "urllc", "mmtc", "v2x"):
            value = slices.get(name)
            if not isinstance(value, dict):
                raise LocalKubernetesError("Daily native slice summary is incomplete")
            bounded_slices[name] = {
                "ueCount": value.get("ueCount", value.get("registered", 0)),
                "upfSessionCount": (value.get("upf") or {}).get("sessionCount", value.get("pduEstablished", 0)),
                "gnbSctpReciprocal": (value.get("gnb") or {}).get("sctpReciprocal"),
                "n6Observed": bool(value.get("n6")) if "n6" in value else value.get("n6Verified"),
                "available": value.get("available", True),
                "reason": value.get("reason"),
            }
        return {
            "snapshotId": observation["snapshotId"], "observedAt": observed_at,
            "generation": generation,
            "source": observation.get("source"), "availability": "available",
            "evidenceTrust": observation.get("evidenceTrust"),
            "criticalOperationEligible": False, "strongBinding": False,
            "ueCount": len(observation.get("ues", [])), "slices": bounded_slices,
        }

    def public_daily_observation(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Expose aggregate evidence state without leaking up to 500 subscriber identities."""
        result = {key: observation.get(key) for key in (
            "schemaVersion", "snapshotId", "available", "reason", "observedAt", "generation", "source", "evidenceTrust",
            "criticalOperationEligible", "strongBinding", "independentNgapProvenance", "mutationsPerformed",
            "diagnostic",
        )}
        result["ueCount"] = len(observation.get("ues", []))
        result["ues"] = []
        result["detailsRedacted"] = True
        if observation.get("available") is True:
            # GET is an evidence view, not an AI authorization. Preserve the
            # observer's truthful availability and timestamp even when a
            # bounded multi-source read makes its earliest source older than
            # the stricter AI point-freshness gate by response time.
            result["slices"] = self.daily_ai_summary(observation, require_fresh=False)["slices"]
        else:
            result["slices"] = {}
        return result

    @staticmethod
    def validate_daily_trend_grounding(summary: dict[str, Any], generated: str) -> None:
        """Reject traffic claims that the aggregate AI input cannot substantiate."""
        # The daily summary carries UE/session counts and N6 path availability,
        # but no measured UE packet or receiver counters. A model may describe
        # those counts; it cannot turn the N6 path flag into traffic evidence.
        unsupported = re.compile(
            r"\btraffic\s+(?:(?:is|was|has\s+been)\s+)?"
            r"(?:observed|detected|active|flowing|present|healthy|normal)\b"
            r"|\b(?:observed|detected|active|flowing)\s+(?:\w+\s+){0,3}traffic\b",
            re.IGNORECASE,
        )
        negated = re.compile(r"\b(?:no|not|without|zero)\b(?:\W+\w+){0,4}\W+traffic\b$", re.IGNORECASE)
        denied_after = re.compile(
            r"\btraffic\s+(?:(?:is|was|has\s+been)\s+)?"
            r"(?:not|never)\s+(?:proven|verified|confirmed|measured|observed|established)\b",
            re.IGNORECASE,
        )
        for clause in re.split(r"[.!?;,]|\b(?:but|however|while|and)\b", generated, flags=re.IGNORECASE):
            for match in unsupported.finditer(clause):
                traffic = re.search(r"\btraffic\b", match.group(), flags=re.IGNORECASE)
                if traffic is None:
                    continue
                traffic_start = match.start() + traffic.start()
                traffic_end = match.start() + traffic.end()
                if negated.search(clause[:traffic_end]) or denied_after.match(clause[traffic_start:]):
                    continue
                raise OpenAIError("OpenAI trend made an unsupported active-traffic claim")

    @staticmethod
    def _audit_rejected_daily_trend(summary: dict[str, Any], generated: str) -> None:
        """Opt-in local diagnostic, confined to ignored .cache and aggregate facts."""
        if os.environ.get("CITYVERSE_PRIVATE_TREND_AUDIT") != "1":
            return
        audit_dir = (PROJECT_ROOT / ".cache" / "private-ai-trend-audit").resolve()
        try:
            audit_dir.mkdir(parents=True, exist_ok=True)
            # The model receives only aggregates. Still mask any invented UE
            # identity or address before keeping its rejected answer locally.
            sanitized = re.sub(r"(?i)\b(?:imsi-|supi-|msisdn-)?\d{10,16}\b", "[redacted-id]", generated[:2048])
            sanitized = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[redacted-ip]", sanitized)
            record = {
                "snapshotId": summary.get("snapshotId"),
                "ueCount": summary.get("ueCount"),
                "slices": {
                    name: {key: values.get(key) for key in ("ueCount", "upfSessionCount", "n6Observed")}
                    for name, values in summary.get("slices", {}).items() if isinstance(values, dict)
                },
                "generated": sanitized,
            }
            path = audit_dir / f"rejected-{uuid4().hex}.json"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False)
        except OSError:
            # Audit I/O must never turn a grounding rejection into success or
            # replace its original error.
            return

    async def daily_trend(self) -> dict[str, Any]:
        observation = await self.daily_native_observation(force=True)
        input_summary = self.daily_ai_summary(observation)
        try:
            client = OpenAIClient.from_env()
            # Trend text is deliberately brief: an unbounded model response can
            # outlive the 15-second native point-freshness publication gate.
            client.max_output_tokens = min(client.max_output_tokens, 512)
            client.reasoning_effort = "low"
            prompt = (
                "In at most 80 words, describe city-wide network trends and safe monitoring priorities from this completed native snapshot. "
                "Do not identify individual UEs, authorize UE-specific operations, or claim any mutation occurred. "
                "The n6Observed field indicates that the N6 observation path is present; it does not prove active UE traffic. "
                "Only claim UE traffic when measured UE packet or receiver counters are supplied; this summary contains no such counters. "
                "When ueCount and UPF session counts are zero, say no UE traffic was observed. Facts:\n"
                + json.dumps(input_summary, ensure_ascii=False, allow_nan=False)
            )
            generated = await asyncio.to_thread(client.generate_text, prompt, self.system_instruction())
        except OpenAIError as exc:
            self._ai_verified_status = "authentication_failed" if "HTTP 401" in str(exc) else "unavailable"
            raise
        self._ai_verified_status = "available"
        try:
            self.validate_daily_trend_grounding(input_summary, generated)
        except OpenAIError:
            self._audit_rejected_daily_trend(input_summary, generated)
            raise
        # Re-observe after model latency. Only publish against a fresh native
        # snapshot in the same generation with identical facts supplied to AI.
        current_observation = await self.daily_native_observation(force=True)
        summary = self.daily_ai_summary(current_observation)
        stable_keys = ("generation", "source", "availability", "evidenceTrust", "ueCount", "slices")
        if any(input_summary[key] != summary[key] for key in stable_keys):
            raise LocalKubernetesError("Daily native observation changed during AI generation")
        generated_at = datetime.fromtimestamp(self._clock(), timezone.utc).isoformat().replace("+00:00", "Z")
        return {"snapshotId": summary["snapshotId"], "observedAt": summary["observedAt"],
                "generation": summary["generation"],
                "source": summary["source"], "availability": summary["availability"],
                "evidenceTrust": summary["evidenceTrust"], "criticalOperationEligible": False,
                "strongBinding": False, "summary": summary, "trend": generated.strip(),
                "ai": {"provider": "openai", "model": client.model, "status": "success",
                       "generatedAt": generated_at, "inputSnapshotId": input_summary["snapshotId"]}}

    def runtime_status(self, *, observed=None) -> dict[str, Any]:
        if os.environ.get("CITYVERSE_ONPREM_RUNTIME", "legacy") == "kubeadm":
            observed = self.ue_controller.status() if observed is None else observed
            from backend.onprem.runtime.ue import ran_connected
            ran_ready = ran_connected(observed)
            ready = observed["status"] in {"established", "stopped"} and ran_ready
            return {"mode": "onprem", "runtimeSelector": "kubeadm", "status": "ready" if ready else "degraded",
                    "ai": self.ai_runtime_status(),
                    "kubernetes": {"configured": self.ue_controller.driver is not None or getattr(getattr(self.ue_controller, "adapter", None), "driver", None) is not None,
                                   "mutationsEnabled": self.ue_controller.driver is not None,
                                   "status": observed["status"], "context": os.environ.get("CITYVERSE_ONPREM_KUBE_CONTEXT"),
                                   "namespace": os.environ.get("CITYVERSE_ONPREM_NAMESPACE", "free5gc-onprem"),
                                   "detail": "; ".join(observed["errors"])},
                    "free5gc": {"configured": os.environ.get("CITYVERSE_ONPREM_EVIDENCE_MODE") == "native" or bool(os.environ.get("CITYVERSE_ONPREM_EVIDENCE_URL")), "status": "verification_unavailable" if not ready and observed["status"] in {"established", "stopped"} else observed["status"],
                                "detail": "; ".join(observed["errors"]) or ("" if ran_ready else "Current NGAP/SCTP peer proof unavailable"),
                                "ranHealth": observed.get('ranHealth')},
                    "ue": observed, "persistence": {"type": "sqlite", "path": str(self.ue_controller.store.path)}}
        snapshot = self.kubernetes.snapshot()
        kubernetes = snapshot["runtime"]
        free5gc = snapshot["free5gc"]
        return {
            "mode": "onprem",
            "status": "ready" if kubernetes.get("status") == "ready" and free5gc.get("connected") else "degraded",
            "ai": self.ai_runtime_status(),
            "kubernetes": kubernetes,
            "free5gc": {
                "configured": bool(kubernetes.get("configured")),
                "status": free5gc.get("status"),
                "detail": free5gc.get("error") or kubernetes.get("detail"),
            },
            "persistence": {"type": "json", "path": str(self.state.path)},
        }

    async def snapshot(self, *, force: bool = False, include_daily: bool = True) -> dict[str, Any]:
        if os.environ.get("CITYVERSE_ONPREM_RUNTIME", "legacy") == "kubeadm":
            status = await asyncio.to_thread(self.ue_controller.status)
            from backend.onprem.runtime.ue import ran_connected
            ran_ready = ran_connected(status)
            connected = status["status"] in {"established", "stopped"} and ran_ready
            metrics = {"registeredUeCount": status["registered"], "pduSessionCount": status["pduEstablished"],
                       "trafficVerified": status["trafficVerified"], "throughputMbps": None, "latencyMs": None,
                       "timestamp": epoch_millis(), "dataSource": "nf-evidence" if status["observedAt"] else "unavailable"}
            slice_values = [{"type": name, **item} for name, item in status["slices"].items()]
            free5gc = {"connected": connected, "status": "verification_unavailable" if not connected and status["status"] in {"established", "stopped"} else status["status"],
                       "registeredUes": [row for row in status["perUe"] if row["registered"]],
                       "metrics": metrics, "slices": slice_values, "checkedAt": status["observedAt"],
                       "source": status["evidenceSources"], "error": "; ".join(status["errors"]) or ("" if ran_ready else "Current NGAP/SCTP peer proof unavailable"),
                       "ranHealth": status.get('ranHealth'), "ue": status}
            result = {"runtime": self.runtime_status(observed=status)["kubernetes"], "free5gc": free5gc,
                      "metrics": metrics, "slices": slice_values}
            if include_daily:
                daily = await self.daily_native_observation(force=force)
                public_daily = self.public_daily_observation(daily)
                free5gc["dailyNativeObservation"] = public_daily
                result["dailyNativeObservation"] = public_daily
            return result
        return await asyncio.to_thread(self.kubernetes.snapshot, force=force)

    async def broadcast(self, message: dict[str, Any], session_id: str = "") -> None:
        stale: list[WebSocket] = []
        for socket, socket_session in list(self.websockets.items()):
            if session_id and socket_session and socket_session != session_id:
                continue
            try:
                await socket.send_json(message)
            except (RuntimeError, WebSocketDisconnect):
                stale.append(socket)
        for socket in stale:
            self.websockets.pop(socket, None)

    @staticmethod
    def system_instruction() -> str:
        return (
            "You are a careful 5G network slicing planner. Write in Traditional Chinese (Taiwan). "
            "The supplied runtime facts are authoritative observations from a local Kubernetes/free5GC lab. "
            "Describe verified bearer traffic as observed, but never claim a QER or policy was applied. "
            "Label network policy changes as proposed. Keep the answer under 180 words. "
            "Return plain text, not JSON or Markdown."
        )

