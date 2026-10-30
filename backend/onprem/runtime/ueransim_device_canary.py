"""Reversible isolated multi-NSSAI RAN objects; rendering never applies them."""
from dataclasses import dataclass
import ipaddress
import json
import re


INIT = """import json,os,re,socket,fcntl,struct
policies=json.loads(os.environ['ARP_INTERFACES'])
for policy in policies:
 name=policy['interface']
 if not re.fullmatch(r'n3[0-9]{1,2}',name): raise RuntimeError('Unowned N3 interface')
 with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
  actual=socket.inet_ntoa(fcntl.ioctl(sock.fileno(),0x8915,struct.pack('256s',name.encode()))[20:24])
 if actual!=policy['address']: raise RuntimeError('N3 interface address mismatch')
 path='/proc/sys/net/ipv4/conf/'+name+'/arp_announce'
 with open(path,'w') as f: f.write('2')
 with open(path) as f:
  if f.read().strip()!='2': raise RuntimeError('N3 ARP source policy unconfirmed')
config=json.loads(os.environ['RAN_CONFIG'])
config.update(linkIp=os.environ['POD_IP'],gtpIp=os.environ['POD_IP'],gtpAdvertiseIp=os.environ['POD_IP'])
with open('/run/gnb/gnb.yaml','w') as f: json.dump(config,f)
"""


@dataclass(frozen=True)
class IsolatedUeransimRan:
    namespace: str
    image: str
    source_revision: str
    node_selector: dict
    network_namespace: str
    n2_ip: str
    amf_n2_ip: str
    n3_addresses: dict
    slices: tuple
    nci: int

    def __post_init__(self):
        if not re.fullmatch(r'cityverse-device-[a-z0-9-]{1,40}',self.namespace):
            raise ValueError('Dedicated device canary namespace required')
        if not re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}',self.image) or not self.source_revision or not self.node_selector:
            raise ValueError('Pinned source/image and explicit node placement required')
        if not self.network_namespace or not 0<self.nci<2**36 or not self.n3_addresses or len(self.slices)<2:
            raise ValueError('Explicit isolated native multi-slice RAN topology required')
        ips=[self.n2_ip,*self.n3_addresses.values()]
        if len(set(ips))!=len(ips):
            raise ValueError('Duplicate isolated RAN addresses')
        for value in [*ips,self.amf_n2_ip]: ipaddress.IPv4Address(value)
        if len({(s['sst'],s['sd']) for s in self.slices})!=len(self.slices):
            raise ValueError('Duplicate isolated RAN S-NSSAI')

    def configuration(self):
        return dict(mcc='208',mnc='93',nci=hex(self.nci),idLength=32,tac=1,
            ngapIp=self.n2_ip,amfConfigs=[dict(address=self.amf_n2_ip,port=38412)],
            slices=list(self.slices),ignoreStreamIds=True)

    def objects(self):
        labels={'cityverse.io/managed-by':'isolated-device-ran','cityverse.io/profile':self.source_revision}
        networks=[dict(name='n2',namespace=self.network_namespace,interface='n2',ips=[self.n2_ip+'/24'])]
        networks.extend(dict(name='n3-'+group,namespace=self.network_namespace,interface='n3'+str(i),ips=[ip+'/24'])
                        for i,(group,ip) in enumerate(sorted(self.n3_addresses.items())))
        pod={'apiVersion':'v1','kind':'Pod','metadata':dict(name='gnb-device',namespace=self.namespace,
            labels=labels,annotations={'k8s.v1.cni.cncf.io/networks':json.dumps(networks)}),
            'spec':dict(restartPolicy='Never',automountServiceAccountToken=False,nodeSelector=self.node_selector,
                initContainers=[dict(name='config',image=self.image,command=['python','-c',INIT],
                    env=[dict(name='RAN_CONFIG',value=json.dumps(self.configuration())),dict(name='POD_IP',valueFrom={'fieldRef':{'fieldPath':'status.podIP'}}),
                         dict(name='ARP_INTERFACES',value=json.dumps([dict(interface='n3'+str(i),address=ip) for i,(_,ip) in enumerate(sorted(self.n3_addresses.items()))]))],
                    securityContext=dict(privileged=True,allowPrivilegeEscalation=True,readOnlyRootFilesystem=True),
                    volumeMounts=[dict(name='config',mountPath='/run/gnb')])],
                containers=[dict(name='gnb-device',image=self.image,command=['/ueransim/nr-gnb','-c','/run/gnb/gnb.yaml'],
                    securityContext=dict(allowPrivilegeEscalation=False,capabilities={'drop':['ALL']}),
                    resources={'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'1','memory':'512Mi'}},
                    volumeMounts=[dict(name='config',mountPath='/run/gnb',readOnly=True)])],
                volumes=[dict(name='config',emptyDir={'medium':'Memory'})])}
        objects=[{'apiVersion':'v1','kind':'Namespace','metadata':{'name':self.namespace,
            'labels':dict(labels,**{'cityverse.io/platform':'kubeadm'})}},pod]
        for group in ('device',):
            objects.append({'apiVersion':'v1','kind':'Service','metadata':dict(name='gnb-'+group,namespace=self.namespace,labels=labels),
                'spec':{'clusterIP':'None','selector':labels,'ports':[dict(name='rls',port=4997,targetPort=4997,protocol='UDP')]}})
        return objects

    def assert_addresses_unused(self,pods):
        """Read all namespace Pods before applying; static Multus has no allocator CAS."""
        expected={self.n2_ip,*self.n3_addresses.values()}
        for pod in pods:
            metadata=pod.get('metadata',{})
            raw=metadata.get('annotations',{}).get('k8s.v1.cni.cncf.io/network-status','[]')
            rows=json.loads(raw)
            used={str(ipaddress.ip_interface(ip).ip) for row in rows for ip in row.get('ips',[])}
            requested=metadata.get('annotations',{}).get('k8s.v1.cni.cncf.io/networks','[]')
            if requested.lstrip().startswith('['):
                used.update(str(ipaddress.ip_interface(ip).ip) for row in json.loads(requested) for ip in row.get('ips',[]))
            if expected & used:
                raise ValueError('Isolated RAN address already assigned; reservation required')
