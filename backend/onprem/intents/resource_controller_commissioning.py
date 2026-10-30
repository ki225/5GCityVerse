"""Compile fixed controller slots from an operator's read-only native census.

Returns configuration only. No Kubernetes calls, credentials or native writes.
Colocated logical sites are explicitly retained; they add no physical capacity.
"""
from copy import deepcopy
import ipaddress
import json
from pathlib import Path

import yaml

from .application_controller import controlled_receiver_template


def compile_controller_configuration(inventory, native, upf_config, base_config, *, state_directory,
                                     upf_origin='http://127.0.0.1:28101',
                                     application_origin='http://127.0.0.1:28102', collector_source):
    state = Path(state_directory)
    if not state.is_absolute() or inventory.get('schema') != 'cityverse.resource-inventory/v1':
        raise ValueError('Absolute operator state directory and native inventory required')
    nodes = inventory['nodes']
    if len(nodes) != 1 or inventory['placement'].get('mode') != 'colocated':
        raise ValueError('This commissioning profile requires an explicit single-node colocated city')
    node = nodes[0]['node_id']
    pods = native['pods']; cms = {c['metadata']['name']: c for c in native['configmaps']}
    existing_addresses = set()
    for p in pods:
        for row in json.loads(p['metadata'].get('annotations', {}).get('k8s.v1.cni.cncf.io/networks', '[]')):
            existing_addresses.update(str(ipaddress.ip_interface(ip).ip) for ip in row.get('ips', []))
    reserved = set(existing_addresses)
    prefixes = []
    for c in cms.values():
        if 'upfcfg.yaml' in c.get('data', {}):
            prefixes.extend(ipaddress.ip_network(r['cidr']) for r in yaml.safe_load(c['data']['upfcfg.yaml'])['dnnList'])
    result = deepcopy(upf_config)
    result['journal'] = str(state / 'upf-controller.sqlite3')
    result['controller_token_file'] = str(state / 'resource-controller-token')
    result['templates'] = {}
    applications = dict(schema='cityverse.application-controller/v1', context=result['context'],
        namespace=result['namespace'], journal=str(state / 'application-controller.sqlite3'),
        templates={}, slots={}, controller_token_file=str(state / 'resource-controller-token'),
        binding_registry=str(state / 'service-routes.sqlite3'))
    smf_templates = {}; smf_origins = {}; resources = []; session_targets = {}
    image = next(c['image'] for p in pods if p['metadata']['name'].startswith('n6-embb-')
                 for c in p['spec']['containers'] if c['name'] == 'traffic')
    if '@sha256:' not in image:
        raise ValueError('Commissioned receiver image digest required')
    def reserve(address):
        value = str(ipaddress.ip_interface(address).ip)
        if value in reserved:
            raise ValueError('Commissioned address collision')
        reserved.add(value)
    for index, slice_id in enumerate(('embb', 'urllc', 'mmtc', 'v2x'), 1):
        original = 'upf-' + slice_id
        pod = next(p for p in pods if p['metadata']['name'].startswith(original + '-'))
        native_cfg = yaml.safe_load(cms[original + '-config']['data']['upfcfg.yaml'])
        old_node = native['smf_userplane']['upNodes'][original]
        interfaces = json.loads(pod['metadata']['annotations']['k8s.v1.cni.cncf.io/networks'])
        initial_site = inventory['city']['slices'][slice_id]['site']
        sites = (initial_site, 'distributed' if initial_site == 'central' else 'central')
        for slot_index, site in enumerate(sites):
            if site not in inventory['city']['sites']:
                raise ValueError('Uncommissioned city site')
            target = f'cv-upf-{slice_id}-{site}'
            template_id = target
            copied = deepcopy(pod)
            copied = {'apiVersion': 'v1', 'kind': 'Pod',
                'metadata': {'name': target, 'labels': {'cityverse.io/site': site,
                    'cityverse.io/resource-slot': target}}, 'spec': copied['spec']}
            spec = copied['spec']
            for key in ('nodeName', 'serviceAccount', 'serviceAccountName', 'priority', 'preemptionPolicy'):
                spec.pop(key, None)
            spec['nodeSelector'] = {'kubernetes.io/hostname': node, 'cityverse.io/gtp5g-ready': 'true'}
            configured = deepcopy(native_cfg)
            network_rows = deepcopy(interfaces)
            for row in network_rows:
                ip = ipaddress.ip_interface(row['ips'][0])
                host = 80 + (index-1)*2 + slot_index if row['interface'] == 'n4' else 80 + slot_index
                row['ips'] = [str(ipaddress.ip_interface(f'{ip.network.network_address + host}/{ip.network.prefixlen}'))]
                reserve(row['ips'][0])
            by_interface = {r['interface']: str(ipaddress.ip_interface(r['ips'][0]).ip) for r in network_rows}
            old_prefix = native_cfg['dnnList'][0]['cidr']
            old_network = ipaddress.ip_network(old_prefix)
            prefix = ipaddress.ip_network((int(old_network.network_address) + (slot_index+1)*old_network.num_addresses, old_network.prefixlen))
            if any(prefix.overlaps(p) for p in prefixes):
                raise ValueError('Commissioned UE prefix collision')
            prefixes.append(prefix)
            configured['pfcp'].update(addr=by_interface['n4'], nodeID=by_interface['n4'])
            for row in configured['gtpu']['ifList']:
                if row.get('type') == 'N3': row['addr'] = by_interface['n3']
            for row in configured['dnnList']: row['cidr'] = str(prefix)
            copied['metadata']['annotations'] = {'k8s.v1.cni.cncf.io/networks': json.dumps(network_rows)}
            config_name, collector_name = target+'-config', target+'-collector'
            for volume in spec['volumes']:
                if volume['name'] == 'config': volume['configMap']['name'] = config_name
                if volume['name'] == 'collector-source': volume['configMap']['name'] = collector_name
            for container in spec['containers']:
                if container['name'] == original:
                    container['command'] = [s.replace(old_prefix, str(prefix)) for s in container['command']]
                if container['name'] == 'upf-evidence':
                    # Listen on native N4 and Pod IP; native consumers still fence exact UID/epoch.
                    container['args'] = ['--upf', target, '--commissioned-upf', target,
                        '--interface', 'upfgtp', '--host', '0.0.0.0', '--port', '9081', '--policy-evidence']
                    container['env'].append({'name': 'POD_UID', 'valueFrom': {'fieldRef': {'fieldPath': 'metadata.uid'}}})
            configmaps = [dict(apiVersion='v1', kind='ConfigMap', metadata={'name': config_name},
                data={'upfcfg.yaml': yaml.safe_dump(configured, sort_keys=False)}),
                dict(apiVersion='v1', kind='ConfigMap', metadata={'name': collector_name},
                     data={'onprem__integrations__upf.py': collector_source})]
            result['templates'][template_id] = dict(pod=copied, configmaps=configmaps,
                node_ids=[node], ue_prefixes=[str(prefix)])
            probe = deepcopy(upf_config['path_probes'][pod['metadata']['name']])
            probe['interfaces'] = by_interface
            result['path_probes'][target] = probe
            native_node = deepcopy(old_node)
            native_node.update(nodeID=by_interface['n4'], addr=by_interface['n4'])
            for row in native_node['interfaces']:
                if row['interfaceType'] == 'N3': row['endpoints'] = [by_interface['n3']]
            for snssai in native_node['sNssaiUpfInfos']:
                for dnn in snssai['dnnUpfInfoList']: dnn['pools'] = [{'cidr': str(prefix)}]
            smf_templates[target] = {'node': native_node, 'linkTo': ['ran-access']}
            smf_origins[by_interface['n4']] = f"http://{by_interface['n4']}:9081"
            session_targets[target] = {'container': original, 'n6_interface': 'n6'}
            resources.append(dict(target_id=target, provider='upf', actions=['place_nf', 'add_upf',
                'assign_sessions', 'drain_and_remove'], template_id=template_id, node_id=node,
                namespace=result['namespace'], container=original, site_id=site, slice=slice_id,
                commissioned=True, restore_scope='owned_created_instance', placement_mode='colocated'))
            app = f'cv-app-{slice_id}-{site}'
            n6_network = ipaddress.ip_interface(next(r['ips'][0] for r in interfaces if r['interface']=='n6')).network
            n6_ip = f'{n6_network.network_address + 90 + slot_index}/{n6_network.prefixlen}'
            reserve(n6_ip)
            applications['templates'][app] = controlled_receiver_template(image,
                network_attachment='n6-'+slice_id, n6_ip_cidr=n6_ip)
            applications['slots'][app] = dict(template_id=app, node_ids=[node], pod_name=app,
                service_name=app, endpoint_id=app, n6_ip_cidr=n6_ip)
            resources.append(dict(target_id=app, provider='applications', actions=['place_application',
                'drain_application', 'remove_application'], template_id=app, node_id=node,
                namespace=result['namespace'], site_id=site, slice=slice_id, container='receiver',
                endpoint_id=app, commissioned=True, restore_scope='owned_created_instance', placement_mode='colocated'))
    for target, pin in upf_config['existing_targets'].items():
        session_targets[target] = dict(container=target, n6_interface='n6', native_upf_id=pin['native_upf_id'],
            pod_name=pin['pod_name'], pod_uid=pin['pod_uid'])
    providers = deepcopy(base_config.get('providers', {}))
    providers.update(kubernetes=dict(context=result['context'], namespace=result['namespace'],
        pod_allowlist=[r['pod_name'] for r in inventory['resources'] if r['provider']=='kubernetes'], in_place_resize=True),
        upf=dict(origin=upf_origin, token_file=result['controller_token_file'], templates=list(result['templates'])),
        applications=dict(origin=application_origin, token_file=applications['controller_token_file'], templates=list(applications['templates'])),
        service_routes=dict(context=result['context'], namespace=result['namespace'], journal_file=applications['binding_registry'], session_targets=session_targets))
    return dict(upf_controller=result, application_controller=applications,
        provider_fragment={'providers': providers, 'placement_topology': dict(context=result['context'],
            namespace=result['namespace'], configmap='cityverse-city-inventory')},
        smf_environment={'SMF_DYNAMIC_UPF_TEMPLATES': json.dumps(smf_templates, separators=(',', ':')),
                         'SMF_SESSION_QER_UPF_ORIGINS_ADDITIONS': smf_origins},
        resources=resources)
