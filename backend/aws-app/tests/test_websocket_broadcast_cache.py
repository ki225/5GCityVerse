from __future__ import annotations

from typing import Any

from websocket_service import WebSocketConnectionService


class _FakeTable:
    def __init__(self, connection_ids: list[str]) -> None:
        self._connection_ids = connection_ids
        self.query_call_count = 0
        self.deleted: list[str] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.query_call_count += 1
        return {"Items": [{"sk": cid} for cid in self._connection_ids]}

    def delete_item(self, Key: dict[str, Any]) -> None:
        self.deleted.append(Key["sk"])


class _ApigwExceptions:
    GoneException = type("GoneException", (Exception,), {})


class _FakeApigw:
    def __init__(self, gone_connection_ids: set[str] | None = None) -> None:
        self.exceptions = _ApigwExceptions()
        self._gone_connection_ids = gone_connection_ids or set()
        self.posts: list[str] = []

    def post_to_connection(self, ConnectionId: str, Data: bytes) -> None:
        self.posts.append(ConnectionId)
        if ConnectionId in self._gone_connection_ids:
            raise self.exceptions.GoneException("gone")


def test_broadcast_queries_dynamo_once_per_service_instance() -> None:
    table = _FakeTable(["conn-1", "conn-2"])
    apigw = _FakeApigw()
    service = WebSocketConnectionService(table, apigw)

    service.broadcast({"type": "test", "payload": {}})
    service.broadcast({"type": "test", "payload": {}})

    assert table.query_call_count == 1
    assert apigw.posts == ["conn-1", "conn-2", "conn-1", "conn-2"]


def test_broadcast_evicts_gone_connection_immediately() -> None:
    table = _FakeTable(["conn-1", "conn-2"])
    apigw = _FakeApigw(gone_connection_ids={"conn-1"})
    service = WebSocketConnectionService(table, apigw)

    service.broadcast({"type": "test", "payload": {}})

    assert table.deleted == ["conn-1"]

    apigw.posts.clear()
    service.broadcast({"type": "test", "payload": {}})

    assert apigw.posts == ["conn-2"]
    assert table.query_call_count == 1
