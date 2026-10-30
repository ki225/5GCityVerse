"""Current launched free-ran UE configuration proof without reading AKA material."""
from __future__ import annotations
import hashlib
import json
import re
import math
from datetime import datetime
from .domain import ContractError
from .service_classification import configured_profile


class NativeUeClassificationReader:
    """One Pod list per snapshot; exact generation/UID, pinned code and image fences.

    The fixed init script renders a private config on a memory volume. We verify
    that actual init command and successful exit, never read the credential file
    or return authentication fields. The native SMF census supplies actual PDU ID.
    """
    def __init__(self, client, *, generation_reader, service_mapping, image,
                 init_image, configuration_source):
        self.client,self.generation_reader=client,generation_reader
        self.services=dict(service_mapping)
        self.image,self.init_image=image,init_image
        self.script=configuration_source
        self.digest=hashlib.sha256(configuration_source.encode()).hexdigest()
        for ref in (image,init_image):
            if not re.fullmatch(r'[^@]+@sha256:[0-9a-f]{64}',ref):
                raise ContractError('ue_classification_requires_pinned_images')
        if not self.services or len(self.services)>100 or any(not v.get('service_type') or not v.get('region') for v in self.services.values()):
            raise ContractError('ue_service_classification_not_commissioned')

    @staticmethod
    def _clock(value):
        stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('Native launch clock must have timezone')
        return stamp.timestamp()

    @staticmethod
    def _running(pod,name,image):
        states=[s for s in pod.get('status',{}).get('containerStatuses',()) if s['name']==name]
        return (len(states)==1 and states[0].get('ready') is True
            and 'running' in states[0].get('state',{})
            and states[0].get('imageID','').endswith(image.split('@')[1])
            and states[0].get('restartCount')==0)

    def current_classifications(self,raw,native_rows):
        try:
            generation=raw['generation']
            if type(generation) is not int or generation<=0:
                raise ValueError('generation')
            before=self.generation_reader()
            if before['generation']!=generation or len(before['allocation'])>1000 or len(raw.get('perUe',()))>1000:
                raise ValueError('generation changed')
            allocation={row['supi']:row for row in before['allocation']}
            if len(allocation)!=len(before['allocation']):
                raise ValueError('duplicate allocation')
            response=self.client.call('get','pods','-l',
                f'cityverse.io/managed-by=onprem-ue-controller,cityverse.io/generation={generation}',
                '-o','json')
            if len(response['items'])>1000:
                raise ValueError('unbounded Pod census')
            pods={}
            for pod in response['items']:
                meta=pod['metadata']; uid=meta['uid']
                if (uid in pods or meta.get('deletionTimestamp') or
                    meta.get('labels',{}).get('cityverse.io/generation')!=str(generation)
                    or meta.get('labels',{}).get('cityverse.io/managed-by')!='onprem-ue-controller'
                    or not re.fullmatch(f'cv-ue-g{generation}-[0-9]+',meta['name'])):
                    raise ValueError('pod identity')
                pods[uid]=pod
            result={}
            # Derive city profiles from the exact reserved allocation and full
            # server round contract, never from an editable per-UE label alone.
            from backend.onprem.scenarios.contracts import bind_scenarios
            profile_owners = {r['supi']:r for r in bind_scenarios(raw['allocation'],
                [{'supi':r['supi'],'slice':r['slice']} for r in before['allocation']])}
            for owner in raw.get('perUe',()):
                supi=owner['supi']; native=native_rows[(supi,str(owner['sessionId']))]
                row=allocation[supi]; pod=pods[owner['podUid']]
                meta,spec=pod['metadata'],pod['spec']
                supis=json.loads(meta['annotations']['cityverse.io/supis'])
                if (not isinstance(supis,list) or not 1<=len(supis)<=250 or len(set(supis))!=len(supis)
                    or supi not in supis or any(s not in allocation for s in supis)
                    or any(allocation[s]['slice']!=row['slice'] for s in supis)
                    or meta['labels'].get('cityverse.io/slice')!=row['slice']):
                    raise ValueError('allocation differs')
                init=[c for c in spec['initContainers'] if c['name']=='config']
                containers=[c for c in spec['containers'] if c['name']=='ue']
                if len(init)!=1 or len(containers)!=1:
                    raise ValueError('container identity')
                init,ue=init[0],containers[0]
                from backend.onprem.runtime.topology import SLICES
                env={e['name']:e['value'] for e in init['env'] if 'value' in e}
                config_volumes=[v for v in spec.get('volumes',()) if v['name']=='config']
                ue_mounts=[m for m in ue.get('volumeMounts',()) if m['name']=='config']
                init_mounts=[m for m in init.get('volumeMounts',()) if m['name']=='config']
                foreign_writers=[c for c in (*spec.get('containers',()),*spec.get('initContainers',()))
                    if c['name'] not in {'ue','config'} and
                    any(m['name']=='config' for m in c.get('volumeMounts',()))]
                states=[s for s in pod['status']['initContainerStatuses'] if s['name']=='config']
                terminated=states[0].get('state',{}).get('terminated',{}) if len(states)==1 else {}
                ue_states=[s for s in pod['status'].get('containerStatuses',()) if s['name']=='ue']
                started=ue_states[0].get('state',{}).get('running',{}).get('startedAt') if len(ue_states)==1 else None
                init_finished=self._clock(terminated['finishedAt'])
                ue_started=self._clock(started)
                created=native['created_at']
                expected_init_mounts=[{'name':'credentials','mountPath':'/credentials','readOnly':True},
                    {'name':'config','mountPath':'/run/fru'}]
                expected_ue_mounts=[{'name':'config','mountPath':'/run/fru','readOnly':True},
                    {'name':'tun','mountPath':'/dev/net/tun'}]
                if (init['image']!=self.init_image or init['command']!=['python','-c',self.script]
                    or len(states)!=1 or terminated.get('exitCode')!=0
                    or terminated.get('reason')!='Completed'
                    or type(created) not in (int,float) or not math.isfinite(created)
                    or not 0<init_finished<=ue_started<=created
                    or init.get('args') or init.get('envFrom') or ue.get('envFrom') or ue.get('env')
                    or set(env)!={'GROUP','SST','SD','MSIN','GNB_HOST'}
                    or init.get('volumeMounts')!=expected_init_mounts or ue.get('volumeMounts')!=expected_ue_mounts
                    or init.get('workingDir') or ue.get('workingDir') not in (None,'/free-ran-ue')
                    or spec.get('hostPID') is True or spec.get('shareProcessNamespace') is True
                    or not states[0].get('imageID','').endswith(self.init_image.split('@')[1])
                    or states[0].get('restartCount')!=0
                    or len(env)!=len(init['env']) or foreign_writers
                    or config_volumes!=[{'name':'config','emptyDir':{'medium':'Memory'}}]
                    or ue_mounts!=[{'name':'config','mountPath':'/run/fru','readOnly':True}]
                    or init_mounts!=[{'name':'config','mountPath':'/run/fru'}]
                    or ue['image']!=self.image or not self._running(pod,'ue',self.image)
                    or ue.get('command')!=['/bin/sh','-c']
                    or ue.get('args')!=[f'exec ./free-ran-ue ue -c /run/fru/ue.json -n {len(supis)} -p 1']
                    or env['MSIN']!=supis[0][10:]
                    or any(int(s[5:])!=int(supis[0][5:])+i for i,s in enumerate(supis))
                    or str(row['credentialGroup'])!=env['GROUP']
                    or any(str(allocation[s]['credentialGroup'])!=env['GROUP'] for s in supis)
                    or env['GNB_HOST']!='gnb-'+row['slice']
                    or str(SLICES[row['slice']]['sst'])!=env['SST'] or SLICES[row['slice']]['sd']!=env['SD']
                    or str(native['sst'])!=str(env['SST']) or native['sd']!=env['SD']
                    or native['dnn']!='internet'):
                    raise ValueError('launched configuration differs')
                # The exact immutable initializer is the evidence for DNN/S-NSSAI.
                # Region/service labels come from the server-owned scenario policy.
                scenario=profile_owners[supi]['scenario']
                if owner['scenario'] != scenario:
                    raise ValueError('Reserved service demand differs')
                classification=self.services[scenario]
                result[(supi,str(native['sessionId']))]=dict(source='ue-configuration',
                    configuration_id=f"{meta['uid']}:{self.digest}:{self.image.split('@')[1]}",
                    supi=supi,pdu_session_id=native['sessionId'],native_generation=generation,
                    s_nssai=f"{native['sst']}-{native['sd']}",dnn=native['dnn'],
                    region=classification['region'],service_type=classification['service_type'],
                    service_profile_id=configured_profile(scenario,classification['service_type'],row['slice']))
            after=self.generation_reader()
            if after['generation']!=generation or after['allocation']!=before['allocation']:
                raise ValueError('generation changed')
            return result
        except Exception:
            raise ContractError('native_ue_classification_not_verified') from None
