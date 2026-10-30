"""One validated current view of physical devices and native PDU sessions."""
from __future__ import annotations

import ipaddress
import json
import re
from uuid import UUID

from backend.onprem.intents.domain import ContractError, SessionKey

V2 = 'cityverse.current-state/v2'
IDENTITY_FIELDS = ('sessionKey', 'deviceId', 'supi', 'pduSessionId', 'sessionId',
                   'establishedEpoch', 'smContextRef', 'podUid', 'generation',
                   'snssai', 'dnn', 'upf', 'nativeUpfId', 'upfEpoch', 'ueIp', 'tun', 'slice', 'containerId', 'processEpoch')


def canonical_session_key(value):
    try:
        parts = json.loads(value)
        if not isinstance(parts, list) or len(parts) != 4:
            raise ValueError
        key = SessionKey(*parts)
        if key.token != value:
            raise ValueError
        return key
    except (TypeError, ValueError, ContractError):
        raise ContractError('current_session_key_not_canonical') from None


def session_identity(row):
    return row['sessionKey'] if 'sessionKey' in row else row['supi']


def session_rows(snapshot):
    """Malformed v2 never falls back to legacy per-device summaries."""
    if not isinstance(snapshot, dict):
        raise ContractError('current_snapshot_object_required')
    schema = snapshot.get('schemaVersion')
    if schema != V2:
        if schema not in (None, 'cityverse.current-state/v1'):
            raise ContractError('current_schema_unsupported')
        rows = snapshot.get('perUe', [])
        if not isinstance(rows, list):
            raise ContractError('legacy_current_per_ue_list_required')
        return rows
    rows, devices = snapshot.get('perSession'), snapshot.get('devices')
    if not isinstance(rows, list) or not isinstance(devices, list) or len(devices) > 100 or len(rows) > 1500:
        raise ContractError('v2_current_session_and_device_lists_required')
    generation = snapshot.get('generation')
    revision = snapshot.get('populationRevision', 0)
    if type(revision) is not int or revision < 0:
        raise ContractError('v2_current_population_revision_invalid')
    if type(generation) is not int or generation < 0:
        raise ContractError('v2_current_generation_missing')
    identities, supis, declared = {}, set(), set()
    for device in devices:
        if (not isinstance(device, dict) or any(not isinstance(device.get(k), str) or not device[k]
                for k in ('deviceId', 'supi', 'podUid', 'containerId', 'processEpoch')) or device.get('generation') != generation
                or device.get('processEpoch', '').lower() in {'unknown', 'none', 'null'}
                or not isinstance(device.get('sessionKeys'), list) or not device['sessionKeys']):
            raise ContractError('v2_current_device_identity_missing')
        if device['deviceId'] in identities or device['supi'] in supis:
            raise ContractError('v2_current_duplicate_device')
        keys = device['sessionKeys']
        if len(keys) > 15 or any(not isinstance(k, str) or len(k) > 2048 for k in keys) or len(set(keys)) != len(keys) or declared.intersection(keys):
            raise ContractError('v2_current_duplicate_session')
        identities[device['deviceId']] = device
        supis.add(device['supi']); declared.update(keys)
    seen, tun_owners, addresses, scopes = set(), set(), set(), {}
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError('v2_current_session_identity_missing')
        key = canonical_session_key(row.get('sessionKey'))
        device = identities.get(row.get('deviceId'), {})
        if (row.get('sessionKey') in seen or row.get('sessionKey') not in device.get('sessionKeys', [])
                or row.get('supi') != key.supi or row.get('supi') != device.get('supi')
                or type(row.get('pduSessionId')) is not int or row['pduSessionId'] != key.pdu_session_id
                or type(row.get('sessionId')) is not int or row['sessionId'] != key.pdu_session_id
                or row.get('establishedEpoch') != key.established_epoch
                or row.get('generation') != generation or row.get('podUid') != device.get('podUid')
                or any(row.get(k) != device.get(k) for k in ('containerId', 'processEpoch'))
                or any(not isinstance(row.get(k), str) or not row[k].strip() or row[k].lower() in {'unknown', 'none', 'null'}
                       for k in ('smContextRef', 'upfEpoch', 'dnn', 'snssai', 'upf', 'nativeUpfId', 'slice', 'tun', 'ueIp'))):
            raise ContractError('v2_current_session_identity_missing')
        if row.get('networkScope', key.network_scope) != key.network_scope:
            raise ContractError('v2_current_session_scope_mismatch')
        try:
            if str(UUID(row['nativeUpfId'])) != row['nativeUpfId']:
                raise ValueError
        except ValueError:
            raise ContractError('v2_current_native_upf_uuid_invalid') from None
        if (device.get('networkScope', key.network_scope) != key.network_scope
                or scopes.setdefault(row['deviceId'], key.network_scope) != key.network_scope):
            raise ContractError('v2_current_session_scope_mismatch')
        match = re.fullmatch(r'([1-9][0-9]{0,2})-([0-9a-f]{6})', row['snssai'])
        if match is None or int(match[1]) > 255:
            raise ContractError('v2_current_session_snssai_invalid')
        if row.get('sst', int(match[1])) != int(match[1]) or row.get('sd', match[2]) not in (match[2], int(match[2], 16)):
            raise ContractError('v2_current_session_snssai_invalid')
        if not re.fullmatch(r'(?:ueTun|uesimtun)[0-9]{1,3}', row['tun']):
            raise ContractError('v2_current_session_tun_invalid')
        try:
            if str(ipaddress.IPv4Address(row['ueIp'])) != row['ueIp']:
                raise ValueError
        except ValueError:
            raise ContractError('v2_current_session_ip_invalid') from None
        if (row['podUid'], row['tun']) in tun_owners or (row['podUid'], row['ueIp']) in addresses:
            raise ContractError('v2_duplicate_tun_or_source_ip_not_supported')
        if row.get('ip', row['ueIp']) != row['ueIp'] or row.get('interface', row['tun']) != row['tun']:
            raise ContractError('v2_current_session_alias_mismatch')
        tun_owners.add((row['podUid'], row['tun'])); addresses.add((row['podUid'], row['ueIp']))
        seen.add(row['sessionKey'])
    if seen != declared:
        raise ContractError('v2_current_session_coverage_incomplete')
    return rows
