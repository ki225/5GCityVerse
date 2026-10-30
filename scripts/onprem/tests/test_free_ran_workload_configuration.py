"""Pure commissioning/formal catalog and explicit100 demand contracts."""
from copy import deepcopy

import pytest

from scripts.onprem.free_ran_device_smoke import configured_workload_compiler, EvidenceUnavailable
from backend.onprem.traffic.session_workload import SessionWorkloadError, session_rates
from backend.onprem.traffic.admission import admit_traffic, TrafficAdmissionBlocked
from backend.onprem.traffic.workload import workload_metadata_for_snapshot, applied_rates, protocol_options


CATALOG={'baseline':{'service_type':'baseline','region':'residential'}}

def demand():
    return {'schemaVersion':'cityverse.service-requirements/v2','services':{
        'baseline':{'traffic':{'protocol':'udp','offeredUplinkMbpsPerUe':.1,'offeredDownlinkMbpsPerUe':.1}}}}

def owners():
    return [dict(identity=f'explicit-device-{i}/native-PDU',ue_id=f'imsi-{208930100000001+i}',
        service_type='baseline',region='residential',slice='embb') for i in range(100)]

def body():
    return dict(baselineUes=100,events={k:0 for k in ('video_streaming','realtime_control','connected_traffic','realtime_control','factory_iot')},
                trafficMode='qos',serviceRequirements=demand())


def test_canary_uses_formal_catalog_and_ignores_old_workload_mapping():
    compiler=configured_workload_compiler({'service_catalog':CATALOG,
        'session_workload':{'functional_combined_budget_kbps':8000,'service_mapping':{'foreign':{}}}})
    result=compiler.compile(owners()[:1])
    assert result['requestedCombinedKbps']==result['appliedCombinedKbps']==2000
    assert result['capReason'] is None
    fallback=configured_workload_compiler({'ue_classification':{'service_mapping':CATALOG}})
    assert fallback.compile(owners()[:1])['appliedCombinedKbps']==2000


@pytest.mark.parametrize('config',[{}, {'service_catalog':{}}, {'session_workload':{'service_mapping':CATALOG}},
    {'service_catalog':CATALOG,'session_workload':[]}])
def test_missing_catalog_and_wrong_budget_shape_still_reject(config):
    with pytest.raises((SessionWorkloadError,EvidenceUnavailable)):configured_workload_compiler(config)


@pytest.mark.parametrize('functional_budget',[8000,20000])
def test_explicit_100_qos_demand_preserves_100kbps_each_direction(functional_budget):
    compiler=configured_workload_compiler({'service_catalog':CATALOG,
        'session_workload':{'functional_combined_budget_kbps':functional_budget}})
    request=body()
    before=deepcopy(request)
    admit_traffic(request,{'CITYVERSE_ONPREM_QOS_MAX_OFFERED_KBPS':'10000'})
    result=compiler.compile(owners(),traffic_mode='qos',service_requirements=request['serviceRequirements'])
    assert result['requestedUplinkKbps']==result['appliedUplinkKbps']==10000
    assert result['requestedDownlinkKbps']==result['appliedDownlinkKbps']==10000
    assert result['requestedCombinedKbps']==result['appliedCombinedKbps']==20000
    assert len(session_rates(result))==100 and set(tuple(r.values()) for r in session_rates(result).values())=={(100,100)}
    assert result['capReason'] is None and request==before
    snapshot=dict(request,allocation={'baselineUes':100,'events':request['events'],'total':100},
                  resolvedRequirements=result['resolvedRequirements'],
                  perUe=[dict(supi=r['ue_id'],scenario='web_browsing',slice='embb') for r in owners()])
    native_plan=workload_metadata_for_snapshot(snapshot)
    assert len(applied_rates(native_plan))==100 and set(tuple(r.values()) for r in applied_rates(native_plan).values())=={(100,100)}
    assert protocol_options(native_plan)=={'traffic_mode':'qos'}
    assert native_plan['appliedCombinedKbps']==20000 and native_plan['capReason'] is None


def test_100_qos_demand_above_explicit_generator_ceiling_rejects_without_scaling():
    request=body()
    with pytest.raises(TrafficAdmissionBlocked):admit_traffic(request,{'CITYVERSE_ONPREM_QOS_MAX_OFFERED_KBPS':'9999'})
    request['serviceRequirements']['services']['baseline']['traffic']['offeredDownlinkMbpsPerUe']=.101
    with pytest.raises(TrafficAdmissionBlocked):admit_traffic(request,{'CITYVERSE_ONPREM_QOS_MAX_OFFERED_KBPS':'10000'})
