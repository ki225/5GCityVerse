"""Validated point demand groups, independent of native RAN observations."""
from copy import deepcopy
import math
import re


def validate_spatial_request(body):
    """Require a complete, non-overlapping allocation whenever geography is used."""
    from .contracts import SCENARIO_SLICES
    present = {'topologyRevision', 'demandGroups'} & set(body)
    if not present:
        return
    if present != {'topologyRevision', 'demandGroups'}:
        raise ValueError('Provide topologyRevision and demandGroups together')
    if type(body['topologyRevision']) is not int or body['topologyRevision'] < 1:
        raise ValueError('topologyRevision must be a positive integer')
    groups = body['demandGroups']
    if not isinstance(groups, list) or not 1 <= len(groups) <= 100:
        raise ValueError('demandGroups must contain 1 to 100 groups')
    counts, seen = dict.fromkeys(SCENARIO_SLICES, 0), set()
    for group in groups:
        required = {'groupId', 'profileId', 'ueCount', 'sourceLocation'}
        if not isinstance(group, dict) or not required <= set(group) or set(group) - required - {'serviceId'}:
            raise ValueError('Each demand group requires groupId, profileId, ueCount and sourceLocation')
        if 'serviceId' in group and (not isinstance(group['serviceId'], str)
                or re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', group['serviceId']) is None):
            raise ValueError('serviceId must be a bounded service identifier')
        identifier = group['groupId']
        if not isinstance(identifier, str) or re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', identifier) is None or identifier in seen:
            raise ValueError('Demand group IDs must be unique bounded identifiers')
        seen.add(identifier)
        event, count = group['profileId'], group['ueCount']
        if not isinstance(event, str) or event not in counts or type(count) is not int or count < 1:
            raise ValueError('Demand groups require a known event and positive integer ueCount')
        location = group['sourceLocation']
        if (not isinstance(location, dict) or not {'type', 'position'} <= set(location)
                or set(location) - {'type', 'position', 'buildingId'} or location['type'] != 'point'):
            raise ValueError('sourceLocation must be a point with position')
        if 'buildingId' in location and (not isinstance(location['buildingId'], str)
                or re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', location['buildingId']) is None):
            raise ValueError('buildingId must be a bounded map object identifier')
        position = location['position']
        if not isinstance(position, dict) or set(position) != {'x', 'y'}:
            raise ValueError('Position requires exactly x and y in km')
        if any(type(position[k]) not in (int, float) or not math.isfinite(position[k]) or not 0 <= position[k] <= 24 for k in ('x', 'y')):
            raise ValueError('Demand coordinates must be finite and within the 24 km city')
        counts[event] += count
    expected = {'web_browsing': body['baselineUes'], **body['events']}
    if counts != expected:
        raise ValueError('Demand group counts must exactly match baselineUes and each event count')


def bind_spatial_groups(allocation, rows):
    """Annotate real allocated identities without inventing attachment evidence."""
    groups = allocation.get('demandGroups')
    if not groups:
        return rows
    result = deepcopy(rows)
    for scenario in {group['profileId'] for group in groups}:
        members = sorted((row for row in result if row['scenario'] == scenario), key=lambda row: row['supi'])
        offset = 0
        for group in groups:
            if group['profileId'] != scenario:
                continue
            end = offset + group['ueCount']
            if end > len(members):
                raise ValueError('Spatial demand exceeds real allocated identities')
            for row in members[offset:end]:
                row['demandGroupId'] = group['groupId']
                row['sourceLocation'] = deepcopy(group['sourceLocation'])
                if 'serviceId' in group:
                    row['serviceId'] = group['serviceId']
            offset = end
        if offset != len(members):
            raise ValueError('Spatial demand does not cover real allocated identities')
    return result
