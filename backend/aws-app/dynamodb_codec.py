import json
from decimal import Decimal
from typing import Any


class DynamoDbCodec:
    @staticmethod
    def to_dynamodb(value: Any) -> Any:
        return json.loads(json.dumps(value), parse_float=Decimal)

    @staticmethod
    def from_dynamodb(value: Any) -> Any:
        if isinstance(value, list):
            return [DynamoDbCodec.from_dynamodb(v) for v in value]
        if isinstance(value, dict):
            return {k: DynamoDbCodec.from_dynamodb(v) for k, v in value.items()}
        if isinstance(value, Decimal):
            return int(value) if value % 1 == 0 else float(value)
        return value

