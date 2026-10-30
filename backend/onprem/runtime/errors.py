"""Shared runtime errors without importing any deployment implementation."""
from typing import Any
import math


REQUEST_BLOCKERS = frozenset({'ue.generation_not_ready', 'ue.retirement_quarantine', 'ue.request_generation_changed'})


class RequestGuardDiagnostic:
    """Fixed status codes and bounded numbers; never exception text or locals."""
    source_codes = frozenset({'observation_expired', 'reconciliation_pending', 'ready',
        'generation_changed', 'native_status_unavailable'})
    outcomes = frozenset({'refreshed', 'not_ready', 'generation_changed', 'deadline_exhausted',
        'cancelled', 'native_error'})

    @classmethod
    def project(cls, value):
        if not isinstance(value, dict):
            return None
        result = {}
        if isinstance(value.get('sourceStatusCode'), str) and value['sourceStatusCode'] in cls.source_codes:
            result['sourceStatusCode'] = value['sourceStatusCode']
        if isinstance(value.get('refreshOutcome'), str) and value['refreshOutcome'] in cls.outcomes:
            result['refreshOutcome'] = value['refreshOutcome']
        for name, maximum in [('generation', 2**53), ('refreshAttempts', 2)]:
            number = value.get(name)
            if type(number) is int and 0 <= number <= maximum:
                result[name] = number
        for name, low, high in [('observationAgeSeconds', -10, 315576000), ('refreshDurationMs', 0, 3600000)]:
            number = value.get(name)
            if type(number) in (int, float) and low <= number <= high and math.isfinite(number):
                result[name] = number
        return result or None

class EvidenceUnavailable(RuntimeError):
    pass

class LocalKubernetesError(RuntimeError):
    pass

class ScenarioRuntimeError(LocalKubernetesError):
    def __init__(self, message: str, evidence: dict[str, Any]):
        super().__init__(message)
        self.evidence = evidence

class RoundRuntimeBlocked(RuntimeError):
    pass

class NativeWindowFailure(RoundRuntimeBlocked):
    """Durable, fixed-code diagnostics for one owned receiver window."""
    def __init__(self, phase, reason_code, diagnostic=None):
        super().__init__("Native receiver window failed")
        self.phase, self.reason_code = phase, reason_code
        self.diagnostic = diagnostic

class ReleaseProofBlocked(RoundRuntimeBlocked):
    """Only locally assigned fixed codes, never remote exception text."""
    def __init__(self, code, proof_age=None):
        super().__init__("Native release proof blocked: " + code)
        self.code, self.proof_age = code, proof_age

