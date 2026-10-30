"""Unwired, default-off validation of operator-reviewed container retirement.

SHA pins integrity, not authenticity. Physical observations must come from a
future authenticated runtime collector, never HTTP request JSON. No I/O here.
"""
import hashlib
import json
import math
import re

from backend.onprem.runtime.kubeadm import TRAFFIC_IMAGE


class RetirementBlocked(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def require(condition):
    if not condition:
        raise RetirementBlocked('Retired container evidence rejected')


def text(value, pattern):
    require(type(value) is str and re.fullmatch(pattern, value) is not None)


def number(value):
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0)
    return value


def unique(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value)
        value[key] = item
    return value


FIELDS = set('schema approvalId reservationId issuedAt expiresAt roundId generation actionId token jobsDigest bindingsDigest '
             'context namespace node nodeBootId pod podUid containerName image oldContainerId '
             'oldPidNamespace oldPid oldStartTicks oldPid1 newContainerId newPidNamespace newPid newStartTicks restartCount '
             'otherIdentitiesDigest archiveDigest planDigest drainDigest files directories'.split())
FILE = r'(?:owner|children|keys|manifest|control\.lock|u[0-4][0-9]-(?:ul|dl)\.(?:json|meta|err|armed|started))'


def validate(raw, pinned_digest, snapshot, physical, reservation, *, now, enabled=False, artifacts=None):
    """Return fixed receipt only after a durable claimed maintenance reservation.

    This function does not install authorization or create the reservation.
    Its caller must own the application maintenance fence and drain all work.
    """
    return _validate(raw, pinned_digest, snapshot, physical, reservation, now=now,
                     enabled=enabled, artifacts=artifacts, expected_state='closure_claimed')


def validate_preclaim(raw, pinned_digest, snapshot, physical, reservation, *, now, enabled=False, artifacts=None):
    """Validate a reserved plan and return only the original receipt for claim CAS.

    No claim is inferred or written. This result grants no dispatch authority;
    provider command preparation still requires durable closure_claimed state.
    """
    _, receipt = _validate(raw, pinned_digest, snapshot, physical, reservation, now=now,
                          enabled=enabled, artifacts=artifacts, expected_state='reserved')
    return receipt


def _validate(raw, pinned_digest, snapshot, physical, reservation, *, now, enabled, artifacts, expected_state):
    require(enabled is True and type(raw) is bytes and len(raw) <= 131072)
    text(pinned_digest, r'[a-f0-9]{64}')
    require(hashlib.sha256(raw).hexdigest() == pinned_digest)
    try:
        approval = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: require(False))
        require(type(approval) is dict and set(approval) == FIELDS and canonical(approval) == raw)
        require(approval['schema'] == 'cityverse.retired-container-epoch/v1')
        for key in ('approvalId', 'reservationId', 'roundId', 'actionId', 'podUid', 'nodeBootId'):
            text(approval[key], r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}')
        for key in ('jobsDigest', 'bindingsDigest', 'otherIdentitiesDigest', 'archiveDigest', 'planDigest', 'drainDigest'):
            text(approval[key], r'[a-f0-9]{64}')
        text(approval['token'], r'[a-f0-9]{32}')
        for key in ('context', 'namespace', 'node', 'pod'):
            text(approval[key], r'[a-z0-9][a-z0-9-]{0,252}')
        for key in ('oldContainerId', 'newContainerId'):
            text(approval[key], r'containerd://[a-f0-9]{64}')
        for key in ('oldPidNamespace', 'newPidNamespace'):
            text(approval[key], r'pid:\[[1-9][0-9]{0,19}\]')
        for key in ('generation', 'oldPid', 'oldStartTicks', 'newPid', 'newStartTicks', 'restartCount'):
            require(type(approval[key]) is int and 0 < approval[key] < 2**63)
        require(type(approval['oldPid1']) is dict and set(approval['oldPid1']) == {'pid', 'comm', 'startTicks'})
        require(type(approval['oldPid1']['pid']) is int and approval['oldPid1']['pid'] == 1
                and type(approval['oldPid1']['startTicks']) is int
                and approval['oldPid1']['startTicks'] == approval['oldStartTicks'])
        text(approval['oldPid1']['comm'], r'[a-zA-Z0-9_.-]{1,64}')
        # Hash actual retained bytes; booleans or path names are not verification.
        require(type(artifacts) is dict and set(artifacts) == {'archive', 'plan', 'drain'})
        for key in artifacts:
            require(type(artifacts[key]) is bytes and 0 < len(artifacts[key]) <= 4_000_000)
            require(hashlib.sha256(artifacts[key]).hexdigest() == approval[key + 'Digest'])
        require(approval['image'] == TRAFFIC_IMAGE and approval['containerName'] == 'traffic')
        require(approval['oldContainerId'] != approval['newContainerId'])
        require(approval['oldPidNamespace'] != approval['newPidNamespace'])
        number(now)
        require(number(approval['issuedAt']) <= now < number(approval['expiresAt']) <= approval['issuedAt'] + 900)
        files = approval['files']
        require(type(files) is dict and 4 <= len(files) <= 600)
        require({'owner', 'children', 'keys', 'manifest', 'control.lock'} <= set(files))
        for name, sha in files.items():
            text(name, FILE)
            text(sha, r'[a-f0-9]{64}')
        require(approval['directories'] in ([], ['cancelling']))
        require(snapshot['roundId'] == approval['roundId'] and type(snapshot['generation']) is int
                and snapshot['generation'] == approval['generation'] and snapshot['stage'] == 'cleaning'
                and snapshot['executionOutcome'] in ('failed', 'cancelled'))
        actions = [a for a in snapshot['actions'] if a.get('id') == approval['actionId']]
        require(len(actions) == 1)
        action = actions[0]
        require(action['kind'] == 'traffic_window' and action['mode'] == 'batch'
                and action['token'] == approval['token'] and action['cleanupVerified'] is False
                and digest(action['jobs']) == approval['jobsDigest']
                and digest(action['bindings']) == approval['bindingsDigest'])
        require(type(action['jobs']) is list and 1 <= len(action['jobs']) <= 100)
        require(all(j['receiver_pod'] == approval['pod'] and j['receiver_uid'] == approval['podUid']
                    for j in action['jobs']))
        expected = {'state': expected_state, 'actionId': approval['actionId'], 'token': approval['token'],
                    'reservationId': approval['reservationId'], 'planDigest': approval['planDigest']}
        if expected_state == 'closure_claimed':
            expected.update(approvalDigest=pinned_digest, approvalId=approval['approvalId'])
        require(reservation == expected)
        # Original observations, not only a caller's aggregate success boolean.
        require(set(physical) == {'identity', 'observedAt', 'oldTasks', 'oldNamespaceProcesses',
                                 'newWorkers', 'scanComplete', 'ready', 'drainDigest', 'originalFiles'})
        identity_keys = ('context', 'namespace', 'node', 'nodeBootId', 'pod', 'podUid', 'containerName',
                         'image', 'oldContainerId', 'oldPidNamespace', 'newContainerId',
                         'newPidNamespace', 'otherIdentitiesDigest', 'oldPid', 'oldStartTicks', 'oldPid1',
                         'newPid', 'newStartTicks', 'restartCount')
        require(physical['identity'] == {k: approval[k] for k in identity_keys})
        require(physical['oldTasks'] == [] and physical['oldNamespaceProcesses'] == []
                and physical['newWorkers'] == [] and physical['scanComplete'] is True and physical['ready'] is True)
        require(physical['drainDigest'] == approval['drainDigest'] and physical['originalFiles'] == files)
        times = physical['observedAt']
        require(type(times) is dict and set(times) == {'pod', 'runtime', 'namespace', 'drain', 'files'})
        require(all(0 <= now - number(t) <= 15 and t >= approval['issuedAt'] for t in times.values()))
        receipt = {'schema': 'cityverse.retired-container-closure/v1', 'approvalDigest': pinned_digest,
                   'approvalId': approval['approvalId'], 'reservationId': approval['reservationId'], 'roundId': approval['roundId'],
                   'generation': approval['generation'], 'actionId': approval['actionId'],
                   'token': approval['token'], 'oldContainerId': approval['oldContainerId'],
                   'newContainerId': approval['newContainerId'], 'proofDigest': digest(physical),
                   'evidenceValidity': False}
        return approval, receipt
    except (KeyError, TypeError, json.JSONDecodeError, OverflowError) as exc:
        raise RetirementBlocked('Retired container evidence malformed') from exc
