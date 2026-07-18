"""
WebSocket Handler Lambda

Handles $connect, $disconnect, and $default routes for
API Gateway WebSocket API.

Persists connection IDs in DynamoDB so the State Bridge
can broadcast to all connected clients.
"""

import json
import os
import boto3
from datetime import datetime, timezone

try:
    from pydantic import BaseModel
except ImportError:  # pragma: no cover
    class BaseModel:  # type: ignore
        def __init__(self, **data) -> None:
            for key, value in data.items():
                setattr(self, key, value)


dynamodb    = boto3.resource('dynamodb')
TABLE_NAME  = os.environ['DYNAMODB_TABLE']

TTL_SECONDS = 7200  # 2h


class WebSocketMessage(BaseModel):
    action: str | None = None


class WebSocketConnectionStore:
    def __init__(self, table_name: str) -> None:
        self.table = dynamodb.Table(table_name)

    def connect(self, conn_id: str) -> None:
        ttl = int(datetime.now(timezone.utc).timestamp()) + TTL_SECONDS
        self.table.put_item(Item={
            'pk': 'WS_CONNECTION',
            'sk': conn_id,
            'connected_at': datetime.now(timezone.utc).isoformat(),
            'ttl': ttl,
        })

    def disconnect(self, conn_id: str) -> None:
        self.table.delete_item(Key={'pk': 'WS_CONNECTION', 'sk': conn_id})


class WebSocketHandler:
    def __init__(self) -> None:
        self.store = WebSocketConnectionStore(TABLE_NAME)

    def handle(self, event: dict) -> dict:
        route = event.get('requestContext', {}).get('routeKey', '$default')
        conn_id = event.get('requestContext', {}).get('connectionId', '')

        if route == '$connect':
            return self.on_connect(conn_id)
        if route == '$disconnect':
            return self.on_disconnect(conn_id)
        return self.on_message(conn_id, event)

    def on_connect(self, conn_id: str) -> dict:
        self.store.connect(conn_id)
        return {'statusCode': 200}

    def on_disconnect(self, conn_id: str) -> dict:
        self.store.disconnect(conn_id)
        return {'statusCode': 200}

    def on_message(self, conn_id: str, event: dict) -> dict:
        try:
            body = WebSocketMessage(**json.loads(event.get('body') or '{}'))
            if body.action == 'ping':
                apigw = boto3.client(
                    'apigatewaymanagementapi',
                    endpoint_url=os.environ.get('APIGW_WS_ENDPOINT', ''),
                )
                apigw.post_to_connection(
                    ConnectionId=conn_id,
                    Data=json.dumps({'type': 'pong'}).encode('utf-8'),
                )
        except Exception as exc:
            print(f'WebSocket message ignored: {exc}')
        return {'statusCode': 200}


_HANDLER = WebSocketHandler()


def lambda_handler(event: dict, _context) -> dict:
    return _HANDLER.handle(event)
