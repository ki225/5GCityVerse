"""HTTP boundary for city geometry and immutable NF/application placement plans."""
from starlette.responses import JSONResponse
from starlette.routing import Route

from backend.onprem.api.http import json_error
from backend.onprem.agents.contracts import AgentLoopBlocked
from backend.onprem.agents.roles import SafeModelFailure
from .api import IntentController
from .actuators import ActuationBlocked
from .domain import ContractError
from .repository import IntentConflict, IntentNotFound
from .service import document, invoke


class PlacementController(IntentController):
    @property
    def service(self):
        return getattr(self.context, 'placement_service', None)

    async def dispatch_placement(self, request, action, *, public=False, status=200):
        if not public:
            denied = self.authorize_write(request)
            if denied is not None:
                return denied
        if self.service is None:
            return json_error(503, 'PLACEMENT_NOT_CONFIGURED', 'Placement service is not configured')
        try:
            result = await action()
            return JSONResponse(document(result), status_code=status, headers={'Cache-Control': 'no-store'})
        except IntentNotFound:
            return json_error(404, 'PLACEMENT_NOT_FOUND', 'Intent or placement plan not found')
        except IntentConflict as error:
            return json_error(409, 'PLACEMENT_CONFLICT', str(error))
        except NotImplementedError:
            return json_error(503, 'PLACEMENT_NOT_CONFIGURED', 'Placement capability is not configured')
        except SafeModelFailure:
            return json_error(503, 'PLACEMENT_MODEL_UNAVAILABLE', 'Placement model did not complete')
        except AgentLoopBlocked:
            return json_error(409, 'PLACEMENT_AGENT_BLOCKED', 'Placement agent could not validate a decision')
        except ActuationBlocked:
            return json_error(409, 'PLACEMENT_ACTUATION_BLOCKED', 'Native placement preflight or readback requires reconciliation')
        except (ContractError, ValueError, TypeError, UnicodeDecodeError):
            return json_error(400, 'INVALID_PLACEMENT_REQUEST', 'Invalid placement contract, revision, or JSON')

    async def topology(self, request):
        return await self.dispatch_placement(request, lambda: invoke(self.service.topology), public=True)

    async def update_topology(self, request):
        async def action():
            expected = request.headers.get('if-match', '').strip('"')
            if not expected.isdigit() or int(expected) < 1:
                raise ContractError('If-Match revision is required')
            return await invoke(self.service.update_topology, await self.body(request), int(expected))
        return await self.dispatch_placement(request, action)

    async def context_view(self, request):
        return await self.dispatch_placement(request, lambda: invoke(self.service.context,
            request.path_params['intent_id'], self.owner(request)))

    async def create_plan(self, request):
        async def action():
            return await invoke(self.service.plan, request.path_params['intent_id'],
                await self.body(request), self.owner(request))
        return await self.dispatch_placement(request, action)

    async def get_plan(self, request):
        return await self.dispatch_placement(request, lambda: invoke(self.service.get_plan,
            request.path_params['intent_id'], request.path_params['plan_id'], self.owner(request)))

    async def execute(self, request):
        async def action():
            key = request.headers.get('idempotency-key', '')
            if not key or len(key) > 256:
                raise ContractError('Bounded Idempotency-Key is required')
            # Execution authority comes from server-side validation of the stored
            # plan. No client flag or replacement action is accepted here.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 65536:
                    raise ContractError('Request exceeds 64 KiB')
            if bytes(body).strip() not in (b'', b'{}'):
                raise ContractError('Execution does not accept a request body')
            return await invoke(self.service.execute, request.path_params['intent_id'],
                request.path_params['plan_id'], self.owner(request), key)
        return await self.dispatch_placement(request, action, status=202)

    async def reconcile_ai(self, request):
        async def action():
            reconcile = getattr(self.service, 'reconcile_ai', None)
            if not callable(reconcile):
                raise NotImplementedError('Placement AI is not configured')
            key = request.headers.get('idempotency-key', '')
            if not key or len(key) > 256:
                raise ContractError('Bounded Idempotency-Key is required')
            return await invoke(reconcile, request.path_params['intent_id'],
                await self.body(request), self.owner(request), key)
        return await self.dispatch_placement(request, action, status=202)

    def routes(self):
        return [
            Route('/api/onprem/city-topology', self.topology, methods=['GET']),
            Route('/api/onprem/city-topology', self.update_topology, methods=['PUT']),
            Route('/api/intents/{intent_id:str}/placement-context', self.context_view, methods=['GET']),
            Route('/api/intents/{intent_id:str}/placement-reconcile', self.reconcile_ai, methods=['POST']),
            Route('/api/intents/{intent_id:str}/placement-plans', self.create_plan, methods=['POST']),
            Route('/api/intents/{intent_id:str}/placement-plans/{plan_id:str}', self.get_plan, methods=['GET']),
            Route('/api/intents/{intent_id:str}/placement-plans/{plan_id:str}/execute', self.execute, methods=['POST']),
        ]
