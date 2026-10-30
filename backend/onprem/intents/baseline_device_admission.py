"""Typed FreeRAN launch reservation using the existing baseline SMF selection.

This port reserves identities, not network capacity or a new UPF route. It must
never replace a configured pre-traffic selection or infrastructure planner.
"""
from __future__ import annotations

import time

from .domain import ContractError
from .pending_admission import PreparedSessionAdmissions
from backend.onprem.runtime.ue_reservation import HeldUERepository, UEAdmissionLaunchProfile


class BaselineDeviceAdmissionPreparer:
    def __init__(self, controller, *, profile_reader, authority, network_scope, clock=time.time):
        self.controller = controller
        self.profile_reader = profile_reader
        self.authority = authority
        self.network_scope = network_scope
        self.clock = clock

    async def capabilities(self):
        from .service import invoke
        profile = await invoke(self.profile_reader, {}) if self.profile_reader else None
        driver = getattr(self.controller, 'driver', None)
        ready = (type(profile) is UEAdmissionLaunchProfile
            and profile.source == 'native-ue-device-launch-profile/v2'
            and bool(profile.device_sessions) and self.clock() < profile.valid_until
            and getattr(driver, 'backend', None) == 'free-ran-ue'
            and getattr(driver, 'reservation_configuration_digest', None) == profile.configuration_digest
            and getattr(driver, 'image', '').split('@')[-1] == profile.image_digest)
        return {'configured': True, 'mode': 'baseline-device-reservation',
            'profile_valid': ready, 'changes_selection': False,
            'profile_valid_until': profile.valid_until if ready else None,
            'source': 'commissioned-device-launch-profile', 'service_effect_verified': False}

    async def __call__(self, intent, body, operation_id, *, owner):
        from .service import invoke
        from backend.onprem.scenarios.contracts import slice_counts, validate_scenario_request

        if intent.definition_issues(self.authority):
            raise ContractError('needs_definition:baseline_device_policy_authority')
        valid = validate_scenario_request(body, allow_none=True)
        if valid['strategy'] != 'none':
            raise ContractError('baseline_device_requires_formal_none_traffic')
        profile = await invoke(self.profile_reader, valid) if self.profile_reader else None
        if (type(profile) is not UEAdmissionLaunchProfile
                or profile.source != 'native-ue-device-launch-profile/v2'
                or not profile.device_sessions or self.clock() >= profile.valid_until):
            raise ContractError('needs_measurement:commissioned_device_launch_profile_missing_or_expired')
        driver = getattr(self.controller, 'driver', None)
        if getattr(driver, 'backend', None) != 'free-ran-ue':
            raise ContractError('baseline_device_requires_free_ran_ue_driver')
        # UEController.reserve independently checks current native empty fleet,
        # exact image/compiler digest, mutation fences and the durable hold CAS.
        from backend.onprem.runtime.errors import EvidenceUnavailable
        try:
            held = await self.controller.reserve(slice_counts(valid), owner_id=owner,
                operation_id=operation_id, request_id=valid['requestId'],
                network_scope=self.network_scope,
                valid_until=min(intent.valid_until, profile.valid_until), profile=profile)
        except EvidenceUnavailable as error:
            from .reservation_diagnostic import annotate
            annotate(error)
            raise
        if not held.devices:
            raise ContractError('baseline_device_typed_reservation_required')
        return PreparedSessionAdmissions(valid['requestId'], held.reservation_id,
            held.version, (), selection_required=False,
            source='native-baseline-device-reservation/v1', held_ue_allocation=held.to_dict())

    def validate_before_start(self, operation_id, owner):
        held = HeldUERepository(self.controller.store).current()
        if (held is None or not held.devices or not held.active(self.clock())
                or (held.operation_id, held.owner_id) != (operation_id, owner)):
            raise ContractError('baseline_device_current_reservation_changed')


def configured_baseline_device_admission(context, config, authority):
    """Explicit operator opt-in; required selection gates cannot be overridden."""
    enabled = config.get('baseline_device_admission', False)
    if type(enabled) is not bool:
        raise ContractError('baseline_device_admission_requires_boolean')
    if not enabled:
        return None
    if config.get('requires_pre_traffic_admission') is True:
        raise ContractError('baseline_device_cannot_replace_required_pretraffic_selection')
    if getattr(context, 'intent_pre_traffic_admission', None) is not None:
        raise ContractError('baseline_device_cannot_replace_configured_pretraffic_admission')
    profile_data = config.get('ue_launch_profile')
    profile = UEAdmissionLaunchProfile.from_dict(profile_data) if profile_data else None
    return BaselineDeviceAdmissionPreparer(context.ue_controller,
        profile_reader=lambda _body: profile, authority=authority,
        network_scope=config.get('session_inventory', {}).get('network_scope', ''))
