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
        self._connections_cache: list[str] | None = None

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

    def _get_connections(self) -> list[str]:
        if self._connections_cache is not None:
            return self._connections_cache
        response = self.table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": DynamoKeys.WS_CONNECTION.value},
        )
        self._connections_cache = [conn["sk"] for conn in response.get("Items", [])]
        return self._connections_cache

    def invalidate_connections_cache(self) -> None:
        self._connections_cache = None

    def broadcast(self, message: dict[str, Any]) -> None:
        for connection_id in list(self._get_connections()):
            try:
                self.post(connection_id, message)
            except self.apigw.exceptions.GoneException:
                self.table.delete_item(Key={"pk": DynamoKeys.WS_CONNECTION.value, "sk": connection_id})
                if self._connections_cache is not None and connection_id in self._connections_cache:
                    self._connections_cache.remove(connection_id)
            except Exception as exc:
                print(f"Failed to broadcast to {connection_id}: {exc}")

    def post(self, connection_id: str, message: dict[str, Any]) -> None:
        self.apigw.post_to_connection(ConnectionId=connection_id, Data=json.dumps(message).encode("utf-8"))

