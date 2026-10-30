"""Pure native scenario receipt validation shared by backend and browser tooling."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import math

BINDING = ('scenario', 'supi', 'sessionId', 'podUid', 'ueIp', 'tun', 'upf')

RELEASE = ('trafficStopped', 'policiesAbsent', 'rulesRestored', 'uesReleased', 'podsAbsent')

DIAGNOSTIC_ACTIONS = {'execution_failure', 'traffic_failure'}

NONPOLICY_ACTIONS = {'ue_allocation', 'ue_stop', 'traffic_window', 'traffic_aggregate', 'no_additional_policy', 'zero_workload'} | DIAGNOSTIC_ACTIONS

POLICY_ACTION = 'application_policy'

def timestamp(value):
    if not isinstance(value, str): return None
    try: return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, OverflowError): return None

def fresh(value, now, seconds=15):
    observed = timestamp(value)
    return observed is not None and -1 <= now - observed <= seconds

def released(snapshot):
    return (snapshot.get('stage') == 'completed' and snapshot.get('cleanupStatus') == 'verified'
            and isinstance(snapshot.get('releaseProof'), dict)
            and all(snapshot['releaseProof'].get(key) is True for key in RELEASE))

def native_errors(snapshot, case, now):
    errors = []
    if not isinstance(snapshot.get('roundId'), str) or not snapshot['roundId']: errors.append('Missing round identity')
    if not all(type(snapshot.get(key)) is int and snapshot[key] >= 0 for key in ('generation', 'sequence', 'policyRevision')): errors.append('Missing native revision')
    if not fresh(snapshot.get('observedAt'), now): errors.append('Snapshot evidence stale/future/missing')
    expected = {key: case[key] for key in ('baselineUes', 'events', 'total')}
    if snapshot.get('allocation') != expected: errors.append('Requested allocation differs from frozen case')
    counts = snapshot.get('counts') or {}
    if any(type(counts.get(key)) is not int or counts[key] != case['total'] for key in ('allocated', 'registered', 'pduEstablished', 'trafficVerified')): errors.append('Not all actual UEs verified')
    rows = snapshot.get('perUe')
    if not isinstance(rows, list) or len(rows) != case['total']:
        return errors + ['Incomplete per-UE bindings']
    if any(not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key] for key in BINDING) for row in rows):
        return errors + ['Malformed per-UE identity']
    if len({row['supi'] for row in rows}) != case['total'] or len({row['ueIp'] for row in rows}) != case['total'] or len({(row['podUid'], row['tun']) for row in rows}) != case['total']:
        errors.append('Duplicate SUPI/IP/TUN identity')
    expected_groups = Counter({'web_browsing': case['baselineUes'], **{key: value for key, value in case['events'].items() if value}})
    if Counter(row['scenario'] for row in rows) != expected_groups: errors.append('Scenario ownership is not disjoint/exact')
    proof = snapshot.get('connectivityEvidence')
    if not isinstance(proof, dict): return errors + ['Missing native connectivity stability evidence']
    if (proof.get('source') != 'native-ue-controller' or proof.get('roundId') != snapshot['roundId']
            or type(proof.get('generation')) is not int or proof['generation'] != snapshot['generation']):
        errors.append('Native stability source/round/generation mismatch')
    stable, observed, verified = (timestamp(proof.get(key)) for key in ('stableSince', 'observedAt', 'verifiedAt'))
    # Stability is a historical, identity-bound native gate. It must have been
    # fresh when verified; later real receiver windows have their own freshness.
    if (stable is None or observed is None or verified is None or observed - stable < 60
            or not fresh(proof.get('observedAt'), verified) or verified > now + 1):
        errors.append('Native connectivity did not prove60 stable seconds with fresh evidence')
    identities = proof.get('identities')
    if (not isinstance(identities, list) or len(identities) != len(rows)
            or any(not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key] for key in BINDING) for row in identities)
            or sorted(tuple(row[key] for key in BINDING) for row in identities) != sorted(tuple(row[key] for key in BINDING) for row in rows)):
        errors.append('Native stability identity changed or incomplete')
    return errors

def shard_aggregate_errors(actions, total):
    """Check publication shape before the calibration-specific native audit."""
    aggregates = [a for a in actions if isinstance(a, dict) and a.get('kind') == 'traffic_aggregate']
    if total != 100 and not aggregates:
        return []
    if (total != 100 or len(aggregates) != 2
            or any(not isinstance(a.get('stage'), str) for a in aggregates)
            or {a['stage'] for a in aggregates} != {'measuring_baseline', 'measuring_result'}):
        return ['Missing or unexpected baseline/result shard aggregates']
    windows = [a for a in actions if isinstance(a, dict) and a.get('kind') == 'traffic_window']
    errors, used_ids = [], []
    for aggregate in aggregates:
        ids = aggregate.get('shardActionIds')
        stage = aggregate['stage']
        stage_windows = [w for w in windows if w.get('stage') == stage]
        if (not isinstance(ids, list) or not 2 <= len(ids) <= 10
                or any(not isinstance(identifier, str) or not identifier for identifier in ids)
                or len(stage_windows) != len(ids) or [w.get('id') for w in stage_windows] != ids
                or [w.get('shardIndex') for w in stage_windows] != list(range(len(ids)))
                or any(w.get('shardCount') != len(ids) or w.get('mode') != 'batch'
                       or w.get('accepted') is not True or w.get('installed') is not True
                       or w.get('effectVerified') is not True or w.get('cleanupVerified') is not True
                       for w in stage_windows)
                or aggregate.get('accepted') is not True or aggregate.get('effectVerified') is not True
                or aggregate.get('cleanupVerified') is not True
                or type(aggregate.get('flowCount')) is not int or aggregate['flowCount'] != 2 * total):
            errors.append('Incomplete or altered ' + stage + ' shard aggregate')
        else:
            used_ids.extend(ids)
    if len(used_ids) != len(set(used_ids)) or len(used_ids) != len(windows):
        errors.append('Duplicate or unaggregated shard action')
    return errors

def result_errors(snapshot, case, now):
    errors = native_errors(snapshot, case, now)
    if errors: return errors
    if snapshot.get('stage') != 'results_ready' or snapshot.get('executionOutcome') != 'passed': errors.append('Execution has not produced a passed result')
    if snapshot.get('errors'): errors.append('Backend reports errors')
    owners = {row['supi']: row for row in snapshot['perUe']}
    measurements = snapshot.get('measurements')
    if not isinstance(measurements, list) or len(measurements) != case['total']: return errors + ['Missing per-UE receiver evidence']
    seen = set()
    for row in measurements:
        if not isinstance(row, dict): errors.append('Malformed receiver row'); continue
        owner = owners.get(row.get('supi'))
        if not owner or row.get('supi') in seen or any(row.get(key) != owner[key] for key in BINDING): errors.append('Receiver identity mismatch/duplicate'); continue
        seen.add(row['supi'])
        if any(row.get(key) != snapshot[key] for key in ('roundId', 'generation', 'policyRevision')) or any(type(row.get(key)) is not int for key in ('generation', 'policyRevision')): errors.append('Receiver revision mismatch')
        if row.get('source') != 'receiver' or not fresh(row.get('observedAt'), now): errors.append('Not fresh actual receiver evidence')
        rates = [row.get('receivedUplinkMbps'), row.get('receivedDownlinkMbps')]
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in rates) or not any(type(value) in (int, float) and math.isfinite(value) and value > 0 for value in rates): errors.append('Missing/nonpositive receiver throughput')
        if timestamp(row.get('observedAt')) is not None and timestamp(row['observedAt']) < timestamp(snapshot['connectivityEvidence']['verifiedAt']):
            errors.append('Receiver observation predates native stability verification')
    actions = snapshot.get('actions')
    if not isinstance(actions, list): return errors + ['Malformed policy evidence']
    if any(not isinstance(action, dict) or action.get('kind') not in NONPOLICY_ACTIONS | {POLICY_ACTION} for action in actions):
        errors.append('Unknown audit action kind; policy absence cannot be established')
    if any(isinstance(action, dict) and action.get('kind') in DIAGNOSTIC_ACTIONS for action in actions):
        errors.append('Execution failure diagnostic recorded; not a passed result')
    errors.extend(shard_aggregate_errors(actions, case['total']))
    policies = [action for action in actions if isinstance(action, dict) and action.get('kind') == POLICY_ACTION]
    if case['strategy'] == 'none':
        if policies: errors.append('No-additional-policy trial executed a policy')
    elif any(not isinstance(action.get('id'), str) or not action['id'] or action.get('operation') != 'create_application_policy'
             or any(action.get(key) is not True for key in ('requested', 'accepted', 'installed', 'effectVerified')) for action in policies):
        errors.append('Policy accepted/installed/effect states not all proven')
    if case['strategy'] == 'static' and not policies: errors.append('Static policy effect missing')
    if case['strategy'] == 'ai':
        ai, original = snapshot.get('aiEvidence') or {}, snapshot.get('aiInputSnapshot') or {}
        ai_time, input_time = timestamp(ai.get('observedAt')), timestamp(original.get('observedAt'))
        if (ai.get('provider') != 'openai' or ai.get('status') != 'completed' or not isinstance(ai.get('responseId'), str) or not ai['responseId']
            or any(ai.get(key) != snapshot.get(key) for key in ('roundId', 'generation'))
            or any(original.get(key) != ai.get(key) for key in ('roundId', 'generation', 'policyRevision'))
            or type(ai.get('snapshotSequence')) is not int or original.get('sequence') != ai.get('snapshotSequence')
            or not 0 <= ai['snapshotSequence'] <= snapshot['sequence']
            or ai_time is None or input_time is None or not input_time <= ai_time <= now + 1
            or any(type(ai.get(key)) is not int or type(original.get(key)) is not int for key in ('generation', 'policyRevision'))): errors.append('Missing real AI receipt or input snapshot correlation')
        if not policies and ai.get('noOp') is not True: errors.append('AI has neither verified action nor explicit no-op')
    return errors
