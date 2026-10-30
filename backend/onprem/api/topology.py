"""Read-only current/historical topology endpoint; reads never query the cluster."""
import asyncio
from datetime import datetime
import math

from starlette.responses import JSONResponse
from backend.onprem.application.topology import TopologyMonitor


class TopologyController:
    def __init__(self, context):
        self.context = context
        if getattr(context, 'topology_monitor', None) is None:
            context.topology_monitor = TopologyMonitor.from_context(context)

    async def status(self, request):
        query = request.query_params
        if set(query) - {'at','roundId','generation'}:
            return JSONResponse({'error':'INVALID_TOPOLOGY_QUERY'}, status_code=400)
        args = {}
        if query:
            try:
                if set(query) != {'at','roundId','generation'}:
                    raise ValueError()
                moment = datetime.fromisoformat(query['at'].replace('Z','+00:00'))
                if moment.tzinfo is None or not math.isfinite(moment.timestamp()):
                    raise ValueError()
                generation = int(query['generation'])
                if generation < 1 or str(generation) != query['generation']:
                    raise ValueError()
                scope = await asyncio.to_thread(self.context.round_ledger.observation_event_scope,
                                               query['roundId'])
                if scope['generation'] != generation:
                    raise ValueError()
                args = dict(at=moment.timestamp(), round_id=query['roundId'], generation=generation)
            except (ValueError, KeyError, OverflowError):
                return JSONResponse({'error':'INVALID_TOPOLOGY_QUERY'}, status_code=400)
        data = await asyncio.to_thread(self.context.topology_monitor.snapshot, **args)
        return JSONResponse(data, headers={'Cache-Control':'no-store'})
