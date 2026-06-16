from __future__ import annotations

from typing import Any

from constants import DynamoKeys
from dynamodb_codec import DynamoDbCodec


class EventRepository:
    def __init__(self, table: Any) -> None:
        self.table = table

    def put_status(self, item: dict[str, Any]) -> None:
        self.table.put_item(Item=DynamoDbCodec.to_dynamodb(item))

    def get_status(self, execution_id: str) -> dict[str, Any] | None:
        result = self.table.get_item(Key={"pk": f"EVENT#{execution_id}", "sk": DynamoKeys.STATUS.value})
        item = result.get("Item")
        if not item:
            return None
        item.pop("pk", None)
        item.pop("sk", None)
        return DynamoDbCodec.from_dynamodb(item)

