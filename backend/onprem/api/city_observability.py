"""Public, sanitized, read-only city monitoring endpoint."""
import asyncio
from starlette.responses import JSONResponse
from backend.onprem.application.city_observability import CityObservabilityReader


class CityObservabilityController:
    def __init__(self, context):
        self.context, self.reader = context, None

    async def status(self, request):
        headers = {'Cache-Control': 'no-store'}
        if request.query_params:
            return JSONResponse({'error': 'INVALID_CITY_OBSERVABILITY_QUERY'}, status_code=400, headers=headers)
        try:
            if self.reader is None:
                self.reader = CityObservabilityReader(self.context)
            return JSONResponse(await asyncio.to_thread(self.reader.snapshot), headers=headers)
        except Exception:
            return JSONResponse({'error': 'CITY_OBSERVABILITY_UNAVAILABLE'}, status_code=503, headers=headers)
