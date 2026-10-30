"""Compare commissioned launch fields after documented Kubernetes defaults."""
from copy import deepcopy
import re

def sealed_container_fields(observed,wanted,fields):
    actual,expected=deepcopy(observed),deepcopy(wanted)
    for value in (actual,expected):
        if not value.get('resources'):value['resources']={}
        for row in value.get('env',[]):
            ref=row.get('valueFrom',{}).get('fieldRef')
            if ref is not None:ref.setdefault('apiVersion','v1')
    if expected.get('command')==['/usr/local/bin/cityverse-traffic-manager']:
        rows=[r for r in actual.get('env',[]) if r.get('name')=='CITYVERSE_TRAFFIC_MANAGER_TOKEN']
        target=[r for r in expected.get('env',[]) if r.get('name')=='CITYVERSE_TRAFFIC_MANAGER_TOKEN']
        if len(rows)!=1 or len(target)!=1 or not re.fullmatch('[a-f0-9]{64}',str(rows[0].get('value',''))):return False
        target[0]['value']=rows[0]['value']
    return all(actual.get(key)==expected.get(key) for key in fields)
