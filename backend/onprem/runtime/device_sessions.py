"""Device allocation and per-PDU native joins, independent of legacy UE counts.

The compiler emits one native UE process for each device. Configuration is a
request; actual PDU IDs and paths come exclusively from the native readback join.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import math
import re

from backend.onprem.intents.domain import ContractError, SessionBinding, required

DEVICE_CONFIG_PATCH = "\nwith open('/run/ue/config.yaml') as f: config=json.load(f)\nconfig.update(json.loads(os.environ['DEVICE_SESSION_CONFIGURATION']))\nwith open('/run/ue/config.yaml','w') as f: json.dump(config,f)\n"


from backend.onprem.runtime.current_sessions import session_rows


@dataclass(frozen=True)
class SessionLaunchSpec:
    session_id: str
    dnn: str
    sst: int
    sd: int
    service_type: str
    region: str
    applications: tuple[str, ...] = ()

    def __post_init__(self):
        for field in ('session_id', 'dnn', 'service_type', 'region'):
            required(getattr(self, field), field)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,99}', self.dnn):
            raise ContractError('invalid_device_session_dnn')
        if type(self.sst) is not int or not 1 <= self.sst <= 255 or type(self.sd) is not int or not 0 <= self.sd <= 0xffffff:
            raise ContractError('invalid_device_session_snssai')
        object.__setattr__(self, 'applications', tuple(self.applications))
        if len(set(self.applications)) != len(self.applications):
            raise ContractError('duplicate_device_application')
        for item in self.applications:
            required(item, 'application')

    @property
    def snssai(self):
        return f'{self.sst}-{self.sd:06x}'


@dataclass(frozen=True)
class DeviceAllocation:
    device_id: str
    supi: str
    credential_group: int
    access_slice: str
    network_scope: str
    sessions: tuple[SessionLaunchSpec, ...]

    def __post_init__(self):
        required(self.device_id, 'device_id')
        required(self.access_slice, 'access_slice')
        required(self.network_scope, 'network_scope')
        if not re.fullmatch(r'imsi-[0-9]{5,15}', self.supi):
            raise ContractError('invalid_device_supi')
        if type(self.credential_group) is not int or not 0 <= self.credential_group <= 3:
            raise ContractError('invalid_device_credentials')
        sessions = tuple(self.sessions)
        if not 1 <= len(sessions) <= 15 or any(type(s) is not SessionLaunchSpec for s in sessions):
            raise ContractError('typed_device_sessions_required')
        if len({s.session_id for s in sessions}) != len(sessions):
            raise ContractError('duplicate_device_session_id')
        # Native UERANSIM matches defaults by APN and S-NSSAI. Distinct service
        # labels alone cannot create two independent PDU sessions.
        if len({(s.dnn, s.sst, s.sd) for s in sessions}) != len(sessions):
            raise ContractError('ambiguous_device_session_network_identity')
        object.__setattr__(self, 'sessions', sessions)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**(dict(value) | {'sessions': tuple(SessionLaunchSpec(**s) for s in value['sessions'])}))


def validate_devices(devices):
    devices = tuple(devices)
    if any(type(d) is not DeviceAllocation for d in devices):
        raise ContractError('typed_device_allocations_required')
    if len({(d.network_scope, d.supi) for d in devices}) != len(devices) or len({d.device_id for d in devices}) != len(devices):
        raise ContractError('duplicate_device_process_identity')
    return devices


@dataclass(frozen=True)
class DeviceDriverProfile:
    driver: str
    image_digest: str
    source_revision: str
    evidence_reference: str
    maximum_sessions: int = 1
    gnb_search_endpoint: str = ''
    commissioned_gnb_endpoints: tuple[str, ...] = ()

    def __post_init__(self):
        if self.driver not in {'ueransim', 'free-ran-ue'}:
            raise ContractError('unsupported_device_driver')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', self.image_digest):
            raise ContractError('pinned_device_image_required')
        required(self.source_revision, 'source_revision')
        required(self.evidence_reference, 'evidence_reference')
        if type(self.maximum_sessions) is not int or not 1 <= self.maximum_sessions <= 15:
            raise ContractError('invalid_device_driver_session_capacity')
        if self.driver == 'free-ran-ue' and self.maximum_sessions != 1:
            raise ContractError('free_ran_single_session_only')
        endpoints = tuple(self.commissioned_gnb_endpoints)
        if len(set(endpoints)) != len(endpoints) or any(not isinstance(e, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', e) or '..' in e for e in endpoints):
            raise ContractError('invalid_commissioned_device_gnb_endpoints')
        if endpoints and self.driver != 'free-ran-ue':
            raise ContractError('device_vendor_gnb_catalog_mismatch')
        object.__setattr__(self, 'commissioned_gnb_endpoints', endpoints)
        if self.driver=='ueransim':
            endpoint=self.gnb_search_endpoint
            if not isinstance(endpoint,str) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?',endpoint):
                raise ContractError('commissioned_matching_gnb_search_endpoint_required')
            if '.' not in endpoint or '..' in endpoint:
                raise ContractError('fully_qualified_matching_gnb_endpoint_required')

    def validate(self, device):
        if len(device.sessions) > self.maximum_sessions:
            raise ContractError('driver_multiple_sessions_not_commissioned')

    def permits_endpoint(self, endpoint):
        return bool(endpoint) and (endpoint == self.gnb_search_endpoint or (self.driver == 'free-ran-ue' and endpoint in self.commissioned_gnb_endpoints))


FREE_RAN_DEVICE_CONFIG_PATCH = "\nconfig['ue']['pduSession']=json.loads(os.environ['DEVICE_SESSION_CONFIGURATION'])\nwith open('/run/fru/ue.json','w') as out: json.dump(config,out)\nos.chmod('/run/fru/ue.json',0o600)\n"


class FreeRanDeviceCompiler:
    """Compile the commissioned FreeRAN single-PDU UE contract, not nr-ue."""
    def __init__(self, profile):
        if type(profile) is not DeviceDriverProfile or profile.driver != 'free-ran-ue':
            raise ContractError('free_ran_device_profile_required')
        self.profile = profile

    def configuration(self, device):
        self.profile.validate(device)
        session = device.sessions[0]
        # FreeRAN parses SST with Atoi and SD as the six hexadecimal digits.
        return {'dnn': session.dnn, 'snssai': {'sst': str(session.sst), 'sd': f'{session.sd:06x}'}}

    def pod_manifest(self, driver, generation, index, device, *, spatial_context=None):
        if getattr(driver, 'backend', None) != 'free-ran-ue' or driver.image.split('@')[-1] != self.profile.image_digest:
            raise ContractError('device_driver_image_profile_mismatch')
        context = validate_device_spatial_context(spatial_context, self.profile)
        endpoint = context['spatialAccess']['endpoint'] if context else self.profile.gnb_search_endpoint
        if not self.profile.permits_endpoint(endpoint):
            raise ContractError('commissioned_matching_gnb_search_endpoint_required')
        # The ordinary FreeRAN manifest retains native credentials, TERM grace,
        # Pod ownership, security and traffic manager. No device recursion.
        manifest = driver.pod_manifest(generation, index, {'slice': device.access_slice,
            'credentialGroup': device.credential_group, 'supis': [device.supi],
            'spatialAccess': {'endpoint': endpoint}})
        patch = self.configuration(device)
        init = next(c for c in manifest['spec']['initContainers'] if c['name'] == 'config')
        ue = next(c for c in manifest['spec']['containers'] if c['name'] == 'ue')
        if ue.get('command') != ['/bin/sh', '-c'] or ue.get('args') != ['exec ./free-ran-ue ue -c /run/fru/ue.json -n 1 -p 1']:
            raise ContractError('one_native_process_per_device_required')
        init['env'].append({'name':'DEVICE_SESSION_CONFIGURATION', 'value':json.dumps(patch, sort_keys=True)})
        init['command'][-1] += FREE_RAN_DEVICE_CONFIG_PATCH
        annotations = manifest['metadata']['annotations']
        annotations.update({'cityverse.io/device-id':device.device_id,
            'cityverse.io/device-sessions':json.dumps(device.to_dict(), sort_keys=True),
            'cityverse.io/device-profile-revision':self.profile.source_revision,
            'cityverse.io/device-driver':'free-ran-ue',
            'cityverse.io/device-profile-sha256':hashlib.sha256(json.dumps(asdict(self.profile),sort_keys=True).encode()).hexdigest(),
            'cityverse.io/device-gnb-search-endpoint':endpoint,
            'cityverse.io/device-configuration-sha256':hashlib.sha256(json.dumps(patch, sort_keys=True).encode()).hexdigest()})
        if context:
            annotations['cityverse.io/spatial-access'] = json.dumps(context['spatialAccess'], sort_keys=True)
            annotations['cityverse.io/device-spatial-context'] = json.dumps(context, sort_keys=True, allow_nan=False)
        else:
            annotations.pop('cityverse.io/spatial-access', None)
        return manifest


class UeransimDeviceCompiler:
    """Compile the actual UERANSIM sessions[] configuration, never guess PDU IDs."""
    def __init__(self, profile: DeviceDriverProfile):
        if profile.driver != 'ueransim':
            raise ContractError('ueransim_device_profile_required')
        self.profile = profile

    def configuration(self, device):
        self.profile.validate(device)
        slices = list({(s.sst, s.sd): {'sst': s.sst, 'sd': s.sd} for s in device.sessions}.values())
        return {'supi': device.supi, 'sessions': [
            {'type': 'IPv4', 'apn': s.dnn, 'slice': {'sst': s.sst, 'sd': s.sd}}
            for s in device.sessions], 'configured-nssai': slices, 'default-nssai': deepcopy(slices)}

    def pod_manifest(self, driver, generation, index, device, *, spatial_context=None):
        """Reuse the native Kubernetes security/credential/traffic-sidecar plumbing."""
        if driver.image.split('@')[-1] != self.profile.image_digest:
            raise ContractError('device_driver_image_profile_mismatch')
        patch = self.configuration(device)
        spatial_context = validate_device_spatial_context(spatial_context, self.profile)
        manifest = driver.pod_manifest(generation, index, {'slice': device.access_slice,
            'credentialGroup': device.credential_group, 'supis': [device.supi]})
        ue = next(c for c in manifest['spec']['containers'] if c['name'] == 'ue')
        if ue['command'][:1] != ['/ueransim/nr-ue'] or ue['command'][-2:] != ['-n', '1']:
            raise ContractError('one_native_process_per_device_required')
        init = next(c for c in manifest['spec']['initContainers'] if c['name'] == 'config')
        for item in init['env']:
            if item['name']=='GNB':
                item['value']=self.profile.gnb_search_endpoint
        init['env'].append({'name': 'DEVICE_SESSION_CONFIGURATION', 'value': json.dumps(patch, allow_nan=False)})
        # Credentials remain inside the init container and never enter this patch.
        init['command'][-1] += DEVICE_CONFIG_PATCH
        manifest['metadata']['annotations'].update({
            'cityverse.io/device-id': device.device_id,
            'cityverse.io/device-sessions': json.dumps(device.to_dict(), sort_keys=True),
            'cityverse.io/device-profile-revision': self.profile.source_revision,
            'cityverse.io/device-gnb-search-endpoint':self.profile.gnb_search_endpoint,
            'cityverse.io/device-configuration-sha256': hashlib.sha256(json.dumps(patch, sort_keys=True).encode()).hexdigest()})
        if spatial_context:
            manifest['metadata']['annotations']['cityverse.io/device-spatial-context'] = json.dumps(spatial_context, sort_keys=True, allow_nan=False)
        return manifest


DEVICE_SPATIAL_FIELDS = ('spatialAccess', 'sourceLocation', 'demandGroupId', 'servingCellId', 'demandProfile')


def validate_device_spatial_context(value, profile):
    """Frozen server allowlist selection and exact native gNB Pod UID pin.

    UERANSIM retains its single commissioned endpoint. FreeRAN can select any
    endpoint from its operator-owned profile; requests cannot extend that list.
    """
    if not value:
        return None
    if not isinstance(value, dict) or not set(value).issubset(DEVICE_SPATIAL_FIELDS):
        raise ContractError('invalid_device_spatial_context')
    if set(value) <= {'demandProfile'}:
        return None  # A non-geographic service profile is not a location claim.
    access = value.get('spatialAccess')
    location = value.get('sourceLocation')
    if (not isinstance(access, dict) or not isinstance(location, dict) or location.get('type') != 'point'
            or not isinstance(location.get('position'), dict) or set(location['position']) != {'x', 'y'}
            or any(type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 24 for n in location['position'].values())
            or not profile.permits_endpoint(access.get('endpoint'))
            or any(not isinstance(access.get(k), str) or not access[k] for k in ('gnbId', 'cellId', 'gnbPodUid'))
            or not isinstance(value.get('demandGroupId'), str) or not value['demandGroupId']
            or value.get('servingCellId') != access['cellId']
            or access.get('groupId', value['demandGroupId']) != value['demandGroupId']):
        raise ContractError('commissioned_device_spatial_binding_required')
    return deepcopy(value)


@dataclass(frozen=True)
class NativeSessionInterface:
    pdu_session_id: int
    interface: str
    address: str

    def __post_init__(self):
        if type(self.pdu_session_id) is not int or not 1 <= self.pdu_session_id <= 15:
            raise ContractError('invalid_native_device_pdu_id')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', self.interface):
            raise ContractError('invalid_native_session_interface')
        if ipaddress.ip_address(self.address).version != 4:
            raise ContractError('native_ipv4_session_required')


@dataclass(frozen=True)
class DeviceSessionReadback:
    device: DeviceAllocation
    pod_uid: str
    container_id: str
    process_epoch: str
    sessions: tuple[tuple[str, SessionBinding, NativeSessionInterface], ...]

    def per_device(self):
        return {'deviceId': self.device.device_id, 'supi': self.device.supi,
            'podUid': self.pod_uid, 'containerId': self.container_id,
            'processEpoch': self.process_epoch, 'sessionKeys': [s.key.token for _, s, _ in self.sessions],
            'sessionCount': len(self.sessions)}

    def per_session(self):
        return [{'logicalSessionId': name, 'sessionId': binding.key.pdu_session_id, 'sessionKey': binding.key.token,
            'deviceId': self.device.device_id, 'supi': binding.key.supi,
            'pduSessionId': binding.key.pdu_session_id, 'smContextRef': binding.key.established_epoch,
            'establishedEpoch': binding.key.established_epoch,
            'snssai': binding.s_nssai, 'dnn': binding.dnn, 'upfId': binding.upf_id, 'upf': binding.upf_id,
            'sst': next(s.sst for s in self.device.sessions if s.session_id==name),
            'sd': next(s.sd for s in self.device.sessions if s.session_id==name),
            'service_type': binding.service_type, 'region': binding.region,
            'declaredApplications': list(next(s.applications for s in self.device.sessions if s.session_id==name)),
            'observedApplications': sorted({f.application_id for f in binding.flows}),
            'podUid': self.pod_uid, 'interface': interface.interface, 'address': interface.address,
            'tun': interface.interface, 'ueIp': interface.address, 'ip': interface.address}
            for name, binding, interface in self.sessions]


def join_ueransim_sessions(device, ps_list, bindings, interfaces, *, pod_uid, container_id, process_epoch):
    """Join actual `nr-cli ps-list`, SMF bindings and actual per-PDU TUN proof.

    Callers must fence all three native reads to the same Pod/container/process.
    A native pending row is never an established-session proof. No allocation
    order is used: two retries may legitimately produce different PDU IDs.
    """
    for value in (pod_uid, container_id, process_epoch):
        required(value, 'native_device_incarnation')
    if not isinstance(ps_list, dict) or len(ps_list) != len(device.sessions):
        raise ContractError('native_device_session_coverage_incomplete')
    bindings, interfaces = tuple(bindings), tuple(interfaces)
    if any(type(s) is not SessionBinding or (s.key.network_scope, s.key.supi) != (device.network_scope, device.supi) for s in bindings):
        raise ContractError('foreign_native_device_binding')
    if len({s.key.pdu_session_id for s in bindings}) != len(bindings):
        raise ContractError('duplicate_native_device_binding')
    if any(type(i) is not NativeSessionInterface for i in interfaces) or len({i.pdu_session_id for i in interfaces}) != len(interfaces):
        raise ContractError('duplicate_or_untyped_native_interface')
    if len({i.interface for i in interfaces}) != len(interfaces) or len({i.address for i in interfaces}) != len(interfaces):
        raise ContractError('native_session_paths_not_distinct')
    expected = {(s.dnn, s.snssai): s for s in device.sessions}
    result, consumed = [], set()
    for name, row in ps_list.items():
        match = re.fullmatch(r'PDU Session([1-9]|1[0-5])', name)
        if not match or not isinstance(row, dict) or row.get('state') != 'PS-ACTIVE' or row.get('session-type') != 'IPv4':
            raise ContractError('native_device_session_not_active')
        pdu = int(match[1])
        slice_value = row.get('s-nssai', {})
        if not isinstance(slice_value, dict):
            raise ContractError('native_device_snssai_missing')
        # UERANSIM ToJson(octet/octet3) emits hex strings, not JSON integers.
        # Explicit integer values also support an already-normalized native reader.
        def octet_value(value, size):
            if isinstance(value, str) and re.fullmatch(r'0x[0-9a-fA-F]{'+str(size*2)+r'}', value):
                value = int(value, 16)
            if type(value) is not int or not 0 <= value < 1 << (8*size):
                raise ContractError('native_device_snssai_missing')
            return value
        sst, sd = octet_value(slice_value.get('sst'), 1), octet_value(slice_value.get('sd'), 3)
        snssai = f'{sst}-{sd:06x}'
        spec = expected.get((row.get('apn'), snssai))
        if spec is None or spec.session_id in consumed:
            raise ContractError('native_device_session_classification_mismatch')
        native = [s for s in bindings if s.key.pdu_session_id == pdu]
        paths = [i for i in interfaces if i.pdu_session_id == pdu]
        if len(native) != 1 or len(paths) != 1:
            raise ContractError('native_device_smf_or_interface_missing')
        binding, path = native[0], paths[0]
        if (binding.s_nssai, binding.dnn, binding.service_type, binding.region) != (spec.snssai, spec.dnn, spec.service_type, spec.region):
            raise ContractError('native_device_session_binding_mismatch')
        if spec.applications and binding.flows and set(spec.applications) != {f.application_id for f in binding.flows}:
            raise ContractError('native_device_application_classification_mismatch')
        if row.get('address') != path.address:
            raise ContractError('native_device_session_address_mismatch')
        consumed.add(spec.session_id)
        result.append((spec.session_id, binding, path))
    if len(bindings) != len(result) or len(interfaces) != len(result):
        raise ContractError('native_device_unmapped_session')
    return DeviceSessionReadback(device, pod_uid, container_id, process_epoch, tuple(result))
