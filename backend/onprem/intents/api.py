"""Intent HTTP boundary using the existing on-prem JSON error conventions."""
import json
import secrets
from starlette.responses import JSONResponse
from starlette.routing import Route
from backend.onprem.api.http import json_error, request_session_id
from .domain import ContractError
from .repository import IntentConflict, IntentNotFound
from .service import invoke


def public_bootstrap_operation(value):
    """Never publish original-operation held allocations or native nonces."""
    if not isinstance(value,dict) or not value.get('pretraffic_infrastructure'):
        return value
    fields={'operation_id','intent_id','revision','kind','status','stage','created_at','updated_at'}
    result={k:v for k,v in value.items() if k in fields}
    record=value['pretraffic_infrastructure']
    result['bootstrap']={'phase':record.get('phase'),'pending':record.get('phase') not in {'ready','compensated'},
                         'service_effect_verified':False}
    return result


def public_control_operation(value):
    """Separate installation, receiver effect and recovery in operation output."""
    result = public_bootstrap_operation(value)
    if not isinstance(result, dict):
        return result
    result = dict(result)
    for name in ('installation_verdict', 'routing_verdict', 'sla_verdict'):
        result.setdefault(name, 'unknown')
    result.setdefault('restore_status', 'not_required' if not value.get('receipts') else 'unknown')
    result['pending'] = value.get('status') in {'queued', 'running', 'recovery_required'}
    result['recovery_required'] = value.get('status') == 'recovery_required'
    if not value.get('graph_nodes'):
        return result
    receipts = value.get('receipts', ())
    effect = value.get('effect', {})
    result['control'] = {
        'nodes': value['graph_nodes'], 'role': value.get('graph_role'), 'phase': value.get('graph_phase'),
        'installation_verified': bool(receipts) and all(r.get('state') in {'installation_confirmed', 'effect_verified'} for r in receipts),
        'receiver_sla_verified': effect.get('met') is True and effect.get('protected_services_met') is True,
        'comparison_eligible': effect.get('comparison_eligible') is True,
        'restoration_verified': value.get('restoration_verified'),
        'recovery_required': value.get('status') == 'recovery_required',
        'desired_policy_retained': value.get('kind') != 'release' and value.get('status') == 'completed'
            and effect.get('met') is True}
    return result


def public_population_operation(value):
    """Keep native admission nonces and the immutable private lease in journals."""
    fields={'operation_id','intent_id','revision','kind','status','stage','phase',
            'created_at','updated_at','pending','generation','population_revision','idempotent_replay'}
    result={k:v for k,v in value.items() if k in fields and type(v) in (str,int,float,bool,type(None))}
    progress=value.get('population')
    if isinstance(progress,dict):
        result['population']={k:v for k,v in progress.items()
            if k in {'phase','pending','generation','population_revision','added_count','removed_count'}
            and type(v) in (str,int,float,bool,type(None))}
    if value.get('error'):
        result['error']={'code':'population_change_failed'}
    return result


class IntentController:
    def __init__(self, context):
        self.context = context

    @property
    def service(self):
        return self.context.intent_service

    def owner(self, request):
        principal = request.scope.get('user')
        if principal is not None and getattr(principal,'is_authenticated',False):
            return str(principal.identity)
        return request_session_id(request)

    def authorize_write(self, request):
        # Session headers partition records; they do not grant native mutation authority.
        expected = getattr(self.context,'intent_write_token',None)
        if not expected:
            return json_error(503,'INTENT_WRITE_NOT_CONFIGURED','Configure intent write authority before submitting operations')
        supplied = request.headers.get('authorization','')
        if not secrets.compare_digest(supplied,'Bearer '+expected):
            return json_error(403,'INTENT_WRITE_FORBIDDEN','Intent write capability is required')
        return None

    async def body(self, request):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content)>65536:
                raise ContractError('Request exceeds 64 KiB')
        value = json.loads(content)
        if not isinstance(value,dict):
            raise ContractError('Request must be an object')
        return value

    async def dispatch(self, request, action):
        if request.url.path != '/api/capabilities':
            denied = self.authorize_write(request)
            if denied:
                return denied
        try:
            result = await action()
            return JSONResponse(result,headers={'Cache-Control':'no-store'})
        except IntentNotFound:
            return json_error(404,'INTENT_NOT_FOUND','Intent or operation not found')
        except IntentConflict as error:
            return json_error(409,'INTENT_CONFLICT',str(error))
        except (ContractError,ValueError,TypeError,UnicodeDecodeError):
            return json_error(400,'INVALID_INTENT_REQUEST','Invalid intent contract, revision, pagination, or JSON')

    async def create(self, request):
        denied = self.authorize_write(request)
        if denied:
            return denied
        async def action():
            return await self.service.submit(await self.body(request),self.owner(request),request.headers.get('idempotency-key',''))
        result = await self.dispatch(request,action)
        if result.status_code==200:
            result.status_code=202
        return result

    async def list(self, request):
        async def action():
            offset,limit = int(request.query_params.get('offset','0')),int(request.query_params.get('limit','50'))
            if offset<0 or not 1<=limit<=100:
                raise ContractError('Invalid pagination')
            return {'items':await invoke(self.service.repository.list,self.owner(request),offset,limit),'offset':offset,'limit':limit}
        return await self.dispatch(request,action)

    async def get(self, request):
        async def action():
            value=await self.service.get(request.path_params['intent_id'],self.owner(request))
            latest=value.get('latest_operation')
            if isinstance(latest,dict) and latest.get('kind')=='population_change':
                value=dict(value,latest_operation=public_population_operation(latest))
            if isinstance(value.get('latest_operation'),dict):
                value=dict(value,latest_operation=public_control_operation(value['latest_operation']))
            return value
        return await self.dispatch(request,action)

    async def operation(self, request):
        async def action():
            value=await invoke(self.service.repository.operation,request.path_params['operation_id'],self.owner(request))
            return public_population_operation(value) if value.get('kind')=='population_change' else public_control_operation(value)
        return await self.dispatch(request,action)

    async def population(self, request):
        denied=self.authorize_write(request)
        if denied:
            return denied
        change=getattr(self.service,'change_population',None)
        if not callable(change):
            return json_error(503,'INTENT_POPULATION_NOT_CONFIGURED','Native population changes are not configured')
        async def action():
            from backend.onprem.scenarios.population import validate_request
            expected=request.headers.get('if-match','').strip('"')
            if not expected.isdigit() or int(expected)<1:
                raise ContractError('If-Match revision is required')
            body=validate_request(await self.body(request))
            value=await change(request.path_params['intent_id'],body,self.owner(request),
                request.headers.get('idempotency-key',''),int(expected))
            return public_population_operation(value)
        response=await self.dispatch(request,action)
        if response.status_code==200:
            response.status_code=202
        return response

    async def update(self, request):
        denied = self.authorize_write(request)
        if denied:
            return denied
        async def action():
            expected = request.headers.get('if-match','').strip('"')
            if not expected.isdigit() or int(expected)<1:
                raise ContractError('If-Match revision is required')
            return await self.service.update(request.path_params['intent_id'],await self.body(request),
                self.owner(request),request.headers.get('idempotency-key',''),int(expected))
        response = await self.dispatch(request,action)
        if response.status_code==200:
            response.status_code=202
        return response

    async def reconcile(self, request):
        return await self._operation(request)

    async def recover_baseline_preparation(self,request):
        async def action():
            body=await self.body(request)
            expected=request.headers.get('if-match','').strip('"')
            if (set(body)!={'schemaVersion','operation_id'}
                    or body.get('schemaVersion')!='cityverse.baseline-preparation-recovery-request/v1'
                    or not isinstance(body.get('operation_id'),str)
                    or not expected.isdigit() or int(expected)<1):
                raise ContractError('Exact baseline recovery operation and revision required')
            return public_control_operation(await self.service.recover_baseline_preparation(
                request.path_params['intent_id'],body['operation_id'],self.owner(request),int(expected)))
        return await self.dispatch(request,action)

    async def release(self, request):
        return await self._operation(request,release=True)

    async def _operation(self, request, *, release=False):
        denied = self.authorize_write(request)
        if denied:
            return denied
        async def action():
            raw = await request.body()
            options = None
            if raw:
                import json
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise ContractError('Invalid reconciliation body') from None
                if not isinstance(body, dict) or set(body) - {'control_options'}:
                    raise ContractError('Reconciliation accepts control_options only')
                options = body.get('control_options')
                if release and options is not None:
                    raise ContractError('Release cannot carry control_options')
            return await self.service.reconcile(request.path_params['intent_id'],self.owner(request),
                request.headers.get('idempotency-key',''),release=release,control_options=options)
        response = await self.dispatch(request,action)
        if response.status_code==200:
            response.status_code=202
        return response

    async def members(self, request):
        async def action():
            value = await self.service.get(request.path_params['intent_id'],self.owner(request))
            offset,limit = int(request.query_params.get('offset','0')),int(request.query_params.get('limit','50'))
            if offset<0 or not 1<=limit<=100:
                raise ContractError('Invalid pagination')
            membership = value.get('membership',{})
            members = membership.get('members',[])
            page=members[offset:offset+limit]
            pending=membership.get('pending_members',[])
            return dict(items=page,total=len(members),offset=offset,limit=limit,
                        rejected=membership.get('rejected',[]),
                        pending_members=[m for m in page if m in pending],
                        desired_count=membership.get('desired_count',len(members)),
                        pending_count=membership.get('pending_count',len(pending)),
                        installed_count=membership.get('installed_count'),
                        effect_invalidated=membership.get('effect_invalidated',False))
        return await self.dispatch(request,action)

    async def capabilities(self, request):
        async def action():
            value = await self.service.capabilities()
            value['write_configured']=bool(getattr(self.context,'intent_write_token',None))
            value.setdefault('population',dict(fleet_join=False,fleet_leave=False,configured=False,
                reason='native_v2_population_not_commissioned'))
            return value
        return await self.dispatch(request,action)

    def routes(self):
        return [Route('/api/intents',self.create,methods=['POST']),Route('/api/intents',self.list,methods=['GET']),
            Route('/api/intents/{intent_id:str}',self.get,methods=['GET']),
            Route('/api/intents/{intent_id:str}',self.update,methods=['PATCH']),
            Route('/api/intents/{intent_id:str}/reconcile',self.reconcile,methods=['POST']),
            Route('/api/intents/{intent_id:str}/recover-baseline-preparation',self.recover_baseline_preparation,methods=['POST']),
            Route('/api/intents/{intent_id:str}/release',self.release,methods=['POST']),
            Route('/api/intents/{intent_id:str}/population',self.population,methods=['POST']),
            Route('/api/intents/{intent_id:str}/members',self.members,methods=['GET']),
            Route('/api/operations/{operation_id:str}',self.operation,methods=['GET']),
            Route('/api/capabilities',self.capabilities,methods=['GET'])]
