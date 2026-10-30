"""Read reservation maintenance/sequence fields without building evidence trees.

This is not a native-evidence or display decoder. All rows are still read in
the caller's original transaction and evaluated by its maintenance predicate.
"""
from __future__ import annotations

import json
from typing import Any

from backend.onprem.evidence.control_decode import parser_policy, requires_legacy_decode

try:
    import msgspec
except ImportError:
    msgspec = None


if msgspec is not None:
    class _Action(msgspec.Struct):
        maintenance: Any = msgspec.UNSET

    class _Snapshot(msgspec.Struct):
        gen17PartialRelease: Any = msgspec.UNSET
        gen34PartialRecovery: Any = msgspec.UNSET
        actions: list[_Action] = msgspec.UNSET
        sequence: Any = msgspec.UNSET

    # Any preserves scalar types; the hook keeps CPython's float conversion.
    _DECODER = msgspec.json.Decoder(_Snapshot, float_hook=float)
else:
    _DECODER = None


def decode_reservation_snapshot(raw):
    """Preserve the original JSON errors and malformed maintenance semantics."""
    policy = parser_policy()
    if _DECODER is None or requires_legacy_decode(raw, policy):
        return json.loads(raw)
    try:
        result = msgspec.to_builtins(_DECODER.decode(raw))
    except (msgspec.DecodeError, UnicodeEncodeError, RecursionError):
        return json.loads(raw)
    if parser_policy() != policy:
        return json.loads(raw)
    return result
