"""
Event Engine Lambda
───────────────────
POST /api/events/trigger   { event_type: str }
POST /api/events/reset
GET  /api/events/status/{execution_id}

Responsibilities:
1. Validate the city event
2. Publish EventBridge event → triggers Bedrock Orchestrator
3. Start UERANSIM K8s Job (via k8s client or EKS API)
4. Start Step Functions execution for multi-step slice reconfiguration
5. Return execution ID
"""

import json
import os
import uuid
import boto3
from datetime import datetime, timezone

# ─── Clients ──────────────────────────────────────────────────────────────────
events_client   = boto3.client('events')
sfn_client      = boto3.client('stepfunctions')
dynamodb        = boto3.resource('dynamodb')

TABLE_NAME     = os.environ['DYNAMODB_TABLE']
SFN_ARN        = os.environ['SFN_STATE_MACHINE_ARN']
EVENT_BUS_NAME = os.environ.get('EVENT_BUS_NAME', 'default')

# ─── Event → Slice mapping ────────────────────────────────────────────────────
EVENT_CONFIG = {
    'concert':   { 'slice_sst': 1, 'slice_type': 'eMBB',  'ue_count': 50, 'iperf_gbps': 5 },
    'typhoon':   { 'slice_sst': 3, 'slice_type': 'mMTC',  'ue_count': 200,'iperf_gbps': 1 },
    'accident':  { 'slice_sst': 4, 'slice_type': 'V2X',   'ue_count': 20, 'iperf_gbps': 0.5 },
    'medical':   { 'slice_sst': 2, 'slice_type': 'URLLC', 'ue_count': 10, 'iperf_gbps': 0.2 },
    'iot_surge': { 'slice_sst': 3, 'slice_type': 'mMTC',  'ue_count': 500,'iperf_gbps': 0.5 },
}


def lambda_handler(event: dict, context) -> dict:
    path   = event.get('path', '')
    method = event.get('httpMethod', 'GET')

    if method == 'POST' and path.endswith('/trigger'):
        return handle_trigger(event)
    if method == 'POST' and path.endswith('/reset'):
        return handle_reset()
    if method == 'GET' and '/status/' in path:
        execution_id = path.split('/status/')[-1]
        return handle_status(execution_id)

    return _resp(404, {'error': 'Not found'})


# ─── /trigger ─────────────────────────────────────────────────────────────────
def handle_trigger(apigw_event: dict) -> dict:
    body = json.loads(apigw_event.get('body') or '{}')
    event_type = body.get('event_type', '')

    if event_type not in EVENT_CONFIG:
        return _resp(400, {'error': f'Unknown event_type: {event_type}'})

    execution_id = str(uuid.uuid4())
    cfg = EVENT_CONFIG[event_type]
    ts  = datetime.now(timezone.utc).isoformat()

    # 1. Persist to DynamoDB
    table = dynamodb.Table(TABLE_NAME)
    table.put_item(Item={
        'pk':           f'EVENT#{execution_id}',
        'sk':           'STATUS',
        'event_type':   event_type,
        'status':       'STARTED',
        'started_at':   ts,
        'config':       cfg,
    })

    # 2. Publish to EventBridge (triggers Bedrock Orchestrator)
    events_client.put_events(Entries=[{
        'Source':       '5gcityverse.events',
        'DetailType':   'CityEventTriggered',
        'Detail':       json.dumps({
            'execution_id': execution_id,
            'event_type':   event_type,
            'config':       cfg,
            'timestamp':    ts,
        }),
        'EventBusName': EVENT_BUS_NAME,
    }])

    # 3. Start Step Functions execution (slice reconfiguration workflow)
    sfn_client.start_execution(
        stateMachineArn=SFN_ARN,
        name=f'{event_type}-{execution_id[:8]}',
        input=json.dumps({
            'execution_id': execution_id,
            'event_type':   event_type,
            'config':       cfg,
        }),
    )

    return _resp(200, {'executionId': execution_id, 'eventType': event_type})


# ─── /reset ───────────────────────────────────────────────────────────────────
def handle_reset() -> dict:
    # Publish reset event — State Bridge will push to frontend
    events_client.put_events(Entries=[{
        'Source':       '5gcityverse.events',
        'DetailType':   'SimulationReset',
        'Detail':       json.dumps({'timestamp': datetime.now(timezone.utc).isoformat()}),
        'EventBusName': EVENT_BUS_NAME,
    }])
    return _resp(200, {'status': 'reset'})


# ─── /status/{id} ─────────────────────────────────────────────────────────────
def handle_status(execution_id: str) -> dict:
    table = dynamodb.Table(TABLE_NAME)
    res = table.get_item(Key={'pk': f'EVENT#{execution_id}', 'sk': 'STATUS'})
    item = res.get('Item')
    if not item:
        return _resp(404, {'error': 'Execution not found'})
    return _resp(200, item)


# ─── Helper ───────────────────────────────────────────────────────────────────
def _resp(status: int, body: dict) -> dict:
    return {
        'statusCode': status,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
        },
        'body': json.dumps(body),
    }
