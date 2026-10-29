"""ASGI application factory and explicit route registration."""
import os
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route, WebSocketRoute

from backend.onprem.api.controllers import (
    NetworkController, ObservationController, ScenarioController, UeController,
)
from backend.onprem.api.http import application_error_handler
from backend.onprem.application.context import ApplicationContext
from backend.onprem.application.errors import ApplicationError
from backend.onprem.settings import ApiSettings
from backend.onprem.api.topology import TopologyController
from backend.onprem.application.topology import topology_lifespan


def create_app(*, settings=None, context=None):
    """Build an isolated app; injected context replaces external adapters in API tests.

    Native controllers start only in the ASGI lifespan. Importing this factory
    alone never creates databases or acquires a process fence.
    """
    if settings is not None and context is not None and settings != context.settings:
        raise ValueError("settings must match the injected application's context")
    context = context if context is not None else ApplicationContext(settings or ApiSettings.from_environment())
    network = NetworkController(context)
    observations = ObservationController(context)
    scenarios = ScenarioController(context)
    ues = UeController(context)
    topology = TopologyController(context)
    from backend.onprem.api.city_infra import CityInfraController
    city_infra = CityInfraController(context)
    from backend.onprem.api.city_observability import CityObservabilityController
    city_observability = CityObservabilityController(context)
    from backend.onprem.intents.api import IntentController
    from backend.onprem.intents.service import intent_lifespan
    if not hasattr(context, 'intent_service'):
        from backend.onprem.intents.integration import configured_intent_service
        context.intent_service = configured_intent_service(context)
    intents = IntentController(context)
    from backend.onprem.intents.placement_api import PlacementController
    if not hasattr(context, 'placement_service'):
        from backend.onprem.intents.placement_runtime import configured_placement_service
        context.placement_service = configured_placement_service(context)
    placements = PlacementController(context)
    app = Starlette(debug=False, lifespan=intent_lifespan(context,topology_lifespan(context)), routes=[
        Route("/api/onprem/topology", topology.status, methods=["GET"]),
        Route("/api/onprem/city-infra", city_infra.status, methods=["GET"]),
        Route("/api/onprem/city-observability", city_observability.status, methods=["GET"]),
        Route("/api/onprem/bootstrap", ues.bootstrap_status, methods=["GET"]),
        Route("/api/onprem/bootstrap/start", ues.bootstrap_start, methods=["POST"]),
        Route("/api/onprem/scenario-rounds/current", scenarios.current, methods=["GET"]),
        Route("/api/onprem/scenario-rounds/queue", scenarios.queue, methods=["GET"]),
        Route("/api/onprem/scenario-rounds/queue/{queue_id:str}/cancel", scenarios.cancel_queued, methods=["POST"]),
        Route("/api/onprem/service-profiles", scenarios.service_capabilities, methods=["GET"]),
        Route("/api/onprem/scenario-rounds/preview", scenarios.preview, methods=["POST"]),
        Route("/api/onprem/scenario-rounds/{round_id:str}/sla", scenarios.sla, methods=["GET"]),
        Route("/api/onprem/scenario-rounds/{round_id:str}/resources", scenarios.resources, methods=["GET"]),
        Route("/api/onprem/scenario-rounds/{round_id:str}/observability", scenarios.observability, methods=["GET"]),
        Route("/api/onprem/scenario-rounds/{round_id:str}/observation-view", scenarios.observation_view, methods=["GET"]),
        Route("/api/onprem/scenario-rounds/{round_id:str}/action-evidence", scenarios.action_evidence, methods=["GET"]),
        Route("/api/onprem/scenario-rounds/{round_id:str}/action-logs", scenarios.action_logs, methods=["GET"]),
        Route("/api/onprem/scenario-rounds/{round_id:str}/events", scenarios.observation_events, methods=["GET"]),
        Route("/api/onprem/scenario-rounds/{round_id:str}/measurements", scenarios.observation_measurements, methods=["GET"]),
        Route("/api/onprem/scenario-rounds/{round_id:str}/measurement-timings", scenarios.measurement_timings, methods=["GET"]),
        Route("/api/onprem/scenario-rounds", scenarios.start, methods=["POST"]),
        Route("/api/onprem/scenario-rounds/{round_id:str}/cancel", scenarios.cancel, methods=["POST"]),
        Route("/api/onprem/ue", ues.status, methods=["GET"]),
        Route("/api/onprem/ue/status", ues.status, methods=["GET"]),
        Route("/api/onprem/ue", ues.desired, methods=["POST"]),
        Route("/health", network.health, methods=["GET"]),
        Route("/api/runtime/status", network.runtime_status, methods=["GET"]),
        Route("/network/slices", network.slices, methods=["GET"]),
        Route("/metrics/current", network.metrics, methods=["GET"]),
        Route("/free5gc/status", network.free5gc, methods=["GET"]),
        Route("/api/onprem/daily-observation", observations.daily_native_observation, methods=["GET"]),
        Route("/api/onprem/daily-trend", observations.daily_trend, methods=["POST"]),
        WebSocketRoute("/ws", network.websocket_endpoint),
    ] + intents.routes() + placements.routes(), exception_handlers={ApplicationError: application_error_handler})
    app.state.context = context
    app.add_middleware(CORSMiddleware,
        allow_origins=list(context.settings.cors_origins), allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-CityVerse-Session-Id", "X-Session-Id", "X-CityVerse-Request-Id", "Idempotency-Key", "If-Match"],
        expose_headers=['X-CityVerse-Request-Id', 'Server-Timing'],
    )
    timing_path = os.environ.get('CITYVERSE_HTTP_TIMING_FILE')
    if timing_path:
        from backend.onprem.api.timing import RequestTimingMiddleware
        app.add_middleware(RequestTimingMiddleware, path=timing_path)
    return app
