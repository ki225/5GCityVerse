"""Current server-owned FreeRAN launch requests, never historical evidence.

There is no measurement TTL or capacity grant here. The fixed single-PDU
compiler requests the initial PDU identity; native registration/session joins
must still independently confirm its actual identity and path after launch.
"""
from dataclasses import asdict, dataclass
import hashlib
import json

from backend.onprem.intents.domain import ContractError
from .device_sessions import DeviceDriverProfile, FREE_RAN_DEVICE_CONFIG_PATCH, SessionLaunchSpec
from .topology import SLICES


@dataclass(frozen=True)
class CurrentDeviceLaunchContract:
    contract_id: str
    image_digest: str
    configuration_digest: str
    session_ids: tuple
    device_sessions: tuple
    device_session_ids: tuple

    def to_dict(self):
        return asdict(self)


def current_device_launch_contract(driver):
    """Derive only from the supported compiler and its trusted driver config.

    The PDU number is the fixed binary's first single-session request, not an
    observed/established PDU claim. No HTTP field or archived profile supplies
    DNN, NSSAI, native service labels, image, compiler or gNB permissions.
    """
    from .free_ran_ue import CONFIG_INIT, UE_IMAGE
    profile = getattr(driver, 'device_profile', None)
    if (getattr(driver, 'backend', None) != 'free-ran-ue'
            or type(profile) is not DeviceDriverProfile or profile.driver != 'free-ran-ue'
            or getattr(driver, 'image', None) != UE_IMAGE
            or profile.image_digest != UE_IMAGE.split('@')[1]
            or profile.maximum_sessions != 1
            or not profile.commissioned_gnb_endpoints
            or not profile.permits_endpoint(profile.gnb_search_endpoint)):
        raise ContractError('current_free_ran_launch_configuration_required')
    digest = hashlib.sha256((CONFIG_INIT + FREE_RAN_DEVICE_CONFIG_PATCH
        + json.dumps(asdict(profile), sort_keys=True)).encode()).hexdigest()
    if getattr(driver, 'reservation_configuration_digest', None) != digest:
        raise ContractError('current_free_ran_launch_configuration_changed')
    sessions = tuple((name, (SessionLaunchSpec('baseline', 'internet', row['sst'],
        int(row['sd'], 16), 'baseline', 'city'),)) for name, row in SLICES.items())
    ids = tuple((name, 1) for name in SLICES)
    device_ids = tuple((name, 'baseline', 1) for name in SLICES)
    identity = hashlib.sha256(json.dumps(dict(image=profile.image_digest,
        configuration=digest, sessions=[(g, [asdict(s) for s in rows]) for g, rows in sessions],
        requested_pdu_ids=device_ids), sort_keys=True).encode()).hexdigest()
    return CurrentDeviceLaunchContract('current-free-ran-' + identity, profile.image_digest,
        digest, ids, sessions, device_ids)
