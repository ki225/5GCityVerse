"""Strict model wire schemas derived from the existing canonical domain types.

Schema is a shape boundary, never native evidence or policy authority. Dynamic
action objects are closed against the same commissioned TargetPolicy settings
used by CapacityPolicyValidator. Domain validators remain mandatory afterward.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import fields,is_dataclass
import hashlib,json,math,types
from typing import get_type_hints,get_origin,get_args,Union
from .domain import (ServiceIntent,Objective,OBJECTIVE_UNITS,OBJECTIVE_STATISTICS,
    OBJECTIVE_SCOPES,OBJECTIVE_DIRECTIONS)
from .planning import ResourcePlan,PlanAction,ActionKind,EXECUTION_MODES


def closed(properties):
    return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}


def schema_for(annotation,overrides=None):
    overrides=overrides or {}
    if annotation in overrides:return deepcopy(overrides[annotation])
    if annotation in (str,int,float,bool,type(None)):
        return {'type':{str:'string',int:'integer',float:'number',bool:'boolean',type(None):'null'}[annotation]}
    origin=get_origin(annotation);args=get_args(annotation)
    if origin in (Union,types.UnionType):return {'anyOf':[schema_for(t,overrides) for t in args]}
    if origin in (tuple,list):return {'type':'array','items':schema_for(args[0],overrides)}
    if is_dataclass(annotation):
        hints=get_type_hints(annotation)
        props={f.name:schema_for(hints[f.name],overrides) for f in fields(annotation)}
        if annotation is Objective:
            props['statistic']={'type':'string','enum':list(OBJECTIVE_STATISTICS)}
            props['scope']={'type':'string','enum':list(OBJECTIVE_SCOPES)}
            props['direction']={'type':'string','enum':list(OBJECTIVE_DIRECTIONS)}
            return {'anyOf':[closed({**props,'metric':{'type':'string','enum':[metric]},
                'unit':{'type':'string','enum':[unit]}}) for metric,unit in OBJECTIVE_UNITS.items()]}
        if annotation is ResourcePlan:
            props['execution_mode']={'type':'string','enum':list(EXECUTION_MODES)}
        return closed(props)
    raise ValueError('Unbounded model field requires an explicit commissioned closed shape')


def schema_hash(schema):
    return hashlib.sha256(json.dumps(schema,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def strict_schema(schema):
    """Reject open or unspecified schemas before sending a model request."""
    if type(schema) is not dict:raise ValueError('Invalid strict schema')
    allowed={'type','properties','required','additionalProperties','items','anyOf','enum',
        'minItems','maxItems','minimum','maximum','minLength','maxLength'}
    if set(schema)-allowed:raise ValueError('Unsupported local strict-schema keyword')
    if 'anyOf' in schema:
        if not schema['anyOf']:raise ValueError('Empty schema union')
        for branch in schema['anyOf']:strict_schema(branch)
        return
    if schema.get('type')=='object':
        props=schema.get('properties')
        if (type(props) is not dict or schema.get('additionalProperties') is not False
                or set(schema.get('required',[]))!=set(props)):
            raise ValueError('Strict objects require all declared fields and forbid extras')
        for value in props.values():strict_schema(value)
    elif schema.get('type')=='array':strict_schema(schema.get('items'))
    elif schema.get('type') not in {'string','number','integer','boolean','null'}:
        raise ValueError('Unsupported strict schema type')


class SchemaMismatch(ValueError):
    def __init__(self,path='root'):
        super().__init__('structured_output_schema_mismatch')
        self.path=path[:256]


def validate(value,schema,path='root'):
    """Validate the API subset locally too; paths expose indices, never values."""
    if 'anyOf' in schema:
        for option in schema['anyOf']:
            try:validate(value,option,path);return
            except SchemaMismatch:pass
        raise SchemaMismatch(path)
    kind=schema.get('type')
    good={'object':type(value) is dict,'array':type(value) is list,'string':type(value) is str,
        'number':type(value) in (int,float),'integer':type(value) is int,
        'boolean':type(value) is bool,'null':value is None}.get(kind,False)
    if not good:raise SchemaMismatch(path)
    if 'enum' in schema and not any(type(value) is type(item) and value==item for item in schema['enum']):
        raise SchemaMismatch(path)
    if kind=='object':
        if set(value)!=set(schema['properties']):raise SchemaMismatch(path)
        for i,(name,child) in enumerate(schema['properties'].items()):validate(value[name],child,f'{path}.field[{i}]')
    if kind=='array':
        if not schema.get('minItems',0)<=len(value)<=schema.get('maxItems',100000):raise SchemaMismatch(path)
        for i,item in enumerate(value):validate(item,schema['items'],f'{path}.item[{i}]')
    if kind in ('number','integer'):
        if not math.isfinite(value) or not schema.get('minimum',-math.inf)<=value<=schema.get('maximum',math.inf):
            raise SchemaMismatch(path)
    if kind=='string' and not schema.get('minLength',0)<=len(value)<=schema.get('maxLength',1000000):
        raise SchemaMismatch(path)


def compiler_schema(authority=None):
    intent=schema_for(ServiceIntent)
    if authority is not None:
        for name in ('priority_policy_id','degradation_policy_id','resource_budget_id'):
            values=sorted(getattr(authority,name.removesuffix('_id')+'_ids'))
            if not values:
                intent=None;break
            intent['properties'][name]={'type':'string','enum':values}
    return closed({'intent':{'anyOf':[intent,{'type':'null'}]} if intent else {'type':'null'},
        'issues':{'type':'array','items':{'type':'string'},'maxItems':20}})


def literal_shape(value):
    if type(value) is dict:return closed({k:literal_shape(v) for k,v in value.items()})
    if type(value) is list or type(value) is tuple:
        choices=[literal_shape(v) for v in value]
        return {'type':'array','items':{'anyOf':choices} if choices else {'type':'string'},
            **({'maxItems':0} if not choices else {})}
    return dict(schema_for(type(value)),enum=[value])


def parameter_shape(target,*,pending=False):
    root=literal_shape(target.get('fixed_parameters',{}))
    for path,bounds in target.get('numeric_bounds',{}).items():
        current=root
        for name in path.split('.')[:-1]:
            props=current['properties'];current=props.setdefault(name,closed({}))
            if current.get('type')!='object':raise ValueError('Conflicting commissioned parameter paths')
        name=path.split('.')[-1];existing=current['properties'].get(name)
        if existing is not None and (existing.get('type') not in {'number','integer'}
                or any(type(v) not in (int,float) or not bounds[0]<=v<=bounds[1] for v in existing.get('enum',[]))):
            raise ValueError('Conflicting fixed/numeric commissioned parameter')
        current['properties'][name]={**(existing or {'type':'number'}),'minimum':bounds[0],'maximum':bounds[1]}
    if pending and target['action']=='assign_sessions':
        from .pending_admission import PendingAdmission,DeferredPendingAdmission
        root['properties']['pending_admissions']={'type':'array','items':{'anyOf':[
            schema_for(PendingAdmission),schema_for(DeferredPendingAdmission)]},'minItems':1,'maxItems':100}
    def require_all(node):
        if node.get('type')=='object':
            node['required']=list(node['properties'])
            for child in node['properties'].values():require_all(child)
    require_all(root)
    return root


def proposal_schema(policy_view,*,maximum_candidates=8,pending=False):
    # parameters/compensation are the only intentionally flexible domain maps.
    # Parameters are shaped by trusted policy. Compensation is owned by backend
    # receipts, and the model's existing empty compensation claim remains empty.
    branches=[]
    for target in policy_view.get('targets',[]):
        hints=get_type_hints(PlanAction)
        props={f.name:closed({}) if f.name in {'parameters','compensation'} else schema_for(hints[f.name]) for f in fields(PlanAction)}
        props['kind']={'type':'string','enum':[target['action']]}
        props['target_id']={'type':'string','enum':[target['target_id']]}
        props['parameters']=parameter_shape(target,pending=pending)
        branches.append(closed(props))
    action_schema={'anyOf':branches} if branches else closed({})
    plan=schema_for(ResourcePlan,{PlanAction:action_schema})
    plan['properties']['actions']['maxItems']=16 if branches else 0
    plan['properties']['model_response_id']={'type':'string','enum':['']}
    plan['properties']['model_name']={'type':'string','enum':['']}
    result=closed({'plans':{'type':'array','items':plan,'maxItems':maximum_candidates}})
    strict_schema(result)
    return result
