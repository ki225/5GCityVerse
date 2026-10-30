"""Narrow display scope; never native evidence or mutation authority."""
from __future__ import annotations

import json
from typing import Any

from backend.onprem.evidence.control_decode import parser_policy, requires_legacy_decode

try:
    import msgspec
except ImportError:
    msgspec = None


if msgspec is not None:
    class _Scope(msgspec.Struct):
        roundId: Any = msgspec.UNSET
        generation: Any = msgspec.UNSET

    _DECODER = msgspec.json.Decoder(_Scope)
else:
    _DECODER = None


def decode_scope(raw):
    """Retain original values/errors while skipping unrelated valid JSON trees.

    Missing fields stay missing. Legacy/corrupt JSON uses the original parser,
    including unknown oversized integers which a partial parser could skip.
    No cache is kept: every caller supplied freshly read committed text.
    """
    policy = parser_policy()
    if _DECODER is None or requires_legacy_decode(raw, policy):
        return json.loads(raw)
    try:
        scope = _DECODER.decode(raw)
    except (msgspec.DecodeError, RecursionError):
        return json.loads(raw)
    if parser_policy() != policy:
        return json.loads(raw)
    return {name: getattr(scope, name) for name in ('roundId', 'generation')
            if getattr(scope, name) is not msgspec.UNSET}
