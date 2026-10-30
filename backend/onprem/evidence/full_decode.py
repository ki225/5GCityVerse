"""Decode complete JSON with the original stdlib compatibility fallback."""
from __future__ import annotations

import json

from backend.onprem.evidence.control_decode import parser_policy, requires_legacy_decode

try:
    import msgspec
except ImportError:
    msgspec = None


_DECODE_ERRORS = ((msgspec.DecodeError,) if msgspec is not None else ()) + (
    UnicodeEncodeError, RecursionError)


class FullDecoder:
    """Complete, untyped trees; no projection, encoding, caching or coercion."""

    def __init__(self):
        # Match CPython's decimal-to-float conversion, including the last bits.
        self.generic = (msgspec.json.Decoder(float_hook=float).decode
                        if msgspec is not None else None)

    def loads(self, raw):
        policy = parser_policy()
        if self.generic is None or requires_legacy_decode(raw, policy):
            return json.loads(raw)
        try:
            result = self.generic(raw)
        except _DECODE_ERRORS:
            return json.loads(raw)
        if parser_policy() != policy:
            return json.loads(raw)
        return result


_DEFAULT_DECODER = FullDecoder()


def decode_full(raw):
    """Return the full decoded value, or the original stdlib result/error."""
    return _DEFAULT_DECODER.loads(raw)
