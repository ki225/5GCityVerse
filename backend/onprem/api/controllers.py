"""HTTP/WebSocket controllers. Native admission lives in ScenarioService."""
from __future__ import annotations
import asyncio
import json
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect
from backend.onprem.api.http import json_error, request_session_id
from backend.onprem.application.scenarios import ScenarioService
from backend.onprem.integrations.openai.openai_client import OpenAIError
from backend.onprem.runtime.errors import LocalKubernetesError
from backend.onprem.scenarios.contracts import ScenarioRequestError

class ScenarioController:
    def __init__(self, context):
        self.context = context
        self.service = ScenarioService(context)

    async def current(self, request):
        value = await self.service.current(request.query_params.get("view", "full"))
        return await asyncio.to_thread(JSONResponse, value)

    async def queue(self, request):
        return JSONResponse(await self.service.queue(request.query_params.get('requestId')), headers={'Cache-Control': 'no-store'})

    async def resources(self, request):
        return JSONResponse(await self.service.resources(request.path_params['round_id']), headers={'Cache-Control': 'no-store'})

    async def cancel_queued(self, request):
        async for _chunk in request.stream():
            pass
        return JSONResponse(await self.service.cancel_queued(request.path_params['queue_id']), status_code=202)

    async def observability(self, request):
        return JSONResponse(await self.service.observability(request.path_params["round_id"]))

    async def observation_view(self, request):
        value = await self.service.observation_view(request.path_params['round_id'],
            after=request.query_params.get('after', '0'), limit=request.query_params.get('limit', '100'),
            scenario=request.query_params.get('scenario'), view=request.query_params.get('view', 'current'))
        return await asyncio.to_thread(JSONResponse, value, headers={'Cache-Control': 'no-store'})

    async def observation_events(self, request):
        return JSONResponse(await self.service.observation_events(request.path_params["round_id"],
            after=request.query_params.get("after", "0"), limit=request.query_params.get("limit", "100"),
            cycle=request.query_params.get("cycle"), action_id=request.query_params.get("actionId"), call_id=request.query_params.get("callId"), role=request.query_params.get("role")))

    async def action_logs(self, request):
        return JSONResponse(await self.service.action_logs(request.path_params["round_id"],
            cycle=request.query_params.get("cycle"), action_id=request.query_params.get("actionId"),
            call_id=request.query_params.get("callId"), role=request.query_params.get("role"),
            after=request.query_params.get("after", "0"), limit=request.query_params.get("limit", "100")))

    async def action_evidence(self, request):
        return JSONResponse(await self.service.action_evidence(request.path_params["round_id"],
            cycle=request.query_params.get("cycle"), action_id=request.query_params.get("actionId"), call_id=request.query_params.get("callId")))

    async def observation_measurements(self, request):
        return JSONResponse(await self.service.observation_measurements(request.path_params["round_id"],
            after=request.query_params.get("after", "0"), limit=request.query_params.get("limit", "100"),
            scenario=request.query_params.get("scenario")))

    async def measurement_timings(self, request):
        return JSONResponse(await self.service.measurement_timings(request.path_params['round_id'],
            after=request.query_params.get('after', '0'), limit=request.query_params.get('limit', '10'),
            legacy_after=request.query_params.get('legacyAfter', '0')),
            headers={'Cache-Control': 'no-store'})

    async def _body(self, request):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 65536:
                raise ScenarioRequestError("REQUEST_TOO_LARGE", "Scenario request exceeds 64 KiB")
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ScenarioRequestError("INVALID_SCENARIO_ALLOCATION", "Request must be an object")
        from backend.onprem.scenarios.profile_request import parse_profile_request
        if 'intent' in value:
            binding = value.pop('intent')
            return {**parse_profile_request(value), 'intent': binding}
        return parse_profile_request(value)

    async def start(self, request):
        from backend.onprem.intents.repository import IntentConflict, IntentNotFound
        try:
            payload = await self._body(request)
            if isinstance(payload, dict) and 'intent' in payload:
                from backend.onprem.intents.api import IntentController
                from backend.onprem.scenarios.intent_bridge import CityIntentBridge
                authority = IntentController(self.context)
                denied = authority.authorize_write(request)
                if denied:
                    return denied
                result = await CityIntentBridge(self.context,self.service).submit(payload,
                    authority.owner(request),request.headers.get('idempotency-key',''))
            else:
                result = await self.service.start(payload)
        except IntentNotFound:
            return json_error(404, 'INTENT_NOT_FOUND', 'Intent or operation not found')
        except IntentConflict as exc:
            return json_error(409, 'INTENT_CONFLICT', str(exc))
        except ScenarioRequestError as exc:
            return json_error(400, exc.code, str(exc))
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            return json_error(400, "INVALID_SCENARIO_ALLOCATION", str(exc))
        return JSONResponse(result, status_code=202)

    async def preview(self, request):
        try:
            return JSONResponse(await self.service.preview(await self._body(request)))
        except ScenarioRequestError as exc:
            return json_error(400, exc.code, str(exc))
        except (ValueError, TypeError, UnicodeDecodeError):
            return json_error(400, "INVALID_SERVICE_REQUIREMENTS", "Invalid service preview request")

    async def service_capabilities(self, request):
        return JSONResponse(await self.service.service_capabilities())

    async def sla(self, request):
        return JSONResponse(await self.service.sla(request.path_params["round_id"]), headers={"Cache-Control": "no-store"})

    async def cancel(self, request):
        # Consume the body before changing network state, including chunked bodies.
        async for _chunk in request.stream():
            pass
        return JSONResponse(await self.service.cancel(request.path_params["round_id"]), status_code=202)


class UeController:
    def __init__(self, context):
        self.context = context
        self.service = ScenarioService(context)

    async def status(self, _request):
        return JSONResponse(self.context.ue_controller.status())

    async def desired(self, request):
        try:
            result = await self.service.set_ues(await request.json())
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            return json_error(400, "INVALID_UE_COUNTS", str(exc))
        return JSONResponse(result, status_code=202)

    async def bootstrap_status(self, _request):
        from backend.onprem.runtime.execution_admission import execution_admission
        status = await self.context.city_bootstrap.status()
        status['executionAdmission'] = await execution_admission(self.context, status)
        return JSONResponse(status, headers={'Cache-Control': 'no-store'})

    async def bootstrap_start(self, _request):
        async with self.context.application_admission_lock:
            result, status = await self.context.city_bootstrap.start()
        return JSONResponse(result, status_code=status)

class NetworkController:
    def __init__(self, context):
        self.context = context

    async def health(self, _request: Request) -> JSONResponse:
        runtime = await asyncio.to_thread(self.context.service.runtime_status)
        return JSONResponse({"ok": runtime.get("status") == "ready", **runtime})

    async def runtime_status(self, _request: Request) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(self.context.service.runtime_status))

    async def slices(self, _request: Request) -> JSONResponse:
        return JSONResponse((await self.context.service.snapshot(include_daily=False))["slices"])

    async def metrics(self, _request: Request) -> JSONResponse:
        return JSONResponse((await self.context.service.snapshot(include_daily=False))["metrics"])

    async def free5gc(self, request: Request) -> JSONResponse:
        values = request.query_params.getlist("includeDaily")
        if len(values) > 1 or (values and values[0] not in {"true", "false"}):
            return json_error(400, "INVALID_REQUEST", "includeDaily must be true or false")
        include_daily = not values or values[0] == "true"
        return JSONResponse((await self.context.service.snapshot(include_daily=include_daily))["free5gc"])

    async def websocket_endpoint(self, websocket: WebSocket) -> None:
        session_id = websocket.query_params.get("sessionId", "")[:256]
        await websocket.accept()
        self.context.service.websockets[websocket] = session_id
        try:
            await websocket.send_json({"type": "runtime_status", "payload": await asyncio.to_thread(self.context.service.runtime_status)})
            await websocket.send_json({"type": "ue_status", "payload": self.context.ue_controller.status()})
            while True:
                # API Gateway clients can send keepalive messages; accept and ignore them.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            self.context.service.websockets.pop(websocket, None)

class ObservationController:
    def __init__(self, context):
        self.context = context

    async def daily_native_observation(self, _request: Request) -> JSONResponse:
        observation = await self.context.service.daily_native_observation()
        return JSONResponse(self.context.service.public_daily_observation(observation))

    async def daily_trend(self, _request: Request) -> JSONResponse:
        try:
            return JSONResponse(await self.context.service.daily_trend())
        except LocalKubernetesError as exc:
            return json_error(503, "DAILY_NATIVE_OBSERVATION_UNAVAILABLE", str(exc))
        except OpenAIError as exc:
            return json_error(502, "AI_GENERATION_FAILED", str(exc))

