"""Full report JSON plus conservative non-finite tracking for strict writes."""
import json
import math

from backend.onprem.evidence.control_decode import parser_policy, requires_legacy_decode

try:
    import msgspec
except ImportError:
    msgspec = None


def decode_checked_report(raw):
    """Keep CPython numeric/duplicate-key semantics; never omit report fields."""
    policy = parser_policy()
    nonfinite = False
    finite, convert = math.isfinite, float
    original_constant = json.JSONDecoder().parse_constant

    def number(text):
        nonlocal nonfinite
        value = convert(text)
        nonfinite = nonfinite or not finite(value)
        return value

    def constant(text):
        nonlocal nonfinite
        nonfinite = True
        return original_constant(text)

    def legacy():
        return json.loads(raw, parse_float=number, parse_constant=constant)

    if msgspec is None or requires_legacy_decode(raw, policy):
        return legacy(), nonfinite
    try:
        value = msgspec.json.Decoder(float_hook=number).decode(raw)
    except (msgspec.DecodeError, UnicodeEncodeError, RecursionError):
        return legacy(), nonfinite
    if parser_policy() != policy:
        return legacy(), nonfinite
    return value, nonfinite
