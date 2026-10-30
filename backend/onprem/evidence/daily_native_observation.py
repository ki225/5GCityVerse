"""Fail-closed city-wide native observation without a commissioning round.

This reader correlates existing AMF/SMF, UE-controller, free-ran-ue, UPF and
N6 evidence.  It deliberately does not claim a per-UE NGAP binding and can
never authorize a targeted mutation.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import ipaddress,json,logging,re,subprocess,threading,time

from backend.onprem.evidence.collector import RAN_EXEC_SLOTS,canonical_fru_imsi,sctp_associations
from backend.onprem.evidence.pod_identity import pod_incarnation

SLICES=('embb','urllc','mmtc','v2x')
SOURCE='amf+smf-pdu+native-ue+free-ran-ue-imsi-sctp+upf-n6/v1'
logger=logging.getLogger(__name__)

class _CoherenceChanged(ValueError):
    def __init__(self,fields):
        super().__init__('daily native observation changed during read')
        self.fields=sorted(fields)

class CityDailyNativeObserver:
    """Read a bounded 0..500 UE, four-slice observation from trusted readers."""

    def _need(self, value):
        if not value: raise ValueError('daily native observation unavailable')

    def _seconds(self, value):
        self._need(isinstance(value, str))
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        self._need(parsed.tzinfo is not None)
        return parsed.timestamp()

    def _same(self, left, right):
        return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)

    def __init__(self,kubectl,native_reader,namespace,amf_pod,gnb_pods,upf_services,upf_pods,n6_pods,
                 *,clock=time.time,monotonic=time.monotonic,ran_backend='free-ran-ue',pod_resolver=None):
        self._need(callable(kubectl) and callable(native_reader) and callable(clock) and callable(monotonic))
        self._need(set(gnb_pods)==set(upf_services)==set(upf_pods)==set(n6_pods)==set(SLICES))
        names=[namespace,amf_pod,*gnb_pods.values(),*upf_services.values(),*upf_pods.values(),*n6_pods.values()]
        self._need(all(isinstance(x,str) and re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}',x) for x in names))
        self.kubectl,self.native_reader=kubectl,native_reader
        self.namespace,self.amf_pod=namespace,amf_pod
        self.gnb_pods,self.upf_services,self.upf_pods,self.n6_pods=dict(gnb_pods),dict(upf_services),dict(upf_pods),dict(n6_pods)
        self.clock,self.monotonic=clock,monotonic;self.lock=threading.Lock()
        self._need(ran_backend in ('free-ran-ue','ueransim'));self.ran_backend=ran_backend
        self._need(pod_resolver is None or callable(pod_resolver));self.pod_resolver=pod_resolver

    def read(self):
        unavailable={'schemaVersion':'cityverse.daily-native-observation/v1','available':False,
            'reason':'native_evidence_unavailable','source':SOURCE,
            'evidenceTrust':'observation-only-no-per-ue-ngap-binding','criticalOperationEligible':False,
            'strongBinding':False,'independentNgapProvenance':False,'mutationsPerformed':False}
        if self.ran_backend!='free-ran-ue':
            return {**unavailable,'reason':'ran_backend_mismatch'}
        if not self.lock.acquire(blocking=False): return unavailable
        started=self.monotonic();self._phase='initializing'
        try: return self._read(unavailable)
        except Exception as exc:
            diagnostic={'phase':self._phase,'errorType':type(exc).__name__,
                'elapsedMs':max(0,round((self.monotonic()-started)*1000))}
            if isinstance(exc,_CoherenceChanged): diagnostic['mismatchFields']=exc.fields
            return {**unavailable,'diagnostic':diagnostic}
        finally: self.lock.release()

    def _read(self,unavailable):
        deadline=self.monotonic()+30
        def remaining():
            left=deadline-self.monotonic();self._need(left>0);return min(10,left)
        def run(args):
            acquired=args[0]!='exec' or RAN_EXEC_SLOTS.acquire(timeout=remaining())
            self._need(acquired)
            try:raw=self.kubectl(args,timeout=remaining())
            finally:
                if args[0]=='exec':RAN_EXEC_SLOTS.release()
            remaining()
            self._need(isinstance(raw,str) and len(raw.encode())<=16*1024*1024);return raw
        def get(args): return json.loads(run(args))
        if self.pod_resolver is not None:
            self._phase='pod_inventory'
            resolved=self.pod_resolver(timeout=remaining())
            self._need(isinstance(resolved,dict) and set(resolved)=={'amf','gnb','upf','n6'})
            self._need(all(isinstance(resolved[k],dict) and set(resolved[k])==set(SLICES)
                           for k in ('gnb','upf','n6')))
            names=[resolved['amf'],*(name for k in ('gnb','upf','n6') for name in resolved[k].values())]
            self._need(all(isinstance(name,str) and re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}',name) for name in names))
            # The read lock freezes this set until the closing incarnation check.
            self.amf_pod=resolved['amf']
            self.gnb_pods,self.upf_pods,self.n6_pods=(dict(resolved[k]) for k in ('gnb','upf','n6'))
            remaining()
        def fresh(stamp, *, guest=False):
            # VM clocks may lead the Windows host by a few seconds after a
            # retained KVM lab resumes. Native controller timestamps come
            # from this host; NF and gNB timestamps come from the guests.
            self._need((-10 if guest else -1)<=self.clock()-self._seconds(stamp)<=15)
        identity_specs={'amf':(self.amf_pod,'amf')}
        for slice_name in SLICES:
            identity_specs['gnb-'+slice_name]=(self.gnb_pods[slice_name],'gnb-'+slice_name)
            identity_specs['upf-'+slice_name]=(self.upf_pods[slice_name],'upf-'+slice_name)
            identity_specs['n6-'+slice_name]=(self.n6_pods[slice_name],'n6-'+slice_name)
        def identities():
            # One fresh inventory for each fence, never reuse the initial list
            # at the closing check. Validate every required Pod/container with
            # the same incarnation verifier used by individual Pod reads.
            inventory=get(['get','pods','-n',self.namespace,'-o','json'])
            self._need(isinstance(inventory,dict) and isinstance(inventory.get('items'),list))
            required={name for name,_ in identity_specs.values()};selected={}
            for value in inventory['items']:
                metadata=value.get('metadata') if isinstance(value,dict) else None
                name=metadata.get('name') if isinstance(metadata,dict) else None
                if name in required:
                    self._need(name not in selected)
                    selected[name]=value
            self._need(set(selected)==required)
            result={}
            for key,(name,container) in identity_specs.items():
                remaining()
                result[key]=pod_incarnation(selected[name],name=name,container=container,
                    namespace=self.namespace,now=self.clock())
            remaining()
            return result
        self._phase='pod_identity_initial';pins=identities()
        self._phase='native_controller_initial'
        native=self.native_reader(timeout=remaining());remaining()
        self._need(native.get('runtime')=='kubeadm' and native.get('errors')==[]);fresh(native.get('observedAt'))
        generation=native.get('generation')
        self._need(type(generation) is int and generation>=0)
        rows=native.get('perUe');self._need(isinstance(rows,list) and len(rows)<=500)
        by_supi={}
        for row in rows:
            self._need(isinstance(row,dict) and isinstance(row.get('supi'),str) and re.fullmatch(r'imsi-[0-9]{15}',row['supi']))
            self._need(row['supi'] not in by_supi and row.get('slice') in SLICES)
            self._need(row.get('registered') is True and row.get('pduEstablished') is True and row.get('trafficVerified') is True)
            self._need(isinstance(row.get('podUid'),str) and row['podUid']);fresh(row.get('observedAt'))
            for k in ('sessionId','smContextRef','ip','dnn','sd','upf','pfcpSeid','upfEpoch'): self._need(isinstance(row.get(k),str) and row[k])
            self._need(type(row.get('sst')) is int and 1<=row['sst']<=255 and re.fullmatch(r'[0-9a-fA-F]{6}',row['sd']))
            self._need(str(ipaddress.IPv4Address(row['ip']))==row['ip']);by_supi[row['supi']]=deepcopy(row)
        self._phase='amf_context'
        amf=get(['get','--raw',f'/api/v1/namespaces/{self.namespace}/pods/{self.amf_pod}:8080/proxy/namf-oam/v1/registered-ue-context'])
        # The AMF encodes its nil Go slice as JSON null when no UE is
        # registered.  That is the wire-equivalent of an empty context list;
        # all other non-list shapes remain fail-closed below.
        if amf is None: amf=[]
        self._need(isinstance(amf,list));amf_by={}
        for value in amf:
            self._need(isinstance(value,dict) and value.get('Supi') in by_supi and value['Supi'] not in amf_by)
            row=by_supi[value['Supi']];session={'Dnn':row['dnn'],'PduSessionId':row['sessionId'],'Sd':row['sd'],
                'SmContextRef':row['smContextRef'],'Sst':str(row['sst'])}
            self._need(value.get('AccessType')=='3GPP_ACCESS' and value.get('CmState')=='CONNECTED' and self._same(value.get('PduSessions'),[session]))
            observation=value.get('ueAmbrObservation');self._need(isinstance(observation,dict) and observation.get('available') is True)
            self._need(observation.get('source')=='amf-active-ue-context' and observation.get('supi')==row['supi']);fresh(observation.get('observedAt'), guest=True)
            amf_by[row['supi']]={'source':observation['source'],'observedAt':observation['observedAt'],'accessType':'3GPP_ACCESS'}
        self._need(set(amf_by)==set(by_supi))
        slice_output={};timestamps=[native['observedAt']]
        self._phase='gnb_sctp_parallel'
        amf_evidence=get(['get','--raw',f'/api/v1/namespaces/{self.namespace}/pods/{self.amf_pod}:9083/proxy/v1/evidence'])
        self._need(amf_evidence.get('schemaVersion')=='cityverse.ran-evidence/v1' and amf_evidence.get('role')=='amf')
        fresh(amf_evidence.get('observedAt'), guest=True);amf_sctp=amf_evidence['sctp']
        queued=self.monotonic()
        def read_gnb(slice_name):
            call_started=self.monotonic();attempts=0;outcome='failed'
            gnb=self.gnb_pods[slice_name];container='gnb-'+slice_name
            def once():
                nonlocal attempts
                attempts+=1
                observed=get(['get','--raw',f'/api/v1/namespaces/{self.namespace}/pods/{gnb}:9083/proxy/v1/evidence'])
                self._need(observed.get('schemaVersion')=='cityverse.ran-evidence/v1' and observed.get('role')=='gnb')
                fresh(observed.get('observedAt'), guest=True)
                return observed['config'],observed
            try:
                try:result=once()
                except subprocess.TimeoutExpired:result=once()
                outcome='complete'
                return result
            finally:
                try:
                    logger.warning('daily_gnb_proxy_timing %s',json.dumps({
                        'slice':slice_name,'wallTimeUnixMs':round(self.clock()*1000),
                        'queueWaitMs':max(0,round((call_started-queued)*1000,3)),
                        'durationMs':max(0,round((self.monotonic()-call_started)*1000,3)),
                        'attempts':attempts,'outcome':outcome},sort_keys=True))
                except Exception:
                    pass  # Diagnostic logging must not alter evidence or failure semantics.
        # Pod proxy GETs do not use the shared exec slots. Limit this reader to
        # two concurrent GETs; N6 execs below retain the global stream ceiling.
        # Context exit drains every submitted read before final identity fences.
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix='daily-gnb-read') as executor:
            gnb_observations=dict(zip(SLICES,executor.map(read_gnb,SLICES)))
        for slice_name in SLICES:
            self._phase='slice_'+slice_name
            gnb=self.gnb_pods[slice_name];container='gnb-'+slice_name
            config,observed=gnb_observations[slice_name];info=observed['info']['gnbInfo']
            expected=sorted(x.removeprefix('imsi-') for x,r in by_supi.items() if r['slice']==slice_name)
            actual=sorted(canonical_fru_imsi(x.get('imsi')) for x in info.get('ranUeList',[]))
            self._need(actual==expected and len(actual)==len(set(actual)))
            self._need(info.get('gnbName')==config['gnbName'] and info.get('plmnId')=='20893')
            local,remote=str(ipaddress.IPv4Address(config['ranN2Ip'])),str(ipaddress.IPv4Address(config['amfN2Ip']));port=config['amfN2Port']
            gm=[x for x in sctp_associations(observed['sctp']) if x['state']==3 and x['local']==[local] and x['remote']==[remote] and x['remotePort']==port];self._need(len(gm)==1)
            reciprocal=[x for x in sctp_associations(amf_sctp) if x['state']==3 and x['local']==[remote] and x['remote']==[local] and x['localPort']==port and x['remotePort']==gm[0]['localPort']];self._need(len(reciprocal)==1)
            upf=get(['get','--raw',f'/api/v1/namespaces/{self.namespace}/services/http:{self.upf_services[slice_name]}:9081/proxy/v1/upf-state'])
            self._need(upf.get('schemaVersion')=='cityverse.upf/v1' and upf.get('source')=='gtp5g-netlink' and upf.get('available') is True)
            self._need(upf.get('upf')=='upf-'+slice_name and upf.get('errors')==[] and upf.get('residuals')==[])
            self._need(isinstance(upf.get('epoch'),str) and upf['epoch'] and isinstance(upf.get('moduleVersion'),str) and upf['moduleVersion'])
            self._need(isinstance(upf.get('netns'),str) and re.fullmatch(r'net:\[[1-9][0-9]*\]',upf['netns']));fresh(upf.get('observedAt'), guest=True)
            qos=upf.get('qosObservation');self._need(isinstance(qos,dict) and qos.get('available') is True and qos.get('enabled') is True)
            self._need(qos.get('source')=='/proc/gtp5g/qos' and qos.get('scope')=='kernel-module' and qos.get('errors')==[])
            self._need(qos.get('epoch')==upf['epoch'] and qos.get('observedAt')==upf['observedAt']);fresh(qos['observedAt'], guest=True)
            expected_rows=[r for r in by_supi.values() if r['slice']==slice_name];sessions=upf.get('sessions');self._need(isinstance(sessions,list) and len(sessions)==len(expected_rows))
            for row in expected_rows:
                matches=[s for s in sessions if s.get('ip')==row['ip'] and str(s.get('pfcpSeid'))==row['pfcpSeid']]
                self._need(len(matches)==1 and matches[0].get('evidence')=='kernel-installed-pdr-far')
                self._need(matches[0].get('upf')==row['upf']=='upf-'+slice_name and row['upfEpoch']==upf['epoch'])
                self._need(matches[0].get('observedAt')==upf['observedAt'])
            n6=self.n6_pods[slice_name];n6_stamp=self.clock()
            address=run(['exec',n6,'-n',self.namespace,'-c','n6-'+slice_name,'--','ip','-o','-4','address','show','dev','n6'])
            match=re.fullmatch(r'\d+: n6\s+inet\s+([0-9.]+)/\d+.*\n?',address);self._need(match and str(ipaddress.IPv4Address(match[1]))==match[1])
            timestamps.append(upf['observedAt'])
            slice_output[slice_name]={'ueCount':len(expected),'gnb':{'source':'free-ran-ue-/api/gnb/info','imsis':actual,
                'sctpReciprocal':True},'upf':{'source':upf['source'],'observedAt':upf['observedAt'],'epoch':upf['epoch'],
                'sessionCount':len(sessions)},'n6':{'source':'owned-n6-ip-address','observedAt':n6_stamp,'ip':match[1]}}
        self._phase='pod_identity_final'
        self._need(self._same(pins,identities()))
        # Re-read the controller fence after all remote evidence. Timestamps may
        # legitimately advance, but ownership/session/UPF epochs may not.
        self._phase='native_controller_final'
        final_native=self.native_reader(timeout=remaining());remaining()
        self._need(final_native.get('runtime')=='kubeadm' and final_native.get('errors')==[]);fresh(final_native.get('observedAt'))
        self._phase='coherence_final'
        if final_native.get('generation')!=generation:
            raise _CoherenceChanged({'generation'})
        final_rows=final_native.get('perUe');self._need(isinstance(final_rows,list) and len(final_rows)<=500)
        fence_keys=('supi','slice','generation','registered','pduEstablished','trafficVerified','podUid','sessionId',
                    'smContextRef','ip','dnn','sst','sd','upf','pfcpSeid','upfEpoch')
        def fence(values):
            self._need(all(isinstance(x,dict) and isinstance(x.get('supi'),str) for x in values))
            result=[{k:x.get(k) for k in fence_keys} for x in values]
            self._need(len({x['supi'] for x in result})==len(result));return sorted(result,key=lambda x:x['supi'])
        initial_fence,final_fence=fence(rows),fence(final_rows)
        if not self._same(initial_fence,final_fence):
            if len(initial_fence)!=len(final_fence): fields={'ueSet'}
            else:
                fields={key for before,after in zip(initial_fence,final_fence) for key in fence_keys if before[key]!=after[key]}
            raise _CoherenceChanged(fields or {'ueSet'})
        for value in amf_by.values(): timestamps.append(value['observedAt'])
        for row in by_supi.values(): timestamps.append(row['observedAt'])
        # Every timestamp was checked against the wall clock immediately after
        # acquisition. Re-aging the earliest source here incorrectly rejects a
        # coherent bounded interval merely because later read-only probes took
        # time. The 30-second monotonic deadline and final native/Pod fences
        # bound the interval and still reject actual mid-snapshot changes.
        remaining()
        ues=[]
        for supi,row in sorted(by_supi.items()):
            ues.append({'supi':supi,'slice':row['slice'],'dnn':row['dnn'],'sst':row['sst'],'sd':row['sd'],
                'sessionId':row['sessionId'],'smContextRef':row['smContextRef'],'ip':row['ip'],'upf':row['upf'],
                'podUid':row['podUid'],'nativeObservedAt':row['observedAt'],'amf':amf_by[supi],
                'bindingTrust':'observation-only-no-per-ue-ngap-binding'})
        return {**unavailable,'available':True,'reason':None,'generation':generation,
            'observedAt':min(timestamps,key=self._seconds),
            'podIncarnations':pins,'ueCount':len(ues),'ues':ues,'slices':slice_output}
