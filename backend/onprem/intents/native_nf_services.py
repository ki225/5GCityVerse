"""Complete current native NF membership, joined to trusted service contracts."""
from __future__ import annotations

from copy import deepcopy
import json
import math
import time

from .actuators import ActuationBlocked
from .domain import SessionBinding,SessionKey
from .membership import MembershipResolver
from .native_nf_load import NativeUpfKernelEpochReader,_name
from .native_session_inventory import NativeSessionBindingReader


class NativeNfServiceInventoryReader:
    """Unknown native users fail closed; city membership never limits the census.

    Only UPFs with exact Pod/UID, native SMF identity and independent kernel
    epoch commissioning are supported. Other NF types remain unconfigured.
    """
    def __init__(self,client,binding_reader,*,targets,raw_reader,active_intents,
                 epoch_reader=None,clock=time.time,maximum_age_seconds=15,registry_container=None,
                 registry_containers=None):
        if not isinstance(binding_reader,NativeSessionBindingReader) or not callable(raw_reader) or not callable(active_intents):
            raise ActuationBlocked('Configured native binding and current intent readers required')
        self.client,self.bindings,self.targets=client,binding_reader,deepcopy(targets)
        self.raw_reader,self.active_intents,self.clock=raw_reader,active_intents,clock
        self.maximum_age=maximum_age_seconds
        self.current_upfs=binding_reader.current_upfs
        self.registry_container=registry_container
        self.registry_containers=deepcopy(registry_containers or {})
        if (not isinstance(self.registry_containers,dict) or len(self.registry_containers)>64):
            raise ActuationBlocked('Bounded exact registry container scope required')
        for target,container in self.registry_containers.items():
            _name(target);_name(container)
        if self.current_upfs is not None and not self.registry_containers:_name(registry_container)
        if (not self.targets or len(self.targets)>64 or type(maximum_age_seconds) not in (int,float)
                or not math.isfinite(maximum_age_seconds) or not 0<maximum_age_seconds<=15):
            raise ActuationBlocked('Bounded exact NF service inventory scope required')
        for pod,spec in self.targets.items():
            _name(pod);_name(spec['container'])
            if (set(spec)!={'target_id','pod_uid','container','evidence_service','native_upf_name'}
                    or spec['target_id'] not in self.bindings.upfs or not spec['pod_uid']):
                raise ActuationBlocked('Exact commissioned UPF service identity required')
        self.epoch_reader=epoch_reader or NativeUpfKernelEpochReader(client,self.targets,clock=clock,
            maximum_age_seconds=maximum_age_seconds)

    def _pod(self,name,spec):
        pod=self.client.call('get','pod',name,'-o','json')
        states=[s for s in pod.get('status',{}).get('containerStatuses',()) if s.get('name')==spec['container']]
        if (pod.get('metadata',{}).get('name')!=name or pod['metadata'].get('namespace')!=self.client.namespace
                or pod['metadata'].get('uid')!=spec['pod_uid'] or pod['metadata'].get('deletionTimestamp')
                or len(states)!=1 or states[0].get('ready') is not True
                or 'running' not in states[0].get('state',{}) or not states[0].get('containerID')
                or type(states[0].get('restartCount')) is not int):
            raise ActuationBlocked('Exact running NF generation required')
        return pod,(pod['metadata']['uid'],states[0]['containerID'],states[0]['restartCount'])

    @staticmethod
    def _seal(inventory):
        return (inventory.get('smf_boot_epoch'),inventory.get('census_revision'),
            sorted(json.dumps(r,sort_keys=True,allow_nan=False) for r in inventory.get('sessions',())),
            sorted(json.dumps({k:v for k,v in r.items() if k!='observed_at'},sort_keys=True,allow_nan=False)
                for r in inventory.get('upfs',())))

    def _scope_targets(self, scope):
        """Keep exact existing container commissioning within live lifecycle scope."""
        targets = {}
        for name, row in scope.rows.items():
            pod = row.slot.pod_name
            known = self.targets.get(pod)
            if known is not None:
                if known['target_id'] != name or known['pod_uid'] != row.binding.pod_uid:
                    raise ActuationBlocked('Commissioned NF identity differs from lifecycle registry')
                container = known['container']
            else:
                container = self.registry_containers.get(name, self.registry_container)
                _name(container)
            targets[pod] = {'target_id': name, 'pod_uid': row.binding.pod_uid,
                            'container': container, 'native_upf_name': name}
        return targets

    def __call__(self,pod_name):
        try:
            scope=self.current_upfs.proof() if self.current_upfs is not None else None
            upfs=scope.bindings() if scope is not None else self.bindings.upfs
            targets=(self._scope_targets(scope)
                if scope is not None else self.targets)
            spec=targets[pod_name]
            def epoch_read(name,pod):
                if scope is None:return self.epoch_reader(name,pod)
                dump=scope.kernel(spec['target_id'],self.client)
                from datetime import datetime
                return {'source':dump['source'],'pod_uid':pod['metadata']['uid'],'upf_epoch':dump['epoch'],
                    'observed_at':datetime.fromisoformat(dump['observedAt'].replace('Z','+00:00')).timestamp()}
            before,identity=self._pod(pod_name,spec)
            epoch=epoch_read(pod_name,before)
            raw=self.raw_reader()
            inventory=self.bindings.api.active_inventory()
            # A completed round may retain its last UE identities after native
            # cleanup. Native absence removes those stale ledger references;
            # every native identity still must have a current trusted owner.
            identities={(r['supi'],str(r['sessionId'])) for r in inventory.get('sessions',())}
            raw=dict(raw,perUe=[r for r in raw.get('perUe',()) if (r.get('supi'),str(r.get('sessionId'))) in identities])
            if not identities and raw.get('schemaVersion')=='cityverse.current-state/v2':
                # Only a whole empty native census can retire stale current
                # device references here. Nonempty/partial device joins retain
                # their full proof contract and fail closed on missing PDUs.
                raw.update(devices=[],perSession=[])
            # Reuse all native census/classification validation without issuing
            # another unsealed census inside the binding reader.
            class PinnedApi:
                def active_inventory(self):return inventory
            reader=NativeSessionBindingReader(PinnedApi(),network_scope=self.bindings.scope,
                smf_id=self.bindings.smf_id,upf_bindings=upfs,catalogue=self.bindings.catalogue,
                classification_reader=self.bindings.classification_reader,
                maximum_age_seconds=self.maximum_age,clock=self.clock)
            batch=reader.current_bindings(raw,upf_scope=scope)
            sessions=[]
            for value in batch.values():
                binding=dict(value['binding'])
                binding['key']=SessionKey(**binding['key'])
                sessions.append(SessionBinding(**binding))
            native_keys={row['session_key'] for row in inventory['sessions']}
            if {row.key.token for row in sessions}!=native_keys:
                raise ActuationBlocked('Unclassified foreign native NF users remain')
            contracts=tuple(self.active_intents())
            if len(contracts)>10000:
                raise ActuationBlocked('Active service contract scope exceeds bound')
            identifiers={row.key.token:set() for row in sessions}
            for intent in contracts:
                if not intent.is_active(self.clock()):continue
                membership=MembershipResolver().resolve(intent,tuple(sessions))
                if membership.rejected:
                    raise ActuationBlocked('Active native application membership is not classifiable')
                for member in membership.members:
                    identifiers[member.session_token].add(intent.intent_id)
            selected=[]
            for row in sessions:
                if row.upf_id!=spec['target_id']:continue
                ids=identifiers[row.key.token] or {'service-type:'+row.service_type}
                selected.append(dict(session_key=row.key.token,service_ids=sorted(ids),service_type=row.service_type))
            native_upfs=[row for row in inventory['upfs']
                if row['node_ip']==upfs[spec['target_id']]['native_node_ip']]
            closing=self.bindings.api.active_inventory()
            if scope is not None:scope.validate_census(closing)
            after,current=self._pod(pod_name,spec)
            final_epoch=epoch_read(pod_name,after)
            times=(inventory['observed_at'],epoch['observed_at'],final_epoch['observed_at'])
            if any(type(at) not in (int,float) or not math.isfinite(at)
                    or not 0<=self.clock()-at<=self.maximum_age for at in times):
                raise ActuationBlocked('Native NF observation clocks are unavailable')
            stamp=min(*times,scope.snapshot.observed_at) if scope is not None else min(times)
            closing_at=closing.get('observed_at')
            if (identity!=current or len(native_upfs)!=1 or self._seal(inventory)!=self._seal(closing)
                    or closing.get('source')!='native-smf-active-sessions'
                    or closing.get('complete') is not True or closing.get('network_scope')!=self.bindings.scope
                    or type(closing_at) not in (int,float) or not math.isfinite(closing_at)
                    or not 0<=self.clock()-closing_at<=self.maximum_age
                    or epoch.get('source')!='gtp5g-netlink' or final_epoch.get('source')!='gtp5g-netlink'
                    or epoch.get('pod_uid')!=identity[0] or final_epoch.get('pod_uid')!=identity[0]
                    or not epoch.get('upf_epoch') or epoch['upf_epoch']!=final_epoch.get('upf_epoch')
                    or epoch['upf_epoch']!=native_upfs[0]['upf_epoch']
                    or type(stamp) not in (int,float) or not math.isfinite(stamp)
                    or not 0<=self.clock()-stamp<=self.maximum_age):
                raise ActuationBlocked('NF identity or full native census changed during classification')
            if scope is not None:scope.finish()
            return dict(source='native-smf-active-sessions',complete=True,pod_uid=identity[0],
                upf_epoch=epoch['upf_epoch'],smf_boot_epoch=inventory['smf_boot_epoch'],
                observed_at=stamp,census_revision=inventory['census_revision'],sessions=selected,
                **({'registry_revision':scope.revision} if scope is not None else {}),
                classification_scope='complete-native-census; active intent IDs or commissioned service type')
        except ActuationBlocked:
            raise
        except Exception:
            raise ActuationBlocked('Complete current native NF service inventory unavailable') from None
