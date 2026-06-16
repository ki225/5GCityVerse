from __future__ import annotations

import json
import time
from typing import Any

from constants import DynamoKeys, TTL_SECONDS
from time_utils import TimeUtils


class WebSocketConnectionService:
    def __init__(self, table: Any, apigw: Any) -> None:
        self.table = table
        self.apigw = apigw

    def connect(self, connection_id: str) -> dict[str, int]:
        self.table.put_item(
            Item={
                "pk": DynamoKeys.WS_CONNECTION.value,
                "sk": connection_id,
                "ttl": int(time.time()) + TTL_SECONDS,
                "connected_at": TimeUtils.now(),
            }
        )
        return {"statusCode": 200}

    def disconnect(self, connection_id: str) -> dict[str, int]:
        self.table.delete_item(Key={"pk": DynamoKeys.WS_CONNECTION.value, "sk": connection_id})
        return {"statusCode": 200}

    def handle_default(self, connection_id: str, event: dict[str, Any]) -> dict[str, int]:
        try:
            body = json.loads(event.get("body") or "{}")
            if body.get("action") == "ping":
                self.post(connection_id, {"type": "pong", "payload": {}})
        except Exception as exc:
            print(f"WebSocket message ignored: {exc}")
        return {"statusCode": 200}

    def broadcast(self, message: dict[str, Any]) -> None:
        response = self.table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": DynamoKeys.WS_CONNECTION.value},
        )
        for conn in response.get("Items", []):
            connection_id = conn["sk"]
            try:
                self.post(connection_id, message)
            except self.apigw.exceptions.GoneException:
                self.table.delete_item(Key={"pk": DynamoKeys.WS_CONNECTION.value, "sk": connection_id})
            except Exception as exc:
                print(f"Failed to broadcast to {connection_id}: {exc}")

    def post(self, connection_id: str, message: dict[str, Any]) -> None:
        self.apigw.post_to_connection(ConnectionId=connection_id, Data=json.dumps(message).encode("utf-8"))

