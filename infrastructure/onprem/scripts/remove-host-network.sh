#!/usr/bin/env bash
# REQ-H1: Root, explicit cluster and --apply; REQ-H2: exact lab links only.
# REQ-H3: Refuse live project namespaces/foreign NAD users; preserve shared CNI.
set -euo pipefail
usage() { echo 'Usage: sudo remove-host-network.sh --kubeconfig FILE --context NAME [--disable-autostart] [--apply]' >&2; }
kubeconfig='' context='' apply=false
disable_autostart=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kubeconfig) kubeconfig=${2:?}; shift 2 ;;
    --context) context=${2:?}; shift 2 ;;
    --apply) apply=true; shift ;;
    --disable-autostart) disable_autostart=true; shift ;;
    *) usage; exit 2 ;;
  esac
done
[[ $EUID == 0 && "$kubeconfig" == /* && -f "$kubeconfig" && -n "$context" ]] || { usage; exit 2; }
python3 - "$kubeconfig" "$context" "$apply" "$disable_autostart" <<'PY'
import json,pathlib,subprocess,sys
k=['kubectl','--kubeconfig',sys.argv[1],'--context',sys.argv[2]]
if sys.argv[4]=='true':
 unit='5gcityverse-secondary-links.service'
 def show(field):return subprocess.check_output(['systemctl','show',unit,'--value','-p',field],text=True).strip()
 fragment=show('FragmentPath')
 if fragment:
  assert fragment=='/etc/systemd/system/'+unit,'Host-network unit owner changed'
  assert show('Description')=='5GCityVerse kubeadm endpoint and same-node Multus links'
  assert 'path=/usr/local/sbin/5gcityverse-secondary-links ' in show('ExecStart'),'Host-network command changed'
  assert not show('RequiredBy'),'Other units require this host-network service'
  for f in (pathlib.Path(fragment),pathlib.Path('/usr/local/sbin/5gcityverse-secondary-links')):
   assert f.stat().st_uid==0 and not f.stat().st_mode & 0o022,'Host-network definition is not root-owned'
  if sys.argv[3]=='true':subprocess.run(['systemctl','disable','--now',unit],check=True)
  print('Verified dedicated host-network autostart:',unit)
def get(*args):return json.loads(subprocess.check_output(k+['get',*args,'-o','json']))
names={n['metadata']['name'] for n in get('namespaces')['items']}
assert not names & {'free5gc-onprem','cityverse-device-canary'},'Remove project namespaces before host links'
parents=['cv-'+n for n in ('n2','n4','n3-embb','n3-urllc','n3-mmtc','n3-v2x','n6-embb','n6-urllc','n6-mmtc','n6-v2x')]
links=json.loads(subprocess.check_output(['ip','-details','-json','link','show']))
byname={n['ifname']:n for n in links}
for nad in get('network-attachment-definitions','--all-namespaces')['items']:
 raw=json.loads(nad['spec']['config']);configs=raw.get('plugins',[raw])
 assert not any(c.get('master') in parents for c in configs),'Foreign NAD still uses a project parent'
ordered=[]
for s in ('embb','urllc','mmtc','v2x'):
 name='cv-gw-'+s;parent='cv-n6-'+s
 if name not in byname:continue
 row=byname[name]
 assert row.get('linkinfo',{}).get('info_kind')=='ipvlan','Refuse non-ipvlan gateway'
 assert parent in byname and int(pathlib.Path('/sys/class/net/'+name+'/iflink').read_text())==byname[parent]['ifindex'],'Gateway parent changed'
 ordered.append(name)
for name in parents:
 if name not in byname:continue
 assert byname[name].get('linkinfo',{}).get('info_kind')=='dummy','Refuse non-dummy project parent'
 allowed=set(ordered)
 for row in links:
  path=pathlib.Path('/sys/class/net/'+row['ifname']+'/iflink')
  if row['ifname']!=name and path.exists() and int(path.read_text())==byname[name]['ifindex']:
   assert row['ifname'] in allowed,'Foreign host interface depends on project parent'
 ordered.append(name)
print('Verified removable project links:',ordered)
if sys.argv[3]=='true':
 for name in ordered:subprocess.run(['ip','link','delete','dev',name],check=True)
 print('Removed project dummy/ipvlan links; shared bridges, ip_forward and untagged iptables preserved')
else:print('Read-only verification; add --apply to remove links')
PY
