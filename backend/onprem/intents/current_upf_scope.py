"""One attested lifecycle projection per current read, never a second registry."""
from datetime import datetime
import math
import time
import json
from copy import deepcopy

from .commissioned_upfs import CommissionedUpfRegistry,RegistrySnapshot
from .domain import ContractError


class CurrentUpfScope:
    def __init__(self,snapshot,*,descriptors=None,clock=time.time,closing_reader=None,require_service_descriptors=True):
        if type(snapshot) is not RegistrySnapshot:raise ContractError('typed_current_upf_registry_required')
        self.snapshot,self.clock=snapshot,clock
        self._closing_reader=closing_reader
        self._source_clocks=[]
        self.descriptors=deepcopy(descriptors or {})
        snapshot.require_fresh(clock())
        self.rows={r.slot.target_id:r for r in snapshot.observations if r.native_registered}
        if not self.rows:raise ContractError('registered_upf_scope_required')
        for row in self.rows.values():
            b=row.binding
            descriptor=self.descriptors.get(row.slot.target_id,{})
            if (row.slot.evidence_route is None or (require_service_descriptors and not (row.slot.slice_instance_id or descriptor.get('slice_instance_id')))
                    or descriptor.get('native_node_ip',b.native_node_ip)!=b.native_node_ip
                    or (row.slot.template_id and (not b.owner_id or not b.operation_id))):
                raise ContractError('incomplete_current_upf_authority')

    @property
    def revision(self):return self.snapshot.revision

    def finish(self,*,maximum_age_seconds=15):
        """Seal a whole consumer read with CURRENT lifecycle authority.

        Sample timestamps never replace the opening clocks. A nested primitive
        shares this scope; only its outer proof owner performs this closing read.
        """
        if type(maximum_age_seconds) not in (int,float) or not 0<maximum_age_seconds<=15:
            raise ContractError('bounded_original_topology_clock_required')
        self.snapshot.require_fresh(self.clock())
        if not callable(self._closing_reader):raise ContractError('current_upf_closing_attestation_required')
        closing=self._closing_reader()
        if type(closing) is not RegistrySnapshot:raise ContractError('typed_current_upf_registry_required')
        closing.require_fresh(self.clock())
        if closing.revision!=self.revision:raise ContractError('current_upf_registry_changed_during_observation')
        self.snapshot.require_fresh(self.clock())
        clocks=[self.snapshot.observed_at,*[r.binding.observed_at for r in self.snapshot.observations if r.binding],*self._source_clocks]
        if any(not 0<=self.clock()-stamp<=maximum_age_seconds for stamp in clocks):
            raise ContractError('original_native_upf_source_clock_expired')
        return self.snapshot

    def bindings(self):
        return {name:{'native_node_ip':r.binding.native_node_ip,'native_upf_id':r.binding.native_upf_id,
            'slice_instance_id':r.slot.slice_instance_id or self.descriptors[name]['slice_instance_id'],'kernel_epoch':r.binding.kernel_epoch,
            'pod_uid':r.binding.pod_uid,'owner_id':r.binding.owner_id,'operation_id':r.binding.operation_id,
            'version':r.binding.version} for name,r in self.rows.items()}

    def validate_census(self,value):
        self.snapshot.require_fresh(self.clock())
        rows=value.get('upfs')
        if (value.get('source')!='native-smf-active-sessions' or value.get('complete') is not True
                or not isinstance(rows,list) or len(rows)!=len(self.rows)):
            raise ContractError('complete_registered_upf_census_required')
        stamp=value.get('observed_at')
        if type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0<=self.clock()-stamp<=15:
            raise ContractError('native_upf_census_stale')
        self._source_clocks.append(stamp)
        seen=set()
        for native in rows:
            matches=[r for r in self.rows.values() if r.binding.native_node_ip==native.get('node_ip')]
            if len(matches)!=1:raise ContractError('unregistered_native_upf')
            row=matches[0];b=row.binding
            if row.slot.target_id in seen:raise ContractError('duplicate_native_upf')
            seen.add(row.slot.target_id)
            if (native.get('upf_id'),native.get('upf_epoch'),value.get('smf_boot_epoch'))!=(
                    b.native_upf_id,b.kernel_epoch,b.smf_boot_epoch):
                raise ContractError('native_upf_incarnation_changed')
            if native.get('target_id')!=row.slot.target_id:raise ContractError('native_upf_target_changed')
            authority_keys={'owner_id','operation_id'} & native.keys()
            if authority_keys and authority_keys!={'owner_id','operation_id'}:
                raise ContractError('partial_native_upf_lifecycle_authority')
            for key,expected in (('owner_id',b.owner_id),('operation_id',b.operation_id),('version',b.version)):
                if key in native and native[key]!=expected:raise ContractError('native_upf_lifecycle_authority_changed')
            stamp=native.get('observed_at')
            if type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0<=self.clock()-stamp<=15:
                raise ContractError('native_upf_census_stale')
            self._source_clocks.append(stamp)
        return value

    def kernel(self,target,client,*,cleanup=False):
        """Trusted Pod-proxy route, UID fenced before and after the native dump."""
        if cleanup:
            matches=[r for r in self.snapshot.observations if r.slot.target_id==target
                and not r.native_registered and r.binding is not None and r.binding.owner_id and r.binding.operation_id]
            if len(matches)!=1:raise ContractError('exact_inactive_owned_cleanup_binding_required')
            row=matches[0]
        else:row=self.rows[target]
        route=row.slot.evidence_route;b=row.binding
        if (client.context,client.namespace)!=(route.context,route.namespace):
            raise ContractError('upf_evidence_route_scope_changed')
        def pod():
            value=client.call('get','pod',route.pod_name,'-o','json')
            if (value.get('metadata',{}).get('uid')!=b.pod_uid
                    or value['metadata'].get('name')!=route.pod_name
                    or value['metadata'].get('namespace')!=route.namespace
                    or value['metadata'].get('deletionTimestamp')):
                raise ContractError('upf_evidence_pod_changed')
            return value
        pod()
        result=client.runner(['kubectl','--context',client.context,'--namespace',client.namespace,
            'get','--raw',route.path],capture_output=True,text=True,timeout=client.timeout+2,check=False)
        if result.returncode or len(result.stdout.encode())>8*1024*1024:
            raise ContractError('bounded_current_upf_kernel_read_failed')
        try:dump=json.loads(result.stdout)
        except ValueError:raise ContractError('current_upf_kernel_response_invalid') from None
        pod()
        try:
            stamp=datetime.fromisoformat(dump['observedAt'].replace('Z','+00:00'))
            if stamp.tzinfo is None:raise ValueError()
            stamp=stamp.timestamp()
        except (KeyError,ValueError,TypeError):raise ContractError('upf_kernel_clock_missing') from None
        if (dump.get('schemaVersion')!='cityverse.upf/v1' or dump.get('source')!='gtp5g-netlink'
                or dump.get('upf')!=target or dump.get('available') is not True or dump.get('errors')!=[]
                or dump.get('epoch')!=b.kernel_epoch or not isinstance(dump.get('sessions'),list)
                or not isinstance(dump.get('residuals'),list) or not 0<=self.clock()-stamp<=15):
            raise ContractError('current_registered_upf_kernel_unavailable')
        self.snapshot.require_fresh(self.clock())
        self._source_clocks.append(stamp)
        return dump

    def validate_kernels(self,census,dumps):
        self.validate_census(census)
        if set(dumps)!=set(self.rows):raise ContractError('partial_registered_upf_kernels')
        for target,row in self.rows.items():
            expected=[str(s['pfcpSeid']) for s in census['sessions'] if s.get('upf_id')==row.binding.native_upf_id]
            actual=[str(s['pfcpSeid']) for s in dumps[target]['sessions']]
            if (len(set(actual))!=len(actual) or len(set(expected))!=len(expected)
                    or set(actual)!=set(expected) or dumps[target]['residuals']):
                raise ContractError('registered_upf_kernel_census_differs')


class CurrentUpfSource:
    """Accept only existing authoritative lifecycle producers, never static config snapshots."""
    def __init__(self,registry,*,descriptors=None,clock=time.time,require_service_descriptors=True):
        from .upf_controller import UpfLifecycleController
        from .resource_adapters import FixedOriginUpfControlApi
        if type(registry) is not CommissionedUpfRegistry or not isinstance(
                getattr(registry.reader,'__self__',None),(UpfLifecycleController,FixedOriginUpfControlApi)):
            raise ContractError('authoritative_current_upf_registry_required')
        self.registry,self.clock=registry,clock
        if type(require_service_descriptors) is not bool:raise ValueError('Explicit descriptor scope required')
        self.require_service_descriptors=require_service_descriptors
        self.descriptors=deepcopy(descriptors or {})

    def proof(self):return CurrentUpfScope(self.registry.snapshot(),descriptors=self.descriptors,clock=self.clock,
        closing_reader=self.registry.snapshot,require_service_descriptors=self.require_service_descriptors)


def configured_current_upfs(config,*,clock=time.time):
    values=config.get('commissioned_upfs')
    if not values:return None
    if values!={'enabled':True}:raise ContractError('invalid_current_upf_consumer_configuration')
    from .resource_adapters import FixedOriginUpfControlApi,NativeControllerClient,_token_file_reader
    native=config.get('providers',{}).get('upf',{})
    if not native.get('origin') or not native.get('token_file'):
        raise ContractError('current_upf_registry_controller_not_configured')
    api=FixedOriginUpfControlApi(NativeControllerClient(native['origin'],
        token_reader=_token_file_reader(native['token_file']),timeout=min(native.get('timeout',10),15)))
    return CurrentUpfSource(CommissionedUpfRegistry(api.current_registry),
        descriptors=config.get('session_inventory',{}).get('upf_bindings',{}),clock=clock)
