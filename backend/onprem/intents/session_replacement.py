"""Server-only exact single-device geographic replacement authority."""
from copy import deepcopy
from dataclasses import asdict,dataclass
import hashlib
import json
import math

from .domain import ContractError
from .session_constraints import PopulationParentAuthority


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def spatial_revision(snapshot):
    spatial=snapshot.get('spatialDemand',{})
    return spatial.get('spatialRevision',spatial.get('resolution',{}).get('spatialRevision'))


def verify_vendor_geography(proof,access,device_profile):
    """A commissioned protocol port, never a caller's compatibility boolean."""
    source={'ueransim':'native-ueransim-single-commissioned-gnb/v1',
        'free-ran-ue':'native-free-ran-ue-single-session/v2'}.get(device_profile.driver)
    allowed=({device_profile.gnb_search_endpoint} if device_profile.driver=='ueransim' else
        set(getattr(device_profile,'commissioned_gnb_endpoints',()))|{device_profile.gnb_search_endpoint})
    if (source is None or proof.get('source')!=source or proof.get('pdr_far_gnb_verified') is not True
            or proof.get('gnbPodUid')!=access.get('gnbPodUid') or proof.get('gnbId')!=access.get('gnbId')
            or access.get('endpoint') not in allowed):
        raise ContractError('replacement_native_vendor_gnb_profile_changed')
    if device_profile.driver=='free-ran-ue' and (proof.get('vendor')!='free-ran-ue'
            or proof.get('gnbSearchEndpoint')!=access['endpoint'] or not proof.get('gnbN3Address')
            or not proof.get('gnbIncarnation') or not proof.get('amfIncarnation')
            or proof.get('peer',{}).get('ngSetupState')!='CONNECTED'):
        raise ContractError('replacement_native_freeran_reciprocal_proof_missing')
    return source


@dataclass(frozen=True)
class ExactSessionReplacement:
    parent:dict
    old_session_key:str
    old_nonces:tuple
    launch_profile_digest:str
    original_record:dict
    demand_profile:dict
    topology_revision:int
    spatial_revision:str

    def __post_init__(self):
        from backend.onprem.runtime.current_sessions import canonical_session_key
        from backend.onprem.runtime.device_sessions import DeviceAllocation
        from backend.onprem.scenarios.spatial import validate_spatial_request
        PopulationParentAuthority.from_dict(self.parent)
        identity=canonical_session_key(self.old_session_key)
        record=json.loads(json.dumps(self.original_record,allow_nan=False))
        device=DeviceAllocation.from_dict(record['device'])
        if (len(device.sessions)!=1 or device.supi!=identity.supi or not self.old_nonces
                or len(set(self.old_nonces))!=len(self.old_nonces) or any(not isinstance(v,str) or not v for v in self.old_nonces)
                or type(self.topology_revision) is not int or self.topology_revision<1
                or not isinstance(self.spatial_revision,str) or not self.spatial_revision
                or len(self.launch_profile_digest)!=64 or len(json.dumps(record))>16384):
            raise ContractError('invalid_exact_geographic_replacement')
        access=record['spatialAccess']
        if (record['demandGroupId']!=access['groupId'] or record['servingCellId']!=access['cellId']
                or any(not isinstance(access.get(k),str) or not access[k] for k in ('groupId','gnbId','cellId','endpoint'))):
            raise ContractError('replacement_original_geo_identity_missing')
        # Reuse the public point geometry validation, never accept endpoint or
        # native proof from the HTTP client as replacement authority.
        event=record.get('scenario','web_browsing')
        from backend.onprem.scenarios.contracts import EVENT_SLICES
        if event!='web_browsing' and event not in EVENT_SLICES:raise ContractError('replacement_scenario_missing')
        validate_spatial_request({'topologyRevision':self.topology_revision,'baselineUes':1 if event=='web_browsing' else 0,
            'events':{k:int(k==event) for k in EVENT_SLICES},'demandGroups':[{'groupId':access['groupId'],
                'profileId':event,'ueCount':1,'sourceLocation':record['sourceLocation']}]})
        if set(self.demand_profile)!={'service_type','region','dnn','snssai','logicalSessionId','slice','offered_ul_kbps','offered_dl_kbps'}:
            raise ContractError('replacement_demand_profile_missing')
        if any(type(self.demand_profile[k]) not in (int,float) or not math.isfinite(self.demand_profile[k]) or self.demand_profile[k]<=0
               for k in ('offered_ul_kbps','offered_dl_kbps')):raise ContractError('replacement_offered_load_missing')
        object.__setattr__(self,'old_nonces',tuple(self.old_nonces))
        object.__setattr__(self,'original_record',record)
        object.__setattr__(self,'demand_profile',deepcopy(self.demand_profile))

    def to_dict(self):return asdict(self)
    @classmethod
    def from_dict(cls,value):
        if not isinstance(value,dict) or set(value)!=set(cls.__dataclass_fields__):raise ContractError('invalid_replacement_fields')
        return cls(**value)

    def validate_profile(self,profile):
        from backend.onprem.runtime.ue_reservation import UEAdmissionLaunchProfile
        from backend.onprem.runtime.device_sessions import DeviceAllocation
        original=DeviceAllocation.from_dict(self.original_record['device'])
        proof=self.original_record.get('nativeSessionBaseline',{}).get('proof',{})
        geo=proof.get('spatialRan',{})
        access=self.original_record['spatialAccess']
        frozen=self.original_record.get('nativeDeviceProfile')
        if frozen is not None:
            from backend.onprem.runtime.device_sessions import DeviceDriverProfile
            native_profile=DeviceDriverProfile(**frozen)
            verify_vendor_geography(geo,access,native_profile)
            if native_profile.image_digest!=profile.image_digest:raise ContractError('replacement_native_profile_image_changed')
        elif geo.get('source')!='native-ueransim-single-commissioned-gnb/v1':
            raise ContractError('replacement_native_commissioned_vendor_profile_missing')
        if (type(profile) is not UEAdmissionLaunchProfile or digest(profile.to_dict())!=self.launch_profile_digest
                or dict(profile.device_sessions).get(original.access_slice)!=original.sessions
                or next((p for g,s,p in profile.device_session_ids if g==original.access_slice and s==original.sessions[0].session_id),None)
                    !=json.loads(self.old_session_key)[2]
                or geo.get('source') not in {'native-ueransim-single-commissioned-gnb/v1','native-free-ran-ue-single-session/v2'}
                or geo.get('pdr_far_gnb_verified') is not True or geo.get('gnbPodUid')!=access.get('gnbPodUid')
                or geo.get('gnbId')!=access['gnbId'] or proof.get('gnbSearchEndpoint')!=access['endpoint']):
            raise ContractError('replacement_commissioned_profile_changed')

    def added_metadata(self):
        return {k:deepcopy(self.original_record[k]) for k in
            ('spatialAccess','sourceLocation','demandGroupId','servingCellId','scenario') if k in self.original_record} | {'demandProfile':deepcopy(self.demand_profile)}

    def validate_delta(self,delta):
        if len(delta.added_devices)!=1 or len(delta.reserved_sessions)!=1 or delta.removed_device_ids:
            raise ContractError('replacement_exact_single_device_required')
        row=delta.added_records[0]
        if (any(row.get(k)!=v for k,v in self.added_metadata().items())
                or delta.reserved_sessions[0].nonce in self.old_nonces
                or row['supi']==self.original_record['supi']):
            raise ContractError('replacement_reservation_geo_or_nonce_changed')

    def validate_retired(self,records):
        old=self.original_record
        matches=[r for r in records if r['device']['device_id']==old['device']['device_id']]
        fields=('groupId','gnbId','cellId','endpoint')
        if (len(matches)!=1 or matches[0]['device']!=old['device'] or matches[0].get('sourceLocation')!=old['sourceLocation']
                or any(matches[0].get('spatialAccess',{}).get(k)!=old['spatialAccess'].get(k) for k in fields)):
            raise ContractError('replacement_original_device_or_geography_not_retired')

    def preview_workload(self,snapshot,resources):
        """Evaluate demand for a proposed replacement; never a native observation."""
        from backend.onprem.traffic.session_workload import session_rates
        from backend.onprem.runtime.current_sessions import session_rows
        preview=deepcopy(snapshot)
        if self.old_session_key in {r['sessionKey'] for r in session_rows(preview)}:
            raise ContractError('replacement_original_session_still_present')
        preview['devices'].append(deepcopy(self.original_record['nativeDeviceBaseline']))
        preview['perSession'].append(deepcopy(self.original_record['nativeSessionBaseline']))
        offered=session_rates(resources.session_workload(preview)).get(self.old_session_key)
        if offered!={'ul':self.demand_profile['offered_ul_kbps'],'dl':self.demand_profile['offered_dl_kbps']}:
            raise ContractError('replacement_planned_offered_load_changed')

    def validate_published(self,snapshot,delta):
        from backend.onprem.runtime.current_sessions import session_rows
        from backend.onprem.runtime.ue import seconds
        import time
        self.validate_delta(delta)
        rows=[r for r in session_rows(snapshot) if r['deviceId']==delta.added_devices[0].device_id]
        if len(rows)!=1:raise ContractError('replacement_native_session_missing')
        row=rows[0];old=self.original_record;access=old['spatialAccess']
        if (row['sessionKey']==self.old_session_key
                or any(row.get(k)!=old[k] for k in ('sourceLocation','demandGroupId','servingCellId'))
                or any(row.get(k)!=v for k,v in self.demand_profile.items() if not k.startswith('offered_'))):
            raise ContractError('replacement_published_demand_changed')
        proof=snapshot.get('spatialRan') or (snapshot.get('connectivityEvidence') or {}).get('spatialRan',{})
        bindings=[r for r in proof.get('bindings',()) if r.get('supi')==row['supi']]
        expected_source=self.original_record['nativeSessionBaseline']['proof']['spatialRan']['source']
        if (proof.get('source')!=expected_source or len(bindings)!=1
                or bindings[0].get('gnbId')!=access['gnbId'] or bindings[0].get('servingCellId')!=access['cellId']
                or bindings[0].get('demandGroupId')!=access['groupId'] or not bindings[0].get('gnbPodUid')
                or bindings[0].get('gnbPodUid')!=access.get('gnbPodUid')
                or bindings[0].get('pdr_far_gnb_verified') is not True
                or not -1<=time.time()-seconds(bindings[0].get('observedAt'))<=15):
            raise ContractError('replacement_native_gnb_proof_changed')
        if self.original_record.get('nativeDeviceProfile'):
            from backend.onprem.runtime.device_sessions import DeviceDriverProfile
            verify_vendor_geography(bindings[0],access,DeviceDriverProfile(**self.original_record['nativeDeviceProfile']))
        return row
