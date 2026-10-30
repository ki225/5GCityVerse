"""Strict public city-traffic v2 -> bounded private runtime allocation."""
from copy import deepcopy
from .profiles import PROFILE_IDS
from .contracts import ScenarioRequestError, validate_scenario_request

SCHEMA = 'cityverse.city-traffic/v2'

def parse_profile_request(value):
    required = {'schemaVersion', 'requestId', 'strategy', 'trafficMode', 'topologyRevision',
                'demandGroups', 'serviceRequirements'}
    if not isinstance(value, dict) or set(value) != required or value.get('schemaVersion') != SCHEMA:
        raise ScenarioRequestError('INVALID_PROFILE_REQUEST', 'Provide cityverse.city-traffic/v2 and profile demand groups')
    groups = value['demandGroups']
    if not isinstance(groups, list) or not 1 <= len(groups) <= 100:
        raise ScenarioRequestError('INVALID_PROFILE_REQUEST', 'Provide 1..100 profile groups')
    counts = dict.fromkeys(PROFILE_IDS, 0)
    normalized = []
    for group in groups:
        fields = {'groupId', 'profileId', 'ueCount', 'sourceLocation'}
        if not isinstance(group, dict) or not fields <= set(group) or set(group) - fields - {'serviceId'}:
            raise ScenarioRequestError('INVALID_PROFILE_REQUEST', 'Demand groups require profileId; retired eventType is unsupported')
        profile, count = group['profileId'], group['ueCount']
        if not isinstance(profile, str) or profile not in counts or type(count) is not int or not 1 <= count <= 100:
            raise ScenarioRequestError('INVALID_SERVICE_PROFILE', 'Unknown profile or invalid UE count')
        counts[profile] += count
        normalized.append(deepcopy(group))
    body = {key: deepcopy(value[key]) for key in
            ('requestId', 'strategy', 'trafficMode', 'topologyRevision', 'serviceRequirements')}
    body.update(baselineUes=counts.pop('web_browsing'), events=counts, demandGroups=normalized)
    return validate_scenario_request(body, allow_none=True)
