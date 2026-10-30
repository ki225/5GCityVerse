"""In-process native evidence coordinator for the on-prem controller.

Reads NF OAM, fresh gtp5g sidecar dumps, Kubernetes UID/ownership, MongoDB
subscriptions and actual UE namespace probes. No browser-supplied evidence and
no fabricated registration/tunnel/session counters.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, wait
from copy import deepcopy
import base64
import json
import ipaddress
import logging
import os
from pathlib import Path
import re
import subprocess
from threading import BoundedSemaphore, Event, Lock
import time
import uuid
import yaml
from urllib.request import urlopen

from backend.onprem.runtime.kubeadm import KubeadmDriver, LABEL
from backend.onprem.integrations.oam import NativeOamAdapter
from backend.onprem.evidence.probe import TunProber
from backend.onprem.evidence.kubernetes_pod_read import KubernetesPodRead
from backend.onprem.evidence.ran_http import RanEvidenceClient

# The four gNBs share one single-node kubelet streaming endpoint. This process
# also serves daily observations, so per-reader worker limits are insufficient:
# both readers must share the same verified two-stream ceiling.
RAN_EXEC_SLOTS = BoundedSemaphore(2)
from backend.onprem.evidence.free_ran_probe import FreeRanTunProber
from backend.onprem.evidence.free_ran_read_transport import configured_ue_read_transport
from backend.onprem.operations.provisioning.subscribers import credentials
from backend.onprem.runtime.ue import BASE_IMSI, ENDPOINTS, EvidenceUnavailable, SLICES, seconds, timestamp

logger = logging.getLogger(__name__)
_TIMING_ERROR_TYPES = frozenset({
    "EvidenceUnavailable", "TimeoutError", "OSError", "HTTPError", "URLError",
    "RemoteDisconnected", "ValueError", "TypeError", "KeyError", "AttributeError",
})


def _timing_error_type(error):
    name = type(error).__name__
    if name in _TIMING_ERROR_TYPES:
        return name
    if isinstance(error, TimeoutError):
        return "TimeoutError"
    if isinstance(error, OSError):
        return "OSError"
    return "UnexpectedError"


def _ran_pod_ip(pod):
    return RanEvidenceClient.address(pod)


def _ran_http_evidence(pod, deadline):
    return RanEvidenceClient().read(pod, deadline)


def canonical_fru_imsi(value):
    """Return the 15-digit FRU IMSI, accepting only its two wire formats."""
    if not isinstance(value, str):
        raise ValueError('Free-RAN IMSI malformed')
    match = re.fullmatch(r'(?:imsi-)?([0-9]{15})', value)
    if not match:
        raise ValueError('Free-RAN IMSI malformed')
    return match.group(1)


# The collector and UPF exporters run on independently synchronized hosts.
# This is deliberately smaller than the stale window: it tolerates modest
# cross-host offset without allowing an arbitrarily future-dated sample.
UPF_FUTURE_SKEW_SECONDS = 10


def sctp_associations(raw):
    """Linux /proc/net/sctp/assocs, not LISTEN endpoints; ST3=ESTABLISHED."""
    lines=raw.splitlines()
    if not lines or lines[0].split()[:16]!=['ASSOC','SOCK','STY','SST','ST','HBKT','ASSOC-ID','TX_QUEUE','RX_QUEUE','UID','INODE','LPORT','RPORT','LADDRS','<->','RADDRS']:
        raise EvidenceUnavailable('SCTP association schema unavailable')
    result=[]
    for line in lines[1:]:
        if not line.strip():continue
        values=line.split();split=values.index('<->')
        if split<14 or len(values[split+1:])<12:raise EvidenceUnavailable('SCTP association row malformed')
        local=[str(ipaddress.IPv4Address(x.removeprefix('*'))) for x in values[13:split]]
        remote=[str(ipaddress.IPv4Address(x.removeprefix('*'))) for x in values[split+1:-11]]
        result.append({'state':int(values[4]),'id':int(values[6]),'localPort':int(values[11]),
                       'remotePort':int(values[12]),'local':local,'remote':remote})
    return result


def current_ngap_peer(config,status,amfs,info,gnb_sctp,amf_sctp):
    """Positive current NG Setup context AND matching two-ended kernel SCTP."""
    if not isinstance(status,dict) or set(status)!={'is-ngap-up'} or status['is-ngap-up'] is not True or not isinstance(amfs,list) or len(amfs)!=1:
        raise EvidenceUnavailable('Current AMF context unavailable')
    peer_id=amfs[0].get('id');peers=config.get('amfConfigs')
    if type(peer_id) is not int or peer_id<0 or not isinstance(peers,list) or len(peers)!=1:
        raise EvidenceUnavailable('Unique configured AMF unavailable')
    peer=peers[0];local=str(ipaddress.IPv4Address(config['ngapIp']));remote=str(ipaddress.IPv4Address(peer['address']))
    port=peer['port']
    if type(port) is not int or not 1<=port<=65535 or type(info.get('id')) is not int or info['id']!=peer_id or info.get('state')!='CONNECTED' or info.get('address')!=f'{remote}:{port}':
        raise EvidenceUnavailable('Current NG Setup peer mismatch')
    association=info.get('association',{});aid=association.get('id')
    # UERANSIM v3.2.7 handleAssociationSetup stores amfId here, NOT ascId.
    # Bind that application ID to its context, and kernel association by the
    # unique reciprocal address/port tuple; these numeric domains are distinct.
    if type(aid) is not int or aid!=peer_id or any(type(association.get(k)) is not int or association[k]<=0 for k in ('rx-num','tx-num')):
        raise EvidenceUnavailable('Current NG Setup association unavailable')
    matches=[x for x in sctp_associations(gnb_sctp) if x['state']==3
             and x['local']==[local] and x['remote']==[remote] and x['remotePort']==port]
    if len(matches)!=1:raise EvidenceUnavailable('gNB kernel association unavailable')
    gnb=matches[0]
    matches=[x for x in sctp_associations(amf_sctp) if x['state']==3 and x['local']==[remote]
             and x['remote']==[local] and x['localPort']==port and x['remotePort']==gnb['localPort']]
    if len(matches)!=1:raise EvidenceUnavailable('AMF kernel association unavailable')
    return {'address':remote,'port':port,'gnbAddress':local,'gnbPort':gnb['localPort'],
            'amfContextId':peer_id,'gnbAssociationId':gnb['id'],'amfAssociationId':matches[0]['id'],'ngSetupState':'CONNECTED'}


class NativeSubscriberReader:
    """Four bulk reads of UDR collections; keys never leave this object.

    Schema follows free5gc/webconsole backend/WebUI api_webui.go and
    utils.go WebAuthSubToModels (encPermanentKey / encOpcKey).
    """
    def __init__(self, database, groups):
        self.database, self.groups = database, groups

    def collect(self):
        expected = {f"imsi-{BASE_IMSI + index}": index for index in range(1000)}
        query = {"ueId": {"$in": list(expected)}}
        names = {"auth": "subscriptionData.authenticationData.authenticationSubscription",
                 "am": "subscriptionData.provisionedData.amData", "sm": "subscriptionData.provisionedData.smData",
                 "smf": "subscriptionData.provisionedData.smfSelectionSubscriptionData"}
        # Every subscriber is still read on every pass. Exclude large, unused
        # profile fields and live AKA sequence material from this observer.
        fields = {"auth": ("encPermanentKey", "encOpcKey", "authenticationMethod"),
                  "am": ("servingPlmnId", "nssai.singleNssais"),
                  "sm": ("servingPlmnId", "singleNssai", "dnnConfigurations.internet"),
                  "smf": ("servingPlmnId", "subscribedSnssaiInfos")}
        tables = {}
        for key, name in names.items():
            projection = {"_id": 0, "ueId": 1, **{field: 1 for field in fields[key]}}
            rows = list(self.database[name].find(query, projection).max_time_ms(3000))
            table = defaultdict(list)
            for row in rows:
                table[row["ueId"]].append(row)
            tables[key] = table
        snssais = {(item["sst"], item["sd"]) for item in SLICES.values()}
        for supi, index in expected.items():
            if any(len(tables[key][supi]) != 1 for key in ("auth", "am", "smf")) or len(tables["sm"][supi]) != 4:
                raise EvidenceUnavailable("Subscriber pool lacks unique complete four-slice records")
            auth, am, smf = (tables[key][supi][0] for key in ("auth", "am", "smf"))
            group = self.groups[index // 250]
            if (auth.get("encPermanentKey", "").lower() != group["key"].lower()
                    or auth.get("encOpcKey", "").lower() != group["opc"].lower()
                    or auth.get("authenticationMethod") != "5G_AKA"):
                raise EvidenceUnavailable("Subscriber credential verification failed")
            if am.get("servingPlmnId") != "20893" or {(item["sst"], item["sd"]) for item in am.get("nssai", {}).get("singleNssais", [])} != snssais:
                raise EvidenceUnavailable("Subscriber allowed slices do not match")
            sessions = tables["sm"][supi]
            if {(item["singleNssai"]["sst"], item["singleNssai"]["sd"]) for item in sessions} != snssais or any(
                    item.get("servingPlmnId") != "20893" or "internet" not in item.get("dnnConfigurations", {}) for item in sessions):
                raise EvidenceUnavailable("Subscriber session slice/DNN verification failed")
            selected = smf.get("subscribedSnssaiInfos", {})
            if smf.get("servingPlmnId") != "20893" or any(
                    not any(item.get("dnn") == "internet" for item in selected.get(f"{sst:02d}{sd}", {}).get("dnnInfos", [])) for sst, sd in snssais):
                raise EvidenceUnavailable("Subscriber SMF selection verification failed")
        return {"verifiedCount": 1000, "credentialsMatched": True, "observedAt": timestamp()}


class NativeCollector:
    def __init__(self, store, driver, native, subscribers, upf_urls, *, prober=None, fetch=None, runner=subprocess.run):
        self.ran_evidence_transport = os.environ.get('CITYVERSE_ONPREM_RAN_EVIDENCE_TRANSPORT', 'kubectl')
        if self.ran_evidence_transport not in {'kubectl', 'pod-http'}:
            raise ValueError('Unsupported trusted RAN evidence transport')
        pod_transport = os.environ.get('CITYVERSE_ONPREM_K8S_POD_READ_TRANSPORT', 'kubectl')
        if pod_transport not in {'kubectl', 'api-https'}:
            raise ValueError('Unsupported trusted Kubernetes Pod read transport')
        self.pod_reader = (KubernetesPodRead(driver.context, driver.namespace)
                           if pod_transport == 'api-https' else None)
        if set(upf_urls) != set(SLICES):
            raise ValueError("Exactly four native UPF evidence URLs are required")
        self.store, self.driver, self.native, self.subscribers = store, driver, native, subscribers
        ue_reads = configured_ue_read_transport(os.environ, driver)
        default_prober = (FreeRanTunProber(driver, read_transport=ue_reads)
                          if getattr(driver, 'backend', None) == 'free-ran-ue' else TunProber(driver))
        self.upf_urls, self.prober = upf_urls, prober or default_prober
        self.fetch, self.runner = fetch or self._fetch, runner
        self._observation_lock = Lock()
        self._observation_flights = {}

    def _pod_list(self, *, deadline, managed=False):
        reader = getattr(self, 'pod_reader', None)
        if reader is not None:
            return reader.pods(deadline=deadline, managed=managed)
        args = ['get', 'pods', *(['-l', LABEL] if managed else []), '-o', 'json']
        return self.driver._read_kubectl(*args)

    @staticmethod
    def _fetch(url):
        with urlopen(url, timeout=5) as response:
            return json.load(response)

    def _upf(self, name,*,upf_scope=None,finalize_scope=True):
        if upf_scope is not None:
            from backend.onprem.intents.resource_adapters import KubectlResourceClient
            dump=upf_scope.kernel(name,KubectlResourceClient(context=self.driver.context,
                namespace=self.driver.namespace,runner=self.driver.runner,timeout=5))
            if finalize_scope:upf_scope.finish()
            return dump
        data = self.fetch(self.upf_urls[name].rstrip("/") + "/v1/upf-state")
        if (not isinstance(data, dict) or data.get("schemaVersion") != "cityverse.upf/v1"
                or data.get("available") is not True or data.get("upf") != f"upf-{name}"
                or data.get("source") != "gtp5g-netlink"
                or data.get("errors") != [] or not data.get("epoch")
                or not isinstance(data.get("sessions"), list) or not isinstance(data.get("residuals"), list)):
            raise EvidenceUnavailable("Native UPF evidence unavailable or incompatible")
        # The evidence sidecar runs in the UPF VM while this collector runs on
        # the Windows host.  Both clocks are NTP-synchronised, but the host can
        # still trail a freshly sampled guest timestamp by several seconds.
        # Keep the future-skew allowance bounded and retain the 15s stale limit.
        if not -UPF_FUTURE_SKEW_SECONDS <= time.time() - seconds(data.get("observedAt")) <= 15:
            raise EvidenceUnavailable("Native UPF dump is stale")
        return data

    def _gnb(self, pods):
        if getattr(self.driver,'device_profile',None) is not None:
            from .device_ran import NativeDeviceRanReader
            from .free_ran_device_sessions import FreeRanDeviceRanReader
            reader=getattr(self.driver,'device_ran_reader',None)
            expected=FreeRanDeviceRanReader if self.driver.device_profile.driver=='free-ran-ue' else NativeDeviceRanReader
            if not isinstance(reader,expected) or reader.driver is not self.driver:
                raise EvidenceUnavailable('Commissioned isolated RAN prelaunch reader unavailable')
            return reader(pods)
        if getattr(self.driver, 'backend', None) == 'free-ran-ue':
            return self._gnb_free_ran(pods)
        started=timestamp();deadline=time.monotonic()+10
        unavailable=lambda:{'ngapAssociated':False,'available':False,'source':'native-ngap-sctp/v2',
                            'reason':'current_ngap_peer_unavailable','observedAt':started}
        def need(ok):
            if not ok:raise EvidenceUnavailable('Current RAN identity unavailable')
        def run(args):
            remaining=deadline-time.monotonic();need(remaining>0)
            result=self.runner(['kubectl','--context',self.driver.context,'--namespace',self.driver.namespace,*args],
                text=True,capture_output=True,timeout=min(5,remaining),check=False)
            need(result.returncode==0 and len(result.stdout.encode())<=1024*1024 and time.monotonic()<deadline)
            return result.stdout
        def select(values,app):
            rows=[p for p in values if p.get('metadata',{}).get('labels',{}).get('app.kubernetes.io/name')==app]
            need(len(rows)==1);return rows[0]
        def identity(pod,container):
            meta,status=pod['metadata'],pod['status']
            need(meta['namespace']==self.driver.namespace and isinstance(meta.get('uid'),str) and meta['uid']
                and not meta.get('deletionTimestamp') and status['phase']=='Running')
            cs=[c for c in status['containerStatuses'] if c['name']==container];need(len(cs)==1);c=cs[0]
            need(c.get('ready') is True and type(c.get('restartCount')) is int and c['restartCount']>=0
                and isinstance(c.get('containerID'),str) and c['containerID'] and c.get('imageID')
                and seconds(c['state']['running']['startedAt'])<=seconds(started))
            return {'podUid':meta['uid'],'containerId':c['containerID'],'imageId':c['imageID'],
                    'restartCount':c['restartCount'],'startedAt':c['state']['running']['startedAt']}
        try:
            amf=select(pods,'free5gc-amf');amf_id=identity(amf,'amf')
        except Exception:return {name:unavailable() for name in SLICES}
        def collect_one(name):
            try:
                app='gnb-'+name;pod=select(pods,app);ident=identity(pod,app)
                prefix=['exec',pod['metadata']['name'],'-c',app,'--']
                script=r'''set -eu
enc(){ base64 | tr -d '\n'; }
node=$(/ueransim/nr-cli --dump)
status=$(/ueransim/nr-cli "$node" --exec status)
amfs=$(/ueransim/nr-cli "$node" --exec amf-list)
id=$(printf '%s\n' "$amfs" | awk '/^- id:/ {print $3; exit}')
[ -n "$id" ]
info=$(/ueransim/nr-cli "$node" --exec "amf-info $id")
printf '%s\t' "$node"
cat /etc/cityverse/gnb.yaml | enc; printf '\t'
printf '%s' "$status" | enc; printf '\t'
printf '%s' "$amfs" | enc; printf '\t'
printf '%s' "$info" | enc; printf '\t'
cat /proc/net/sctp/assocs | enc; printf '\n'
'''
                fields=run(prefix+['sh','-c',script]).strip().split('\t')
                need(len(fields)==6)
                node=fields[0];need(bool(re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',node)))
                decoded=[base64.b64decode(value,validate=True).decode() for value in fields[1:]]
                config,status,amfs,info=map(yaml.safe_load,decoded[:4]);sctp=decoded[4]
                need(isinstance(amfs,list) and len(amfs)==1 and type(amfs[0].get('id')) is int)
                return name,{'config':config,'status':status,'amfs':amfs,'info':info,'sctp':sctp,'identity':ident}
            except Exception:return name,None
        with ThreadPoolExecutor(max_workers=4) as executor:
            facts=dict(executor.map(collect_one,SLICES))
        try:
            amf_sctp=run(['exec',amf['metadata']['name'],'-c','amf','--','cat','/proc/net/sctp/assocs'])
            after=json.loads(run(['get','pods','-o','json']))['items']
            need(identity(select(after,'free5gc-amf'),'amf')==amf_id)
            result={}
            for name,values in facts.items():
                try:
                    need(values is not None and identity(select(after,'gnb-'+name),'gnb-'+name)==values['identity'])
                    peer=current_ngap_peer(values['config'],values['status'],values['amfs'],values['info'],values['sctp'],amf_sctp)
                    result[name]={'ngapAssociated':True,'available':True,'source':'native-ngap-sctp/v2','reason':None,
                        'observedAt':started,'podUid':values['identity']['podUid'],'gnbIncarnation':values['identity'],
                        'amfIncarnation':amf_id,'peer':peer}
                except Exception:result[name]=unavailable()
            return result
        except Exception:return {name:unavailable() for name in SLICES}

    def _gnb_free_ran(self, pods):
        """Current free-ran-ue API plus reciprocal kernel SCTP, never a TCP-ready proxy."""
        if (os.environ.get('CITYVERSE_ONPREM_SPATIAL_RAN_CONFIG')
                or os.environ.get('CITYVERSE_ONPREM_CITY_VALUES')):
            from backend.onprem.runtime.spatial_ran import configured_registry, native_readouts
            catalog = configured_registry()
            native = native_readouts(self.driver, catalog, pods=pods)
            result = {}
            for name in SLICES:
                rows = [r for r in catalog['gnbs'] if name in r['slices']]
                primary = next((r for r in rows if r['app'] == 'gnb-' + name), None)
                if primary is None:
                    raise EvidenceUnavailable('Commissioned primary slice gNB unavailable')
                proofs = [native[r['gnbId']] for r in rows]
                result[name] = dict(native[primary['gnbId']], spatialReadouts=proofs,
                    ueCount=len({s for proof in proofs for s in proof['supis']}))
            return result
        started = timestamp()
        deadline = time.monotonic() + 15
        def unavailable():
            return {'ngapAssociated': False, 'available': False,
                    'source': 'free-ran-ue-api+native-ngap-sctp/v1',
                    'reason': 'current_ngap_peer_unavailable', 'observedAt': started}
        def run(args):
            command = ['kubectl', '--context', self.driver.context,
                       '--namespace', self.driver.namespace, *args]
            for attempt in range(2):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    acquired=args[0]!='exec' or RAN_EXEC_SLOTS.acquire(timeout=remaining)
                    if not acquired:break
                    try:
                        remaining=deadline-time.monotonic()
                        if remaining<=0:raise EvidenceUnavailable('Free-RAN gNB observation transport unavailable')
                        result = self.runner(command, text=True, capture_output=True,
                                             timeout=min(5, remaining), check=False)
                    finally:
                        if args[0]=='exec':RAN_EXEC_SLOTS.release()
                except subprocess.TimeoutExpired:
                    result = None
                if (result is not None and result.returncode == 0
                        and len(result.stdout.encode()) <= 1024 * 1024):
                    return result.stdout
                if attempt == 0 and deadline - time.monotonic() > 0:
                    continue
                break
            raise EvidenceUnavailable('Free-RAN gNB observation transport unavailable')
        def one_pod(app):
            matches = [pod for pod in pods if pod.get('metadata', {}).get('labels', {}).get('app.kubernetes.io/name') == app]
            if len(matches) != 1:
                raise EvidenceUnavailable('Unique RAN/AMF Pod unavailable')
            return matches[0]
        def identity(pod, container):
            meta, status = pod['metadata'], pod['status']
            matches = [row for row in status['containerStatuses'] if row['name'] == container]
            if (meta.get('namespace') != self.driver.namespace or not meta.get('uid')
                    or meta.get('deletionTimestamp') or status.get('phase') != 'Running'
                    or len(matches) != 1 or matches[0].get('ready') is not True
                    or not matches[0].get('containerID') or not matches[0].get('imageID')
                    or type(matches[0].get('restartCount')) is not int):
                raise EvidenceUnavailable('Current RAN/AMF incarnation unavailable')
            return {'podUid': meta['uid'], 'containerId': matches[0]['containerID'],
                    'imageId': matches[0]['imageID'], 'restartCount': matches[0]['restartCount']}
        direct = getattr(self, 'ran_evidence_transport', 'kubectl') == 'pod-http'
        def read_evidence(pod):
            if direct:
                return _ran_http_evidence(pod, deadline)
            return run(['get', '--raw',
                f"/api/v1/namespaces/{self.driver.namespace}/pods/{pod['metadata']['name']}:9083/proxy/v1/evidence"])
        try:
            amf = one_pod('free5gc-amf')
            amf_id = identity(amf, 'amf')
            amf_snapshot = json.loads(read_evidence(amf))
            if (amf_snapshot.get('schemaVersion') != 'cityverse.ran-evidence/v1'
                    or amf_snapshot.get('role') != 'amf'
                    or not -15 <= seconds(started) - seconds(amf_snapshot.get('observedAt')) <= 15):
                raise EvidenceUnavailable('AMF evidence endpoint unavailable or stale')
            amf_sctp = amf_snapshot['sctp']
            def read_one(name):
                try:
                    pod = one_pod('gnb-' + name)
                    ident = identity(pod, 'gnb-' + name)
                    observed = json.loads(read_evidence(pod))
                    if (observed.get('schemaVersion') != 'cityverse.ran-evidence/v1'
                            or observed.get('role') != 'gnb'
                            or not -15 <= seconds(started) - seconds(observed.get('observedAt')) <= 15):
                        raise EvidenceUnavailable('gNB evidence endpoint unavailable or stale')
                    cfg = observed['config']
                    info = observed['info']['gnbInfo']
                    expected_slice = SLICES[name]
                    if (info.get('gnbName') != cfg['gnbName'] or info.get('plmnId') != '20893'
                            or info.get('snssai') != {'sst': str(expected_slice['sst']),
                                                      'sd': expected_slice['sd']}
                            or not isinstance(info.get('ranUeList'), list)):
                        raise EvidenceUnavailable('Free-RAN gNB API identity mismatch')
                    try:
                        imsis = [canonical_fru_imsi(row.get('imsi')) for row in info['ranUeList']]
                    except (AttributeError, ValueError):
                        raise EvidenceUnavailable('Free-RAN gNB UE context malformed')
                    if len(imsis) != len(set(imsis)):
                        raise EvidenceUnavailable('Free-RAN gNB UE context malformed')
                    local = str(ipaddress.IPv4Address(cfg['ranN2Ip']))
                    remote = str(ipaddress.IPv4Address(cfg['amfN2Ip']))
                    port = cfg['amfN2Port']
                    gnb_matches = [row for row in sctp_associations(observed['sctp'])
                                   if row['state'] == 3 and row['local'] == [local]
                                   and row['remote'] == [remote] and row['remotePort'] == port]
                    if len(gnb_matches) != 1:
                        raise EvidenceUnavailable('Free-RAN gNB SCTP association unavailable')
                    association = gnb_matches[0]
                    amf_matches = [row for row in sctp_associations(amf_sctp)
                                   if row['state'] == 3 and row['local'] == [remote]
                                   and row['remote'] == [local] and row['localPort'] == port
                                   and row['remotePort'] == association['localPort']]
                    if len(amf_matches) != 1:
                        raise EvidenceUnavailable('Free-RAN reciprocal AMF SCTP unavailable')
                    return name, {'ngapAssociated': True, 'available': True,
                                  'source': 'free-ran-ue-api+native-ngap-sctp/v1',
                                  'reason': None, 'observedAt': started,
                                  'podUid': ident['podUid'], 'gnbIncarnation': ident,
                                  'amfIncarnation': amf_id, 'ueCount': len(imsis),
                                  'peer': {'address': remote, 'port': port, 'gnbAddress': local,
                                           'gnbPort': association['localPort'],
                                           'gnbAssociationId': association['id'],
                                           'amfAssociationId': amf_matches[0]['id'],
                                           'ngSetupState': 'CONNECTED'}}
                except Exception:
                    return name, unavailable()
            # The kubelet streaming endpoint on the single RAN node reliably
            # returns two concurrent exec streams; four-way config/evidence
            # bursts can execute in-container yet lose stdout at the client.
            with ThreadPoolExecutor(max_workers=2) as executor:
                result = dict(executor.map(read_one, SLICES))
            reader = getattr(self, 'pod_reader', None)
            fresh = (reader.pods(deadline=deadline)['items'] if reader is not None
                     else json.loads(run(['get', 'pods', '-o', 'json']))['items'])
            current_amf = [p for p in fresh if p.get('metadata', {}).get('uid') == amf['metadata']['uid']][0]
            if (identity(current_amf, 'amf') != amf_id
                    or direct and _ran_pod_ip(current_amf) != _ran_pod_ip(amf)):
                raise EvidenceUnavailable('AMF incarnation changed during RAN read')
            for name, item in result.items():
                if item['available']:
                    old = one_pod('gnb-' + name)
                    current = [p for p in fresh if p.get('metadata', {}).get('uid') == old['metadata']['uid']]
                    if (len(current) != 1 or identity(current[0], 'gnb-' + name) != item['gnbIncarnation']
                            or direct and _ran_pod_ip(current[0]) != _ran_pod_ip(old)):
                        result[name] = unavailable()
            # Successful rows describe the completed coherent proof, not the
            # beginning of its bounded multi-endpoint collection.  Failure
            # rows retain ``started`` so delay cannot make them look fresh.
            completed = timestamp()
            for item in result.values():
                if item['available']:
                    item['observedAt'] = completed
            return result
        except Exception:
            return {name: unavailable() for name in SLICES}

    def observe(self, generation):
        return self._shared_observation(generation, release_only=False)

    def _shared_observation(self, generation, *, release_only):
        """Share only a concurrent identical capture; preserve every source clock."""
        if type(generation) is not int or generation < 0:
            raise EvidenceUnavailable('No matching durable generation')
        deadline = time.monotonic() + 12
        key = (generation, release_only)
        with self._observation_lock:
            flight = self._observation_flights.get(key)
            leader = flight is None or flight.done()
            if leader:
                flight = Future()
                self._observation_flights[key] = flight
        if not leader:
            _, pending = wait((flight,), timeout=max(0., deadline - time.monotonic()))
            if pending or time.monotonic() >= deadline:
                raise EvidenceUnavailable('Native evidence observation wait exceeded deadline')
            # A caller's cancellation/timeout never cancels another reader.
            return deepcopy(flight.result())
        try:
            result = self._observe(generation, release_only=release_only)
            own_result = deepcopy(result)
            flight.set_result(result)
            return own_result
        except BaseException as error:
            flight.set_exception(error)
            raise
        finally:
            with self._observation_lock:
                if self._observation_flights.get(key) is flight:
                    del self._observation_flights[key]

    def _nas_states(self, pods, generation, cancel, deadline):
        """All owned NAS nodes, including those without SMF; unknown is not health."""
        if getattr(self.driver, 'backend', None) == 'free-ran-ue':
            # No per-UE NAS status endpoint exists. AMF/SMF/native TUN and
            # gNB API observations remain independent; do not synthesize NAS.
            return {}
        def one(pod):
            meta = pod['metadata']
            if (cancel.is_set() or time.monotonic() >= deadline
                    or meta.get('deletionTimestamp') or pod.get('status', {}).get('phase') != 'Running'
                    or meta.get('labels', {}).get('cityverse.io/generation') != str(generation)):
                return {}
            observed = timestamp()  # Original capture start, never renewed.
            try:
                fresh = self.driver.kubectl('get', 'pod', meta['name'], '-o', 'json')
                current = fresh.get('metadata', {})
                if (current.get('uid') != meta.get('uid') or not meta.get('uid')
                        or current.get('labels', {}).get('cityverse.io/managed-by') != 'onprem-ue-controller'
                        or current.get('labels', {}).get('cityverse.io/generation') != str(generation)
                        or current.get('annotations', {}).get('cityverse.io/supis') != meta.get('annotations', {}).get('cityverse.io/supis')
                        or current.get('deletionTimestamp') or fresh.get('status', {}).get('phase') != 'Running'):
                    return {}
                supis = json.loads(meta['annotations']['cityverse.io/supis'])
                if (not isinstance(supis, list) or not 1 <= len(supis) <= 250
                        or len(set(supis)) != len(supis)
                        or any(not isinstance(supi, str) or not re.fullmatch(r'imsi-[0-9]{15}', supi) for supi in supis)):
                    return {}
                states = self.driver.ue_states(meta['name'], supis)
                after = self.driver.kubectl('get', 'pod', meta['name'], '-o', 'json').get('metadata', {})
                if (after.get('uid') != meta['uid']
                        or after.get('labels', {}).get('cityverse.io/managed-by') != 'onprem-ue-controller'
                        or after.get('labels', {}).get('cityverse.io/generation') != str(generation)
                        or after.get('annotations', {}).get('cityverse.io/supis') != meta['annotations']['cityverse.io/supis']
                        or after.get('deletionTimestamp')):
                    return {}
                if set(states) != set(supis) or cancel.is_set() or time.monotonic() >= deadline:
                    return {}
                output = {}
                for supi, state in states.items():
                    values = [state.get(key) for key in ('rm-state', 'mm-state', '5u-state')]
                    if any(not isinstance(value, str) or len(value) > 80 for value in values):
                        continue
                    output[supi] = dict(source='owned-ueransim-nr-cli-status', podUid=meta['uid'],
                        supi=supi, generation=generation, observedAt=observed,
                        rmState=values[0], mmState=values[1], updateState=values[2])
                return output
            except Exception:
                return {}  # Raw CLI/errors never leave this boundary.
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(one, pods))
        return {supi: state for result in results for supi, state in result.items()}

    def observe_release(self, generation):
        """Native release proof cannot depend on already-disabled UE traffic.

        Keep OAM, kernel residuals and generation/Pod UID fencing unchanged;
        mark traffic unavailable so this snapshot cannot prove establishment.
        """
        return self._shared_observation(generation, release_only=True)

    @staticmethod
    def _probe_inputs(current, pods, native, *, release_only):
        """Join owned Pods and SMF targets without waiting for kernel dumps."""
        owned, rows = {}, {}
        for pod in pods:
            meta = pod.get('metadata', {})
            if meta.get('labels', {}).get('cityverse.io/managed-by') != 'onprem-ue-controller':
                continue
            supis = json.loads(meta.get('annotations', {}).get('cityverse.io/supis', '[]'))
            if (not isinstance(supis, list) or not 1 <= len(supis) <= 250
                    or any(not isinstance(supi, str) or not re.fullmatch(r'imsi-[0-9]{15}', supi) for supi in supis)):
                raise EvidenceUnavailable('Owned UE Pod has malformed SUPI allocation')
            for supi in supis:
                if supi in owned:
                    raise EvidenceUnavailable('A SUPI is owned by multiple UE Pods')
                owned[supi] = pod
        for item in native['ues']:
            row = deepcopy(item)
            supi = row['supi']
            if supi not in rows:
                rows[supi] = row
                continue
            previous = rows[supi]
            if (not release_only or previous.get('amf', {}).get('registered') is row.get('amf', {}).get('registered')
                    or {previous.get('amf', {}).get('registered'), row.get('amf', {}).get('registered')} != {True, False}):
                raise EvidenceUnavailable('Duplicate native OAM identity')
            active, residual = (previous, row) if previous['amf']['registered'] else (row, previous)
            rows[supi] = {**active, 'smf': residual.get('smf', active.get('smf', {}))}
        batches = defaultdict(list)
        allocated = {item['supi']: item['slice'] for item in current['allocation']}
        for supi in set(rows) | set(owned):
            row = rows.setdefault(supi, {'supi': supi})
            pod = owned.get(supi)
            row['generation'] = int(pod['metadata']['labels']['cityverse.io/generation']) if pod else -1
            smf = row.get('smf', {})
            if pod and smf.get('established') is True and supi in allocated:
                batches[pod['metadata']['name']].append(
                    {'supi': supi, 'smf': deepcopy(smf), 'endpoint': ENDPOINTS[allocated[supi]]})
        return owned, rows, batches

    def _observe(self, generation, *, release_only):
        current = self.store.current()
        if current is None and generation == 0:
            current = {"generation": 0, "allocation": []}
        if current is None or current["generation"] != generation:
            raise EvidenceUnavailable("No matching durable generation")
        if not release_only and any('device' in row for row in current['allocation']):
            observer=getattr(self,'device_observer',None)
            if observer is None or not callable(getattr(observer,'observe',None)):
                raise EvidenceUnavailable('Native device observer is not commissioned')
            # Before Pods exist the ordinary native inventory authorizes launch;
            # it cannot satisfy the strict v2 readiness verifier. Once every
            # device Pod exists, only the exact native multi-PDU join is accepted.
            from backend.onprem.runtime.ue import shards
            expected={f"cv-ue-g{generation}-{unit.get('podIndex',index)}" for index,unit in enumerate(shards(current['allocation'],self.driver.batch_size))}
            present={p.get('metadata',{}).get('name') for p in self._pod_list(deadline=time.monotonic()+5,managed=True).get('items',[])}
            if expected <= present:
                return observer.observe(generation)
        cancel = Event()
        snapshot_started = time.monotonic()
        deadline = snapshot_started + 12
        started = timestamp()
        phase = "native_sources"
        observation_id = uuid.uuid4().hex[:12]
        source_lock = Lock()
        source_timings = {name: {"state": "pending"} for name in (
            "kubernetes_pods", "native_oam", "subscriber_pool",
            "gnb_ngap", "ue_nas", "ue_tun_probes", "generation_uid_recheck",
            *(f"native_upf_{name}" for name in SLICES))}
        if release_only:
            for name in ("subscriber_pool", "gnb_ngap", "ue_nas", "ue_tun_probes"):
                source_timings[name] = {"state": "skipped"}
        failure_type = None

        def timed_source(name, function, *args, **kwargs):
            source_started = time.monotonic()
            with source_lock:
                source_timings[name] = {"state": "running"}
            try:
                value = function(*args, **kwargs)
            except Exception as error:
                with source_lock:
                    source_timings[name] = {"state": "failed",
                        "elapsedMs": round((time.monotonic() - source_started) * 1000),
                        "errorType": _timing_error_type(error)}
                raise
            with source_lock:
                source_timings[name] = {"state": "complete",
                    "elapsedMs": round((time.monotonic() - source_started) * 1000)}
            return value

        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                native_future = executor.submit(timed_source, "native_oam", self.native.collect,
                                                cancel=cancel, deadline=deadline)
                subscriber_future = None if release_only else executor.submit(
                    timed_source, "subscriber_pool", self.subscribers.collect)
                upf_futures = {name: executor.submit(timed_source, f"native_upf_{name}", self._upf, name)
                               for name in SLICES}
                pod_future = executor.submit(timed_source, "kubernetes_pods", self._pod_list,
                                             deadline=deadline)
                phase = "kubernetes_pods"
                pods = pod_future.result()["items"]
                phase = "native_oam"
                native = native_future.result()
                phase = "subscriber_pool"
                subscribers = subscriber_future.result() if subscriber_future else {}
                phase = 'pod_ownership'
                owned, rows, batches = self._probe_inputs(current, pods, native, release_only=release_only)
                pod_by_name = {pod['metadata']['name']: pod for pod in owned.values()}
                # Probe inputs depend on Pod ownership and SMF, not UPF dumps.
                # Start these independent reads on the same capture deadline;
                # publish only after their complete cross-source join below.
                gnb_future = None if release_only else executor.submit(
                    timed_source, 'gnb_ngap', self._gnb, pods)
                nas_future = None if release_only else executor.submit(
                    timed_source, 'ue_nas', self._nas_states, list(pod_by_name.values()), generation, cancel, deadline)
                probe_future = None if release_only else executor.submit(
                    timed_source, 'ue_tun_probes', self.prober.probe_many,
                    [(pod_by_name[name], group) for name, group in batches.items()], cancel=cancel, deadline=deadline)
                dumps = {}
                try:
                    for name, future in upf_futures.items():
                        phase = f"native_upf_{name}"
                        dumps[name] = future.result()
                    phase = 'ue_tun_probes'
                    probed = probe_future.result() if probe_future else []
                    nas_states = nas_future.result() if nas_future else {}
                    phase = 'gnb_ngap'
                    gnb = gnb_future.result() if gnb_future else {}
                except Exception:
                    cancel.set()
                    raise
            if time.monotonic() >= deadline:
                raise EvidenceUnavailable("Native source snapshot exceeded freshness deadline")
            phase = "source_join"
            # _probe_inputs already rejects duplicate native SUPIs. Its rows
            # also include owned UEs not yet present in OAM; those must remain
            # observable as pending rather than invalidate the entire snapshot.
            sessions = {}
            residual = []
            for name, dump in dumps.items():
                for session in dump["sessions"]:
                    ip = session["ip"]
                    if ip in sessions or session.get("upf") != f"upf-{name}":
                        raise EvidenceUnavailable("Duplicate or incorrectly attributed UPF PDU IP")
                    sessions[ip] = session
                residual.extend(dump["residuals"])
            matched_ips = set()
            for supi in set(rows) | set(owned):
                row = rows.setdefault(supi, {"supi": supi})
                pod = owned.get(supi)
                row["generation"] = int(pod["metadata"]["labels"]["cityverse.io/generation"]) if pod else -1
                smf = row.get("smf", {})
                upf = sessions.get(smf.get("ip"))
                if upf and smf.get("sessionId"):
                    matched_ips.add(smf["ip"])
                    row["upf"] = {**upf, "sessionId": smf["sessionId"]}
                    smf["upf"] = upf["upf"]
            if not release_only and generation == 0 and not all(item.get("ngapAssociated") is True for item in gnb.values()):
                raise EvidenceUnavailable("Native bootstrap awaits all four gNB NGAP associations")
            for probe in probed:
                rows[probe["supi"]].update(probe)
            for supi, state in nas_states.items():
                rows[supi]['nas'] = state
                rows[supi]['ownedPodUid'] = owned[supi]['metadata']['uid']
            # Preserve every unresolved kernel context even without a SUPI;
            # verifier treats these as residual runtime identities, blocking zero.
            for index, state in enumerate([*residual, *(value for ip, value in sessions.items() if ip not in matched_ips)]):
                key = f"residual-upf-{index}"
                rows[key] = {"supi": key, "generation": -1, "upf": state}
            latest = self.store.current()
            phase = "generation_uid_recheck"
            current_pods = timed_source("generation_uid_recheck", self._pod_list,
                                        deadline=deadline, managed=True)["items"]
            current_uids = {pod["metadata"]["name"]: pod["metadata"]["uid"] for pod in current_pods}
            original_uids = {pod["metadata"]["name"]: pod["metadata"]["uid"] for pod in pods if pod.get("metadata", {}).get("labels", {}).get("cityverse.io/managed-by") == "onprem-ue-controller"}
            latest_generation = latest["generation"] if latest else 0
            if latest_generation != generation or current_uids != original_uids:
                cancel.set()
                raise EvidenceUnavailable("Generation or Pod UID changed during observation")
            if time.monotonic() >= deadline:
                raise EvidenceUnavailable("Native evidence snapshot exceeded freshness deadline")
            sources = dict(native["sources"])
            ue_observed = (min(row["ue"]["observedAt"] for row in probed) if probed else timestamp())
            ue_source = ('kubernetes-uid:free-ran-ue-tun' if getattr(self.driver, 'backend', None) == 'free-ran-ue'
                         else 'kubernetes-uid:ueransim-nr-cli')
            sources.update({"upf": {"available": True, "source": "gtp5g-netlink:four-upf", "epoch": json.dumps({name: dump["epoch"] for name, dump in dumps.items()}, sort_keys=True), "observedAt": min(dump["observedAt"] for dump in dumps.values())},
                            "ue": {"available": True, "source": ue_source, "observedAt": ue_observed},
                            "traffic": {"available": not release_only, "source": "not-collected:release-only" if release_only else "ue-tun:icmp-request-reply", "observedAt": ue_observed}})
            sources['upf']['inventories']={name:{'epoch':dump['epoch'],'sessionCount':len(dump['sessions']),
                'residualCount':len(dump['residuals']),'observedAt':dump['observedAt']} for name,dump in dumps.items()}
            return {"schemaVersion": "cityverse.evidence/v1", "generation": generation, "observedAt": timestamp(),
                    "captureDurationMs": round((time.monotonic() - snapshot_started) * 1000),
                    "sources": sources, "ues": list(rows.values()), "gnb": gnb, "subscriberPool": subscribers}
        except Exception as exc:
            cancel.set()
            failure_type = _timing_error_type(exc)
            # Only a fixed phase and exception class cross the API boundary;
            # raw database/HTTP errors can contain credentials or private URLs.
            raise EvidenceUnavailable(f"Native evidence unavailable: phase={phase}; errorType={type(exc).__name__}") from None
        finally:
            try:
                finished = time.monotonic()
                elapsed_ms = round((finished - snapshot_started) * 1000)
                if failure_type is not None or elapsed_ms >= 500:
                    with source_lock:
                        sources = {name: dict(value) for name, value in source_timings.items()}
                    row = {"wallTimeUnixMs": round(time.time() * 1000),
                           "observationId": observation_id, "phase": phase,
                           "outcome": "failed" if failure_type is not None else "complete",
                           "elapsedMs": elapsed_ms,
                           "deadlineRemainingMs": round((deadline - finished) * 1000),
                           "sourceMs": sources,
                           "errorType": failure_type}
                    logger.warning("native_observation_timing %s", json.dumps(row, sort_keys=True))
            except Exception:
                pass  # Diagnostic logging must never alter native evidence semantics.


def authoritative_inventory_options(environ, smf_url):
    """Translate supervisor-only opt-in settings into constructor arguments."""
    authoritative = environ.get("CITYVERSE_ONPREM_SMF_INVENTORY_ENABLED", "false").lower() == "true"
    if not authoritative:
        return {"authoritative_inventory": False, "inventory_token": ""}
    image = environ.get("CITYVERSE_ONPREM_SMF_INVENTORY_IMAGE", "")
    endpoint = environ.get("CITYVERSE_ONPREM_SMF_INVENTORY_URL", "")
    token = environ.get("CITYVERSE_ONPREM_SMF_INVENTORY_TOKEN", "")
    if not re.fullmatch(r"docker\.io/cityverse/free5gc-smf@sha256:[0-9a-f]{64}", image):
        raise ValueError("Authoritative SMF inventory requires the exact immutable custom image reference")
    if endpoint != smf_url.rstrip("/") + "/nsmf-oam/v1/sm-contexts":
        raise ValueError("Authoritative SMF inventory endpoint must exactly match the configured SMF origin")
    if not token:
        raise ValueError("Authoritative SMF inventory token is required")
    return {"authoritative_inventory": True, "inventory_token": token, "inventory_image": image}


def verify_authoritative_smf_runtime(driver, image):
    """Read-only identity fence for the configured Deployment and ready Pod."""
    deployment = driver.kubectl("get", "deployment", "smf", "-o", "json")
    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    selected = [item for item in containers if item.get("name") == "smf"]
    if len(selected) != 1 or selected[0].get("image") != image:
        raise ValueError("Authoritative SMF inventory Deployment image mismatch")
    pods = driver.kubectl("get", "pods", "-l", "app.kubernetes.io/name=smf", "-o", "json").get("items", [])
    ready = []
    for pod in pods:
        if pod.get("metadata", {}).get("deletionTimestamp"):
            continue
        spec = [item for item in pod.get("spec", {}).get("containers", []) if item.get("name") == "smf"]
        status = [item for item in pod.get("status", {}).get("containerStatuses", []) if item.get("name") == "smf"]
        if len(spec) == len(status) == 1 and status[0].get("ready") is True:
            ready.append((spec[0], status[0]))
    digest = image.rsplit("@", 1)[1]
    if (len(ready) != 1 or ready[0][0].get("image") != image
            or not str(ready[0][1].get("imageID", "")).endswith("@" + digest)):
        raise ValueError("Authoritative SMF inventory ready Pod image identity mismatch")


def configured_native_collector(store, driver=None):
    if os.environ.get("CITYVERSE_ONPREM_EVIDENCE_MODE") != "native":
        return None
    from pymongo import MongoClient
    driver = driver or KubeadmDriver(os.environ.get("CITYVERSE_ONPREM_KUBE_CONTEXT", ""), os.environ.get("CITYVERSE_ONPREM_NAMESPACE", "free5gc-onprem"))
    mongo = MongoClient(os.environ["CITYVERSE_ONPREM_MONGO_URI"], serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, socketTimeoutMS=3000)
    reader = NativeSubscriberReader(mongo[os.environ.get("CITYVERSE_ONPREM_MONGO_DATABASE", "free5gc")], credentials(Path(os.environ["CITYVERSE_ONPREM_CREDENTIALS_FILE"])))
    smf_url = os.environ["CITYVERSE_ONPREM_SMF_OAM_URL"].rstrip("/")
    inventory_options = authoritative_inventory_options(os.environ, smf_url)
    inventory_image = inventory_options.pop("inventory_image", None)
    if inventory_image is not None:
        verify_authoritative_smf_runtime(driver, inventory_image)
    native = NativeOamAdapter(os.environ["CITYVERSE_ONPREM_AMF_OAM_URL"], smf_url,
        os.environ.get("CITYVERSE_ONPREM_OAM_TOKEN", ""), reference_db=store.path.with_suffix(".oam.sqlite3"),
        **inventory_options)
    urls = json.loads(os.environ.get("CITYVERSE_ONPREM_UPF_EVIDENCE_URLS", "{}"))
    if not urls:
        urls = {name: f"http://upf-{name}-evidence.{driver.namespace}.svc:9081" for name in SLICES}
    collector=NativeCollector(store, driver, native, reader, urls)
    if getattr(driver,'device_profile',None) is not None:
        from backend.onprem.intents.integration import load_configuration
        from backend.onprem.evidence.device_sessions import configured_native_device_observer
        collector.device_observer=configured_native_device_observer(collector,load_configuration())
    return collector
