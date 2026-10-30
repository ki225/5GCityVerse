"""Offline fail-closed tests; no live browser, UE, policy, or network access."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('scenario_matrix', Path(__file__).parents[1] / 'run_browser_scenario_matrix.py')
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)
NOW = 1790553600.0
ISO = datetime.fromtimestamp(NOW, timezone.utc).isoformat()


def capability():
    return dict(start=True, staticPolicy=True, aiPolicy=True, aiAuthReady=True, aiAuthObservedAt=ISO)


def observation(strategy='none'):
    case = matrix.matrix_cases(6)[matrix.STRATEGIES.index(strategy)]
    bindings = [dict(scenario='web_browsing', supi=f'imsi-{i}', sessionId='1', podUid='pod1', ueIp=f'10.101.0.{i+1}', tun=f'uesimtun{i}', upf='upf-embb') for i in range(6)]
    value = dict(roundId='round1', generation=1, policyRevision=0, sequence=30, observedAt=ISO,
                 stage='results_ready', executionOutcome='passed', cleanupStatus='pending',
                 allocation={key: case[key] for key in ('baselineUes','events','total')},
                 counts=dict(allocated=6, registered=6, pduEstablished=6, trafficVerified=6), perUe=bindings,
                 measurements=[dict(**row, roundId='round1', generation=1, policyRevision=0, observedAt=ISO, source='receiver', receivedUplinkMbps=1., receivedDownlinkMbps=2.) for row in bindings],
                 actions=[], errors=[], capabilities=capability())
    value['connectivityEvidence'] = dict(source='native-ue-controller', roundId='round1', generation=1,
        stableSince=datetime.fromtimestamp(NOW-60,timezone.utc).isoformat(), observedAt=ISO,
        verifiedAt=ISO, identities=deepcopy(bindings))
    if strategy == 'static': value['actions'] = [dict(id='a1', kind='application_policy', operation='create_application_policy', requested=True, accepted=True, installed=True, effectVerified=True)]
    if strategy == 'ai':
        value['aiInputSnapshot'] = dict(roundId='round1', generation=1, policyRevision=0, sequence=10, observedAt=ISO)
        value['aiEvidence'] = dict(provider='openai',status='completed',responseId='actual-response-id',roundId='round1',generation=1,policyRevision=0,snapshotSequence=10,observedAt=ISO,noOp=True)
    return case, value


def test_96_cases_disjoint_groups_and_frozen_strategy_allocation():
    cases = matrix.matrix_cases()
    assert len(cases) == len({row['id'] for row in cases}) == 96
    assert {row['mask'] for row in cases} == set(range(32))
    for mask in range(32):
        rows = cases[mask*3:mask*3+3]
        assert [row['strategy'] for row in rows] == ['none','static','ai']
        assert all(row['baselineUes'] > 0 and row['baselineUes'] + sum(row['events'].values()) == 50 for row in rows)
        assert rows[0]['events'] == rows[1]['events'] == rows[2]['events']
        assert len({row['baselineUes'] for row in rows}) == 1
        assert [event for event,count in rows[0]['events'].items() if count] == [event for i,event in enumerate(matrix.EVENTS) if mask & (1<<i)]


@pytest.mark.parametrize('value',[0,1,5,51,-1,True,6.5])
def test_bad_matrix_total_rejected(value):
    with pytest.raises(ValueError): matrix.matrix_cases(value)


def test_default_is_offline_plan_not_pass(monkeypatch,tmp_path):
    monkeypatch.setattr(matrix,'read_snapshot',lambda *args: pytest.fail('Default must not access network'))
    matrix.main(['--output',str(tmp_path/'plan')])
    summary=json.loads((tmp_path/'plan/summary.json').read_text())
    assert summary['status']=='not_executed' and summary['passed']==0
    assert len(summary['cases'])==96 and all(row['status']=='unrun' for row in summary['cases'].values())


def test_execute_missing_capability_blocks_before_browser(monkeypatch,tmp_path):
    monkeypatch.setattr(matrix,'read_snapshot',lambda *args: dict(stage='idle',capabilities={}))
    with pytest.raises(RuntimeError,match='Capability unavailable'):
        matrix.main(['--execute','--output',str(tmp_path/'blocked')])
    summary=json.loads((tmp_path/'blocked/summary.json').read_text())
    assert summary['status']=='blocked' and summary['passed']==0
    assert all(row['status']=='unrun' for row in summary['cases'].values())


def test_configured_ai_not_auth_and_old_auth_fails_initial_gate():
    value=dict(stage='idle',capabilities=dict(start=True,staticPolicy=True,aiPolicy=True,configured=True))
    assert matrix.preflight_errors(value,NOW)
    value['capabilities']=capability()
    assert matrix.preflight_errors(value,NOW)==[]
    assert matrix.preflight_errors(value,NOW+301)
    assert matrix.preflight_errors(value,NOW-2)


def test_prior_round_requires_every_release_proof_not_global_smf_zero():
    value=dict(stage='completed',cleanupStatus='verified',capabilities=capability(),releaseProof=dict.fromkeys(matrix.RELEASE,True))
    assert matrix.preflight_errors(value,NOW)==[]
    for key in matrix.RELEASE:
        changed=deepcopy(value); changed['releaseProof'][key]=False
        assert matrix.preflight_errors(changed,NOW)
    value['stage']='results_ready'
    assert not matrix.released(value)


@pytest.mark.parametrize('strategy',matrix.STRATEGIES)
def test_complete_receivers_and_separate_policy_states_pass(strategy):
    case,value=observation(strategy)
    assert matrix.result_errors(value,case,NOW)==[]


@pytest.mark.parametrize('change',[
    {'source':'ping'}, {'roundId':'other'}, {'generation':True}, {'policyRevision':2},
    {'ueIp':'10.1.1.1'}, {'podUid':'other'}, {'tun':'different'}, {'supi':'other'},
    {'scenario':'realtime_control'}, {'observedAt':None}, {'receivedUplinkMbps':True},
    {'receivedUplinkMbps':float('nan')}, {'receivedUplinkMbps':None,'receivedDownlinkMbps':None},
    {'receivedDownlinkMbps':None},
])
def test_bad_receiver_cannot_pass(change):
    case,value=observation();value['measurements'][0].update(change)
    assert matrix.result_errors(value,case,NOW)


def test_stale_future_partial_duplicate_or_boolean_native_counts_fail():
    case,value=observation()
    assert matrix.result_errors(value,case,NOW+16)
    assert matrix.result_errors(value,case,NOW-2)
    for change in (dict(perUe=value['perUe'][:-1]),dict(measurements=value['measurements'][:-1]),dict(counts=dict(allocated=True)),dict(perUe=[value['perUe'][0]]*6)):
        assert matrix.result_errors({**value,**change},case,NOW)


def test_http_accepted_or_installed_without_effect_is_not_pass():
    case,value=observation('static')
    value['actions'][0]['effectVerified']=False
    assert matrix.result_errors(value,case,NOW)
    value['actions']=[]
    assert matrix.result_errors(value,case,NOW)


def test_real_ai_receipt_and_exact_input_revision_required_no_forced_improvement():
    case,value=observation('ai')
    assert matrix.result_errors(value,case,NOW)==[]  # genuine documented no-op
    for change in ({'responseId':None},{'provider':'fallback'},{'snapshotSequence':11},{'roundId':'other'},{'noOp':False},{'generation':True},{'observedAt':datetime.fromtimestamp(NOW+2,timezone.utc).isoformat()}):
        changed=deepcopy(value);changed['aiEvidence'].update(change)
        assert matrix.result_errors(changed,case,NOW)


def test_failed_execution_cleanup_does_not_become_pass(tmp_path,monkeypatch):
    _,value=observation();value.update(stage='completed',executionOutcome='failed',cleanupStatus='verified',releaseProof=dict.fromkeys(matrix.RELEASE,True))
    monkeypatch.setattr(matrix,'read_snapshot',lambda *args:value)
    assert matrix.wait_snapshot(None,'unused',tmp_path,matrix.released,120,'round1',allow_failed=True)['executionOutcome']=='failed'
    with pytest.raises(RuntimeError,match='execution failed'):
        matrix.wait_snapshot(None,'unused',tmp_path,matrix.released,120,'round1')


def test_superseded_round_refused_even_when_other_round_completed(tmp_path,monkeypatch):
    monkeypatch.setattr(matrix,'read_snapshot',lambda *args:dict(roundId='other',sequence=10,stage='completed'))
    with pytest.raises(RuntimeError,match='superseded'):
        matrix.wait_snapshot(None,'unused',tmp_path,lambda s:True,120,'owned')


def at(value, seconds):
    result=deepcopy(value)
    stamp=datetime.fromtimestamp(NOW+seconds,timezone.utc).isoformat()
    result['observedAt']=stamp
    for row in result['measurements']: row['observedAt']=stamp
    return result


def test_native_stability_proof_then_single_fresh_receiver_window():
    case,value=observation();window=matrix.EvidenceWindow()
    assert window.observe(value,case,NOW)
    assert not window.observe(value,case,NOW+16)  # Frozen results do not become fresh on polling.
    assert window.observe(at(value,60),case,NOW+60)  # New real windows retain the same historical native gate.
    short=deepcopy(value)
    short['connectivityEvidence']['stableSince']=datetime.fromtimestamp(NOW-59,timezone.utc).isoformat()
    assert not window.observe(short,case,NOW)


@pytest.mark.parametrize('mutation',['podUid','generation','roundId','missing','source','stale_at_capture','future','missing_identity'])
def test_stability_proof_requires_exact_identity_and_fresh_native_capture(mutation):
    case,value=observation()
    proof=value['connectivityEvidence']
    if mutation=='podUid':
        value['perUe'][0]['podUid']='replacement'
        value['measurements'][0]['podUid']='replacement'
    elif mutation=='generation': proof['generation']=2
    elif mutation=='roundId': proof['roundId']='other'
    elif mutation=='missing': value.pop('connectivityEvidence')
    elif mutation=='source': proof['source']='simulation'
    elif mutation=='stale_at_capture':
        proof['observedAt']=datetime.fromtimestamp(NOW-16,timezone.utc).isoformat()
        proof['stableSince']=datetime.fromtimestamp(NOW-76,timezone.utc).isoformat()
    elif mutation=='future': proof['verifiedAt']=datetime.fromtimestamp(NOW+2,timezone.utc).isoformat()
    else: proof['identities'].pop()
    assert not matrix.EvidenceWindow().observe(value,case,NOW)


def test_none_allows_only_known_orchestration_audit_kinds():
    case,value=observation()
    value['actions']=[dict(id=kind,kind=kind,requested=True,accepted=False,installed=False,effectVerified=False)
                      for kind in matrix.NONPOLICY_ACTIONS - matrix.DIAGNOSTIC_ACTIONS - {'traffic_aggregate'}]
    assert matrix.result_errors(value,case,NOW)==[]
    for kind in ('arbitrary_policy',None,'application_policy','traffic_aggregate'):
        changed=deepcopy(value)
        changed['actions'].append(dict(id='unexpected',kind=kind))
        assert matrix.result_errors(changed,case,NOW)


def test_100_ue_shard_aggregates_fail_closed_on_missing_duplicate_or_wrong_stage():
    actions = []
    for stage in ('measuring_baseline', 'measuring_result'):
        ids = [f'{stage}-{index}' for index in range(5)]
        actions.extend({'kind': 'traffic_window', 'id': identifier, 'stage': stage,
            'shardIndex': index, 'shardCount': 5, 'mode': 'batch',
            'accepted': True, 'installed': True, 'effectVerified': True, 'cleanupVerified': True}
            for index, identifier in enumerate(ids))
        actions.append({'kind': 'traffic_aggregate', 'stage': stage, 'shardActionIds': ids,
            'flowCount': 200, 'accepted': True, 'effectVerified': True, 'cleanupVerified': True})
    assert matrix.shard_aggregate_errors(actions, 100) == []
    for mutation in ('missing-aggregate', 'missing-shard', 'duplicate-id', 'wrong-stage', 'unfinished-shard'):
        changed = deepcopy(actions)
        if mutation == 'missing-aggregate': changed.pop()
        elif mutation == 'missing-shard': changed.pop(0)
        elif mutation == 'duplicate-id': changed[-1]['shardActionIds'][1] = changed[-1]['shardActionIds'][0]
        elif mutation == 'wrong-stage': changed[0]['stage'] = 'foreign'
        else: changed[0]['cleanupVerified'] = False
        assert matrix.shard_aggregate_errors(changed, 100), mutation


@pytest.mark.parametrize('kind', ['execution_failure', 'traffic_failure'])
@pytest.mark.parametrize('strategy', matrix.STRATEGIES)
@pytest.mark.parametrize('outcome', ['failed', 'passed'])
def test_failure_diagnostics_are_known_nonpolicy_but_never_a_pass(kind, strategy, outcome):
    case, value = observation(strategy)
    value['executionOutcome'] = outcome
    value['actions'].append(dict(id='diagnostic', kind=kind, stage='measuring_baseline',
                                 code='batch.worker_failed', errorType='PolicyBlocked', observedAt=ISO))
    errors = matrix.result_errors(value, case, NOW)
    assert kind in matrix.NONPOLICY_ACTIONS
    assert 'Execution failure diagnostic recorded; not a passed result' in errors
    assert not any('Unknown audit action kind' in error for error in errors)
    assert not any('trial executed a policy' in error for error in errors)
    assert not matrix.EvidenceWindow().observe(value, case, NOW)
    if outcome == 'failed':
        assert 'Execution has not produced a passed result' in errors


def test_static_and_ai_ignore_audit_but_require_actual_policy_or_noop():
    for strategy in ('static','ai'):
        case,value=observation(strategy)
        value['actions'].append(dict(id='traffic',kind='traffic_window',effectVerified=True))
        assert matrix.result_errors(value,case,NOW)==[]
        value['actions']=[dict(id='traffic',kind='traffic_window',effectVerified=True)]
        if strategy=='ai': value['aiEvidence']['noOp']=False
        assert matrix.result_errors(value,case,NOW)


def test_network_preflight_failure_saved_as_blocked_not_pass(monkeypatch,tmp_path):
    def unavailable(*args): raise TimeoutError()
    monkeypatch.setattr(matrix,'read_snapshot',unavailable)
    with pytest.raises(TimeoutError): matrix.main(['--execute','--output',str(tmp_path/'offline')])
    summary=json.loads((tmp_path/'offline/summary.json').read_text())
    assert summary['status']=='blocked' and summary['passed']==0
def test_pre_established_cleanup_is_round_release_not_fleet_release():
    from scripts.onprem.run_browser_scenario_matrix import round_resources_released, released
    snapshot = {"stage": "completed", "cleanupStatus": "verified", "releaseProof": {
        "scope": "pre-established-round-resources-only", "trafficStopped": True,
        "policiesAbsent": True, "rulesRestored": True, "ueLeaseReleased": True,
        "fleetPreserved": True, "uesReleased": False, "podsAbsent": False}}
    assert round_resources_released(snapshot) is True
    assert released(snapshot) is False
    snapshot["releaseProof"]["policiesAbsent"] = False
    assert round_resources_released(snapshot) is False

