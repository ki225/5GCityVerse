"""Provision native-runtime inputs outside the Helm chart; never launch UEs.

The chart owns NF workloads. This operator command owns private credentials,
the existing stdlib UPF exporter source, retained local storage prerequisites
and the existing AF callback deployment helper. Secret values never print.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import socket


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kubeconfig',default=Path('/etc/kubernetes/admin.conf'),type=Path)
    p.add_argument('--context')
    p.add_argument('--namespace',default='free5gc-onprem',choices=['free5gc-onprem'])
    p.add_argument('--node',default=socket.gethostname())
    p.add_argument('--state-directory',default=Path('/var/lib/cityverse/operator-native'),type=Path)
    p.add_argument('--smf-token-source',type=Path)
    p.add_argument('--callback-pcf-ip')
    p.add_argument('--callback-pcf-port',type=int,default=8080)
    p.add_argument('--provision-mongo-pv',action='store_true',help='Only when Helm localStorage.mongodb.enabled is false')
    p.add_argument('--apply',action='store_true')
    a=p.parse_args()
    if os.geteuid()!=0 or not a.kubeconfig.is_absolute() or not a.state_directory.is_absolute():
        p.error('Root and explicit absolute kubeconfig/private state directory are required')
    if a.state_directory.is_symlink():p.error('Private state directory cannot be a symlink')
    a.state_directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    if a.state_directory.stat().st_mode & 0o077:p.error('Private state directory must have mode 0700')
    if not a.context:
        a.context=subprocess.run(['kubectl','--kubeconfig',str(a.kubeconfig),'config','current-context'],capture_output=True,text=True,check=True,timeout=10).stdout.strip()
    k=['kubectl','--kubeconfig',str(a.kubeconfig),'--context',a.context]
    def call(args,value=None):
        r=subprocess.run(k+args,input=None if value is None else json.dumps(value),text=True,capture_output=True,timeout=30)
        if r.returncode:raise RuntimeError('Native prerequisite Kubernetes command failed: '+r.stderr[-800:])
        return r.stdout
    def apply(obj):
        if a.apply:call(['apply','-f','-'],obj)
    raw=call(['get','namespace',a.namespace,'--ignore-not-found','-o','json'])
    if raw.strip():
        ns=json.loads(raw)
        if ns['metadata'].get('labels',{}).get('cityverse.io/platform')!='kubeadm':
            raise RuntimeError('Existing namespace is not commissioned for this runtime')
    else:apply({'apiVersion':'v1','kind':'Namespace','metadata':{'name':a.namespace,'labels':{'cityverse.io/platform':'kubeadm'}}})
    node=json.loads(call(['get','node',a.node,'-o','json']))
    if node['metadata'].get('labels',{}).get('kubernetes.io/hostname')!=a.node:raise RuntimeError('Exact node hostname label required')
    def token(name,source=None,hexadecimal=False):
        path=a.state_directory/name
        if path.is_symlink():raise RuntimeError('Credential path cannot be a symlink')
        if path.exists():
            if path.stat().st_mode & 0o077:raise RuntimeError('Credential must be private')
            value=path.read_text().strip()
        else:
            if source:
                if not source.is_absolute() or source.is_symlink():raise RuntimeError('Regular absolute credential source required')
                value=source.read_text().strip()
            else:value=secrets.token_hex(32) if hexadecimal else secrets.token_urlsafe(48)
            if a.apply:
                with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:f.write(value+'\n')
        if len(value)<24 or '\n' in value:raise RuntimeError('Bounded nonempty credential required')
        if hexadecimal and (len(value)!=64 or any(c not in '0123456789abcdef' for c in value)):raise RuntimeError('Traffic manager requires a 64-character hexadecimal token')
        return value
    def secret(name,values):
        apply({'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':a.namespace,'labels':{'app.kubernetes.io/part-of':'cityverse'}},'type':'Opaque','stringData':values})
    secret('smf-qer-actuator-token',{'token':token('smf-token',a.smf_token_source)})
    secret('cityverse-smf-inventory',{'token':token('smf-inventory-token',hexadecimal=True)})
    secret('cityverse-traffic-manager',{'token':token('traffic-manager-token',hexadecimal=True)})
    source=Path(__file__).resolve().parents[2]/'integrations/upf.py'
    raw=source.read_text(encoding='utf-8');revision=hashlib.sha256(raw.encode()).hexdigest()
    apply({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'cityverse-upf-collector-source','namespace':a.namespace,'labels':{'app.kubernetes.io/part-of':'cityverse'}},'data':{'onprem__integrations__upf.py':raw}})
    ran_source=source.parents[1]/'api/evidence_endpoint.py'
    ran_raw=ran_source.read_text(encoding='utf-8')
    ran_revision=hashlib.sha256(ran_raw.encode()).hexdigest()
    apply({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'cityverse-ran-evidence-source','namespace':a.namespace,'labels':{'app.kubernetes.io/part-of':'cityverse'}},'data':{'onprem__api__evidence_endpoint.py':ran_raw}})
    scraw=call(['get','storageclass','cityverse-local','--ignore-not-found','-o','json'])
    if scraw.strip():
        sc=json.loads(scraw)
        if sc['provisioner']!='kubernetes.io/no-provisioner' or sc['volumeBindingMode']!='WaitForFirstConsumer':raise RuntimeError('Local StorageClass contract differs')
    else:apply({'apiVersion':'storage.k8s.io/v1','kind':'StorageClass','metadata':{'name':'cityverse-local'},'provisioner':'kubernetes.io/no-provisioner','volumeBindingMode':'WaitForFirstConsumer','reclaimPolicy':'Retain'})
    data=Path('/var/lib/cityverse-mongodb')
    if data.is_symlink():raise RuntimeError('MongoDB data path cannot be a symlink')
    if not data.exists() and a.apply:
        data.mkdir(mode=0o2775);os.chown(data,1001,1001)
    pv={'apiVersion':'v1','kind':'PersistentVolume','metadata':{'name':'cityverse-mongodb','labels':{'app.kubernetes.io/part-of':'cityverse'}},'spec':{'capacity':{'storage':'10Gi'},'volumeMode':'Filesystem','accessModes':['ReadWriteOnce'],'persistentVolumeReclaimPolicy':'Retain','storageClassName':'cityverse-local','local':{'path':str(data)},'claimRef':{'apiVersion':'v1','kind':'PersistentVolumeClaim','namespace':a.namespace,'name':'datadir-mongodb-0'},'nodeAffinity':{'required':{'nodeSelectorTerms':[{'matchExpressions':[{'key':'kubernetes.io/hostname','operator':'In','values':[a.node]}]}]}}}}
    old=call(['get','pv','cityverse-mongodb','--ignore-not-found','-o','json'])
    if old.strip():
        existing=json.loads(old);s=existing['spec']
        if s.get('local',{}).get('path')!=str(data) or s.get('claimRef',{}).get('namespace')!=a.namespace or s.get('persistentVolumeReclaimPolicy')!='Retain':raise RuntimeError('Existing MongoDB PV ownership differs')
    elif a.provision_mongo_pv:apply(pv)
    state=Path('/var/lib/cityverse/smf-intent-state')
    if state.is_symlink():raise RuntimeError('SMF state path cannot be a symlink')
    if a.apply:state.mkdir(parents=True,exist_ok=True,mode=0o700)
    if a.callback_pcf_ip:
        from .policy_callback import objects
        secret('cityverse-af-callback-auth',{'CITYVERSE_AF_CALLBACK_SECRET':token('af-callback-secret'),'CITYVERSE_AF_ADMIN_TOKEN':token('af-admin-token')})
        directory=Path('/var/lib/cityverse-af-callback')
        if directory.is_symlink():raise RuntimeError('Callback data path cannot be a symlink')
        if not directory.exists() and a.apply:directory.mkdir(mode=0o770);os.chown(directory,1337,1337)
        if not 1 <= a.callback_pcf_port <= 65535:p.error('Invalid PCF service port')
        for obj in objects(a.callback_pcf_ip,a.namespace,a.node,'http://'+a.callback_pcf_ip+':'+str(a.callback_pcf_port)+'/npcf-policyauthorization/v1'):apply(obj)
    report={'namespace':a.namespace,'node':a.node,'applied':a.apply,'upf_source_sha256':revision,'ran_source_sha256':ran_revision,'native_values':{'nativeRuntime':{'enabled':True,'upfEvidence':{'sourceRevision':revision},'ranEvidence':{'sourceRevision':ran_revision}}},'private_state_directory':str(a.state_directory),'retained_mongo_path':str(data),'callback_configured':bool(a.callback_pcf_ip),'no_ue_or_traffic':True}
    output=a.state_directory/'bootstrap-values.json'
    if a.apply:
        output.write_text(json.dumps(report['native_values'],indent=2)+'\n');output.chmod(0o600)
        proof=a.state_directory/'bootstrap-report.json';proof.write_text(json.dumps(report,indent=2));proof.chmod(0o600)
    print(json.dumps(report))


if __name__=='__main__':main()
