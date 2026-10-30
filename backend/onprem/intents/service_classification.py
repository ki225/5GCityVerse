"""Canonical city profiles are distinct from native launch service labels.

Call configured_profile only after the caller has authenticated the server-owned
launch specification. A label on a receiver, request or map object is not proof.
"""
from backend.onprem.scenarios.profiles import PROFILE_IDS, PROFILE_SLICES
from .domain import ContractError


def configured_profile(logical_session_id, service_type, slice_id=None):
    candidates = {value for value in (logical_session_id, service_type) if value in PROFILE_IDS}
    if len(candidates) > 1:
        raise ContractError('service_profile_classification_conflict')
    profile = next(iter(candidates), '')
    if profile and slice_id is not None and PROFILE_SLICES[profile] != slice_id:
        raise ContractError('service_profile_slice_conflict')
    return profile


def configured_session_profile(spec):
    from backend.onprem.runtime.topology import SLICES
    slices = [key for key, value in SLICES.items()
              if value['sst'] == spec.sst and int(value['sd'], 16) == spec.sd]
    if len(slices) != 1:
        raise ContractError('service_profile_slice_not_commissioned')
    return configured_profile(spec.session_id, spec.service_type, slices[0])


def matches_service_types(selected, native_type, profile_id=''):
    """Canonical selectors require a canonical classification; legacy is exact.

    Never map baseline to web (or to any other profile) by alias. The caller
    obtains profile_id from an independently bound launch classification.
    """
    if profile_id and profile_id not in PROFILE_IDS:
        raise ContractError('unknown_service_profile_classification')
    if profile_id and native_type in PROFILE_IDS and native_type != profile_id:
        raise ContractError('service_profile_classification_conflict')
    if not selected:
        return True
    return any((profile_id or native_type) == value if value in PROFILE_IDS
               else native_type == value for value in selected)
