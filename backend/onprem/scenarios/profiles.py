"""Canonical city service catalog shared with the frontend; no retired aliases."""
from copy import deepcopy
import json
from pathlib import Path
from types import MappingProxyType

_catalog = json.loads((Path(__file__).resolve().parents[3] / 'shared/service-profiles.json').read_text(encoding='utf-8'))
PROFILE_IDS = tuple(row['id'] for row in _catalog['profiles'])
if len(PROFILE_IDS) != 5 or len(set(PROFILE_IDS)) != 5:
    raise ValueError('Exactly five unique city service profiles required')
PROFILE_SLICES = MappingProxyType({row['id']: row['slice'] for row in _catalog['profiles']})
DEFAULT_TRAFFIC = MappingProxyType({row['id']: {
    'slice': row['slice'], 'protocol': 'udp',
    'offeredUplinkMbpsPerUe': row['offeredUplinkMbpsPerUe'],
    'offeredDownlinkMbpsPerUe': row['offeredDownlinkMbpsPerUe'],
} for row in _catalog['profiles']})

def public_catalog():
    return deepcopy(_catalog)

def allocation_counts(allocation):
    """Private runtime allocation projection; public requests use profile groups."""
    return {'web_browsing': allocation['baselineUes'], **allocation['events']}
