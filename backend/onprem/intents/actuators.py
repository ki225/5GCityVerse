"""Typed resource commands. Native installation and service effect are separate facts."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import math
import time
from typing import Any, Mapping, Protocol


ACTIONS = ("update_qos", "resize_nf", "add_upf", "assign_sessions",
           "provision_node", "place_nf", "drain_and_remove", "place_application", "remove_application",
           "bind_application_endpoint", "drain_application_endpoint", "reestablish_sessions",
           "start_service_flows", "stop_service_flows", "drain_application")


class ActuationBlocked(RuntimeError):
    """A configuration, identity, ownership or resource prerequisite failed."""


@dataclass(frozen=True)
class Capability:
    action: str
    status: str
    reason: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ActionRequest:
    action: str
    target_id: str
    owner_id: str
    operation_id: str
    expected_version: str
    parameters: dict[str, Any] = field(default_factory=dict)
    session_keys: tuple[str, ...] = ()

    def __post_init__(self):
        if self.action not in ACTIONS:
            raise ValueError("Unknown resource action")
        if any(not isinstance(v, str) or not v or len(v) > 1024 for v in
               (self.target_id, self.owner_id, self.operation_id, self.expected_version)):
            raise ValueError("Resource command requires exact target, owner, operation and version")
        if not isinstance(self.parameters, dict) or len(set(self.session_keys)) != len(self.session_keys):
            raise ValueError("Malformed resource command")
        if any(not isinstance(k, str) or not k for k in self.session_keys):
            raise ValueError("Invalid session key")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**{**value, "session_keys": tuple(value.get("session_keys", ()))})


@dataclass
class ActionReceipt:
    request: ActionRequest
    state: str
    before: dict = field(default_factory=dict)
    installed: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    started_at: float = 0
    verified_at: float | None = None
    installation_confirmed_at: float | None = None

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**{**value, "request": ActionRequest.from_dict(value["request"])})


class ResourceProvider(Protocol):
    """Providers own bounded native IO, exact selectors and compare-and-swap."""
    def capabilities(self) -> tuple[Capability, ...]: ...
    def inspect(self, request: ActionRequest) -> dict: ...
    def preflight(self, request: ActionRequest, before: dict) -> dict: ...
    def mutate(self, request: ActionRequest, before: dict) -> dict: ...
    def observe(self, request: ActionRequest, mutation: dict) -> dict: ...
    def confirmed(self, request: ActionRequest, observed: dict) -> bool: ...
    def restore(self, request: ActionRequest, before: dict, installed: dict) -> dict: ...


class ResourceActuator:
    """Command adapter; the operation journal is persisted by the runtime before apply."""
    def __init__(self, action: str, provider: ResourceProvider | None, *, clock=time.time):
        self.action, self.provider, self.clock = action, provider, clock

    def capabilities(self):
        if self.provider is None:
            return (Capability(self.action, "not_configured", "Native provider is not configured"),)
        try:
            values = tuple(c for c in self.provider.capabilities() if c.action == self.action)
            return values or (Capability(self.action, "not_configured", "No commissioned capability"),)
        except Exception:
            return (Capability(self.action, "not_configured", "Native capability proof is unavailable"),)

    def _provider(self, request):
        if request.action != self.action:
            raise ActuationBlocked("Resource action does not match actuator")
        if not any(c.status == "configured" for c in self.capabilities()):
            raise ActuationBlocked("Native capability is not configured")
        return self.provider

    def preflight(self, request: ActionRequest):
        provider = self._provider(request)
        before = provider.inspect(request)
        if str(before.get("version")) != request.expected_version:
            raise ActuationBlocked("Resource version changed; replan required")
        return {"before": deepcopy(before), "checks": provider.preflight(request, before)}

    def apply(self, request: ActionRequest):
        checked = self.preflight(request)
        receipt = ActionReceipt(request, "applying", checked["before"],
                                evidence={"preflight": checked["checks"]}, started_at=self.clock())
        try:
            mutation = self.provider.mutate(request, receipt.before)
            receipt.evidence["mutation"] = deepcopy(mutation)
            return self.readback(receipt)
        except Exception:
            # Unknown native outcomes must survive as a recoverable operation, not a retryable success.
            receipt.state = "recovery_required"
            receipt.evidence["failure"] = "Native action or readback did not complete"
            return receipt

    def readback(self, receipt: ActionReceipt):
        provider = self._provider(receipt.request)
        actual = provider.observe(receipt.request, receipt.evidence.get("mutation", {}))
        receipt.installed = deepcopy(actual)
        preserved = getattr(provider, "identity_preserved", lambda before, after: True)(receipt.before, actual)
        receipt.state = ("installation_confirmed" if preserved and provider.confirmed(receipt.request, actual)
                         else "recovery_required")
        if receipt.state == "installation_confirmed" and receipt.installation_confirmed_at is None:
            receipt.installation_confirmed_at = self.clock()
        return receipt

    def verify(self, receipt: ActionReceipt, effect: Mapping):
        self.readback(receipt)
        request = receipt.request
        stamp = effect.get("observed_at")
        window_started = effect.get("window_started_at")
        installed = receipt.installation_confirmed_at
        expected_keys = request.session_keys
        flow_window_verified = True
        if request.action == 'start_service_flows':
            # This action performs an entire bounded measurement window. Its
            # completion is later than the evidence it produces. Preserve the
            # dispatch cutoff and independently join the native window receipt.
            installed = receipt.started_at
            verifier = getattr(self.provider, 'verify_effect_window', None)
            flow_window_verified = callable(verifier) and verifier(receipt, effect) is True
            # Protected-service windows may begin before this measurement
            # dispatch. The hook fences each owned native flow individually.
            if flow_window_verified and type(window_started) in (int, float):
                window_started = max(window_started, receipt.started_at)
        if request.action == "reestablish_sessions" or request.action == "assign_sessions" and request.parameters.get("mode") == "new_sessions":
            resolver = getattr(self.provider, "resolve_effect_session_keys", None)
            expected_keys = resolver(receipt) if callable(resolver) else None
        valid = (receipt.state == "installation_confirmed"
                 and flow_window_verified
                 and effect.get("operation_id") == request.operation_id
                 and effect.get("owner_id") == request.owner_id
                 and expected_keys is not None
                 and (bool(expected_keys) or request.action not in {"assign_sessions", "reestablish_sessions"})
                 and set(effect.get("session_keys", ())) == set(expected_keys)
                 and bool(effect.get("source")) and bool(effect.get("observation_id"))
                 and type(stamp) in (int, float) and math.isfinite(stamp)
                 and type(window_started) in (int, float) and math.isfinite(window_started)
                 and type(installed) in (int, float) and math.isfinite(installed)
                 and receipt.started_at <= installed <= window_started < stamp <= self.clock()
                 and 0 <= self.clock() - stamp <= 60
                 and effect.get("met") is True and effect.get("protected_services_met") is True)
        receipt.evidence["effect"] = deepcopy(dict(effect))
        if valid:
            receipt.state, receipt.verified_at = "effect_verified", self.clock()
        return receipt

    def prepare_drain(self, receipt: ActionReceipt):
        """Close future admission before owned UE drain, without deleting live NFs."""
        provider = self._provider(receipt.request)
        prepare = getattr(provider, 'prepare_drain', None)
        if callable(prepare):
            result = prepare(receipt.request, receipt.before, receipt.installed)
            if not isinstance(result, dict) or result.get('admissions_closed') is not True:
                raise ActuationBlocked('Native admission quiescence is not confirmed')
            receipt.evidence['drain_preparation'] = deepcopy(result)
            return result
        if receipt.request.action in {'add_upf','assign_sessions','place_nf','drain_and_remove'}:
            raise ActuationBlocked('Native admission quiescence is unavailable')
        return {'admissions_closed': True, 'source': 'resource-without-session-admission'}

    def compensate(self, receipt: ActionReceipt):
        provider = self._provider(receipt.request)
        receipt.evidence.setdefault("compensation_started_at", self.clock())
        try:
            restored = provider.restore(receipt.request, receipt.before, receipt.installed)
            receipt.evidence["restoration"] = deepcopy(restored)
            receipt.state = ("compensated" if restored.get("restored") is True else
                             "compensating" if restored.get("pending") is True else "recovery_required")
        except Exception:
            receipt.state = "recovery_required"
            receipt.evidence["restoration"] = {"restored": False,
                "reason": "Native ownership or restoration could not be verified"}
        return receipt

    def compensation_readback(self, receipt: ActionReceipt):
        """Read an already dispatched compensation; never dispatch native writes here."""
        provider = self._provider(receipt.request)
        read = getattr(provider, "restoration_state", None)
        if not callable(read):
            receipt.state = "recovery_required"
            return receipt
        try:
            restored = read(receipt.request, receipt.before, receipt.installed)
            receipt.evidence["restoration"] = deepcopy(restored)
            receipt.state = ("compensated" if restored.get("restored") is True else
                             "compensating" if restored.get("pending") is True else "recovery_required")
        except Exception:
            # Native observations can be temporarily unavailable within the cleanup deadline.
            receipt.state = "compensating"
            receipt.evidence["restoration"] = {"restored": False, "pending": True,
                                               "reason": "Native compensation readback unavailable"}
        return receipt

    def release(self, receipt: ActionReceipt):
        self.compensate(receipt)
        if receipt.state == "compensated":
            receipt.state = "released"
        return receipt


class ActuatorRegistry:
    def __init__(self, adapters=()):
        self._adapters = {a.action: a for a in adapters}

    def get(self, action):
        value = getattr(action, "value", action)
        if value not in self._adapters:
            raise ActuationBlocked("Unknown resource actuator")
        return self._adapters[value]

    def capabilities(self):
        return tuple(c for adapter in self._adapters.values() for c in adapter.capabilities())

    def preflight(self, request):
        return self.get(request.action).preflight(request)

    def apply(self, request):
        return self.get(request.action).apply(request)

    def readback(self, receipt):
        return self.get(receipt.request.action).readback(receipt)

    def verify(self, receipt, effect):
        return self.get(receipt.request.action).verify(receipt, effect)

    def compensate(self, receipt):
        return self.get(receipt.request.action).compensate(receipt)

    def prepare_drain(self, receipt):
        return self.get(receipt.request.action).prepare_drain(receipt)

    def compensation_readback(self, receipt):
        return self.get(receipt.request.action).compensation_readback(receipt)

    def release(self, receipt):
        return self.get(receipt.request.action).release(receipt)


def build_registry(providers=None, *, clock=time.time):
    providers = providers or {}
    names = {"update_qos": "qos", "resize_nf": "kubernetes", "place_nf": "upf",
             "add_upf": "upf", "assign_sessions": "upf", "drain_and_remove": "upf",
             "provision_node": "nodes", "place_application": "applications", "remove_application": "applications", "drain_application": "applications",
             "bind_application_endpoint": "application_endpoints", "drain_application_endpoint": "application_endpoints",
             "reestablish_sessions": "session_routes", "start_service_flows": "service_flows", "stop_service_flows": "service_flows"}
    return ActuatorRegistry(ResourceActuator(action, providers.get(names[action]), clock=clock)
                            for action in ACTIONS)
