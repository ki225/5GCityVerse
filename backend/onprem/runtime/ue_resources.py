"""Operator-pinned UE scheduling requests; never a capacity grant.

Only reservations may be reduced. Existing limits, process configuration and
security settings remain owned by the pinned native driver. A new profile still
needs an independent real-device footprint test before capacity commissioning.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re

SCHEMA = 'cityverse.ue-resource-requests/v1'
DEFAULT_REQUESTS = {'ue': {'cpu': '100m', 'memory': '128Mi'},
                    'traffic': {'cpu': '50m', 'memory': '64Mi'}}


def resource_policy_digest(profile, *, traffic_enabled, traffic_memory_limit):
    """Seal actual complete resources as well as the selected request profile."""
    if profile is not None and type(profile) is not UERequestsProfile:
        raise ValueError('Typed operator UE request profile required')
    requests = DEFAULT_REQUESTS if profile is None else profile.to_dict()['containers']
    containers = {'ue': {'requests': requests['ue'], 'limits': {'memory': '384Mi'}}}
    if traffic_enabled:
        containers['traffic'] = {'requests': requests['traffic'], 'limits': {'memory': traffic_memory_limit}}
    value = {'schema': 'cityverse.ue-resource-policy/v1', 'containers': containers,
             'request_profile_sha256': profile.digest if profile is not None else None}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _quantity(value, suffix, minimum, maximum):
    if type(value) is not str or not re.fullmatch(r'[1-9][0-9]{0,5}' + suffix, value):
        raise ValueError('UE resource quantity requires bounded integer units')
    number = int(value[:-len(suffix)])
    if not minimum <= number <= maximum:
        raise ValueError('UE resource request outside operator bounds')
    return value


@dataclass(frozen=True)
class UERequestsProfile:
    profile_id: str
    ue_cpu: str
    ue_memory: str
    traffic_cpu: str
    traffic_memory: str

    def __post_init__(self):
        if type(self.profile_id) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', self.profile_id):
            raise ValueError('Invalid operator UE resource profile identifier')
        for value, suffix, maximum in ((self.ue_cpu, 'm', 100), (self.ue_memory, 'Mi', 128),
                (self.traffic_cpu, 'm', 50), (self.traffic_memory, 'Mi', 64)):
            _quantity(value, suffix, 10 if suffix == 'm' else 16, maximum)

    def to_dict(self):
        return {'schemaVersion': SCHEMA, 'profileId': self.profile_id,
            'containers': {'ue': {'cpu': self.ue_cpu, 'memory': self.ue_memory},
                           'traffic': {'cpu': self.traffic_cpu, 'memory': self.traffic_memory}}}

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict or set(value) != {'schemaVersion', 'profileId', 'containers'}
                or value.get('schemaVersion') != SCHEMA or type(value['containers']) is not dict
                or set(value['containers']) != {'ue', 'traffic'}
                or any(type(row) is not dict or set(row) != {'cpu', 'memory'} for row in value['containers'].values())):
            raise ValueError('Invalid operator UE resource profile schema')
        ue, traffic = value['containers']['ue'], value['containers']['traffic']
        return cls(value['profileId'], ue['cpu'], ue['memory'], traffic['cpu'], traffic['memory'])

    def apply(self, manifest):
        containers = manifest['spec']['containers']
        for name, expected in DEFAULT_REQUESTS.items():
            matches = [container for container in containers if container['name'] == name]
            if len(matches) != 1 or matches[0].get('resources', {}).get('requests') != expected:
                raise ValueError('Native UE default resource contract changed')
            resources = matches[0]['resources']
            # Preserve the exact native limits; malformed or absent limits fail.
            memory = resources.get('limits', {}).get('memory')
            if (type(memory) is not str or not re.fullmatch(r'[1-9][0-9]*Mi', memory)
                    or int(memory[:-2]) < int(expected['memory'][:-2])):
                raise ValueError('Native UE memory limit contract unavailable')
            resources['requests'] = dict(self.to_dict()['containers'][name])
        manifest['metadata'].setdefault('annotations', {}).update({
            'cityverse.io/resource-profile-id': self.profile_id,
            'cityverse.io/resource-profile-sha256': self.digest})
        return manifest


def configured_ue_requests(environment=None):
    env = os.environ if environment is None else environment
    filename, digest = env.get('CITYVERSE_ONPREM_UE_RESOURCE_PROFILE_FILE'), env.get('CITYVERSE_ONPREM_UE_RESOURCE_PROFILE_SHA256')
    if filename is None and digest is None:
        return None
    if (not filename or not digest or type(digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise ValueError('Paired operator UE resource profile and SHA256 required')
    path = Path(filename)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('Absolute regular operator UE resource profile required')
    with path.open('rb') as stream:
        raw = stream.read(16_385)
    if len(raw) > 16_384 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('Operator UE resource profile digest or size differs')
    return UERequestsProfile.from_dict(json.loads(raw))
