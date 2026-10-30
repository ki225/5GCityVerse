"""City map Pod resource observations; no mutations or implicit runtime start."""
import asyncio
from starlette.responses import JSONResponse
from backend.onprem.application.city_infra import CityInfraReader
from backend.onprem.evidence.topology import PodTopologyCollector


class CityInfraController:
    def __init__(self, context):
        self.context, self.reader = context, None

    async def status(self, request):
        if request.query_params:
            return JSONResponse({'error': 'INVALID_INFRA_QUERY'}, status_code=400)
        try:
            if self.reader is None:
                collector = PodTopologyCollector.from_context(self.context)
                if collector is None:
                    raise ValueError('Not configured')
                self.reader = CityInfraReader(collector.read_pods, self.context.ue_controller.driver.namespace)
            data = await asyncio.to_thread(self.reader.snapshot)
            return JSONResponse(data, headers={'Cache-Control': 'no-store'})
        except Exception:
            # Do not leak kubectl stderr, raw Pod manifests or credential paths.
            return JSONResponse({'error': 'INFRA_OBSERVATION_UNAVAILABLE'}, status_code=503,
                                headers={'Cache-Control': 'no-store'})
