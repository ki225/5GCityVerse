"""Decode only ownership metadata; native evidence uses its original decoder."""
from __future__ import annotations

import json
import sys
from typing import Any

try:
    import msgspec
except ImportError:
    msgspec = None


if msgspec is not None:
    class _Ue(msgspec.Struct):
        supi: Any = msgspec.UNSET
        scenario: Any = msgspec.UNSET
        slice: Any = msgspec.UNSET
        podUid: Any = msgspec.UNSET

    class _Action(msgspec.Struct):
        id: Any = msgspec.UNSET
        kind: Any = msgspec.UNSET
        mode: Any = msgspec.UNSET
        token: Any = msgspec.UNSET
        jobs: Any = msgspec.UNSET
        stage: Any = msgspec.UNSET
        cleanupVerified: Any = msgspec.UNSET
        shardIndex: Any = msgspec.UNSET
        shardCount: Any = msgspec.UNSET
        workload: Any = msgspec.UNSET
        offeredBySupi: Any = msgspec.UNSET
        aggregateKbps: Any = msgspec.UNSET
        bindings: Any = msgspec.UNSET
        executionMode: Any = msgspec.UNSET
        detachedReservation: Any = msgspec.UNSET
        workerDiagnostics: Any = msgspec.UNSET

    class _Snapshot(msgspec.Struct):
        roundId: Any = msgspec.UNSET
        generation: Any = msgspec.UNSET
        stage: Any = msgspec.UNSET
        executionOutcome: Any = msgspec.UNSET
        fleetLease: Any = msgspec.UNSET
        trafficMode: Any = msgspec.UNSET
        allocation: Any = msgspec.UNSET
        serviceRequirements: Any = msgspec.UNSET
        resolvedRequirements: Any = msgspec.UNSET
        populationRevision: Any = msgspec.UNSET
        populationPending: Any = msgspec.UNSET
        perUe: list[_Ue] = msgspec.UNSET
        actions: list[_Action] = msgspec.UNSET

    _DECODER = msgspec.json.Decoder(_Snapshot)
    _ROOT_FIELDS = frozenset(_Snapshot.__struct_fields__) - {'perUe', 'actions'}
    _ACTION_FIELDS = frozenset(_Action.__struct_fields__) - {'workerDiagnostics'}
else:
    _DECODER = None
    _ROOT_FIELDS = _ACTION_FIELDS = frozenset()


_DIGIT_TRANSLATION = str.maketrans('0123456789', '0000000000')


def parser_policy():
    return (sys.get_int_max_str_digits(), sys.getrecursionlimit())


def requires_legacy_decode(raw, policy):
    # SQLite snapshots are str. Other JSON encodings keep their original path.
    if type(raw) is not str:
        return True
    limit = policy[0]
    if limit == 0 or len(raw) <= limit:
        return False
    # Skipped integers must obey CPython's dynamic decimal conversion limit.
    # Quoted digits / floats may also match: only stdlib decides validity.
    return '0' * (limit + 1) in raw.translate(_DIGIT_TRANSLATION)


def decode_control(raw, *, root_fields, action_fields):
    # Future control fields must never disappear because an older optimized
    # schema skipped them. Missing dependency/schema changes keep the old path.
    policy = parser_policy()
    if (_DECODER is None or frozenset(root_fields) != _ROOT_FIELDS
            or frozenset(action_fields) != _ACTION_FIELDS
            or requires_legacy_decode(raw, policy)):
        return json.loads(raw)
    try:
        result = msgspec.to_builtins(_DECODER.decode(raw))
    except msgspec.DecodeError:
        # Preserve stdlib semantics for malformed/legacy shapes, nonfinite
        # numbers, duplicate keys and Unicode. Internal errors must propagate.
        return json.loads(raw)
    if parser_policy() != policy:
        return json.loads(raw)
    return result
