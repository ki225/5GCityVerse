"""
WebSocket Handler Lambda
────────────────────────
Handles $connect, $disconnect, and $default routes for
API Gateway WebSocket API.

Persists connection IDs in DynamoDB so the State Bridge
can broadcast to all connected clients.
"""

import json
import os
import boto3
from datetime import datetime, timezone

dynamodb    = boto3.resource('dynamodb')
TABLE_NAME  = os.environ['DYNAMODB_TABLE']

TTL_SECONDS = 7200  # 2h


def lambda_handler(event: dict, _context) -> dict:
    route  = event.get('requestContext', {}).get('routeKey', '$default')
    conn_id = event.get('requestContext', {}).get('connectionId', '')

    if route == '$connect':
        return _on_connect(conn_id)
    if route == '$disconnect':
        return _on_disconnect(conn_id)
    return _on_message(conn_id, event)


def _on_connect(conn_id: str) -> dict:
    table = dynamodb.Table(TABLE_NAME)
    ttl   = int(datetime.now(timezone.utc).timestamp()) + TTL_SECONDS
    table.put_item(Item={
        'pk':         'WS_CONNECTION',
        'sk':         conn_id,
        'connected_at': datetime.now(timezone.utc).isoformat(),
        'ttl':        ttl,
    })
    return {'statusCode': 200}


def _on_disconnect(conn_id: str) -> dict:
    table = dynamodb.Table(TABLE_NAME)
    table.delete_item(Key={'pk': 'WS_CONNECTION', 'sk': conn_id})
    return {'statusCode': 200}


def _on_message(conn_id: str, event: dict) -> dict:
    """Echo-back or handle ping."""
    try:
        body = json.loads(event.get('body') or '{}')
        if body.get('action') == 'ping':
            apigw = boto3.client(
                'apigatewaymanagementapi',
                endpoint_url=os.environ.get('APIGW_WS_ENDPOINT', ''),
            )
            apigw.post_to_connection(
                ConnectionId=conn_id,
                Data=json.dumps({'type': 'pong'}).encode('utf-8'),
            )
    except Exception:
        pass
    return {'statusCode': 200}
