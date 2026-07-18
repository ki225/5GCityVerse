from __future__ import annotations

from typing import Any
import time
from botocore.exceptions import ClientError

from constants import DynamoKeys
from dynamodb_codec import DynamoDbCodec
from time_utils import TimeUtils

NEF_HITS_LIMIT = 20
NEF_HITS_WINDOW_SECONDS = 300


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

    def has_active_events(self, session_id: str | None = None) -> bool:
        active_statuses = {
            "AGENT_QUEUED", "AGENT_RUNNING",  # legacy records
            "SIMULATION_QUEUED", "SIMULATION_RUNNING",
        }
        scan_kwargs = {
            "ProjectionExpression": "#status, sessionId",
            "ExpressionAttributeNames": {"#status": "status"},
        }
        while True:
            result = self.table.scan(**scan_kwargs)
            for item in result.get("Items", []):
                decoded = DynamoDbCodec.from_dynamodb(item)
                if decoded.get("status") in active_statuses and (not session_id or decoded.get("sessionId") == session_id):
                    return True
            last_key = result.get("LastEvaluatedKey")
            if not last_key:
                return False
            scan_kwargs["ExclusiveStartKey"] = last_key

    def acquire_session_lease(self, session_id: str, ttl_seconds: int = 600) -> bool:
        now = int(time.time())
        try:
            self.table.update_item(
                Key={"pk": "CONTROL", "sk": "SIMULATION_LEASE"},
                UpdateExpression="SET #owner = :owner, #expires = :expires",
                ConditionExpression="attribute_not_exists(#owner) OR #owner = :owner OR #expires < :now",
                ExpressionAttributeNames={"#owner": "sessionId", "#expires": "expiresAt"},
                ExpressionAttributeValues={":owner": session_id, ":expires": now + ttl_seconds, ":now": now},
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def session_lease(self) -> dict[str, Any] | None:
        item = self.table.get_item(Key={"pk": "CONTROL", "sk": "SIMULATION_LEASE"}).get("Item")
        if not item:
            return None
        decoded = DynamoDbCodec.from_dynamodb(item)
        if int(decoded.get("expiresAt") or 0) < int(time.time()):
            return None
        return decoded

    def release_session_lease(self, session_id: str) -> bool:
        try:
            self.table.delete_item(
                Key={"pk": "CONTROL", "sk": "SIMULATION_LEASE"},
                ConditionExpression="#owner = :owner",
                ExpressionAttributeNames={"#owner": "sessionId"},
                ExpressionAttributeValues={":owner": session_id},
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def update_status(self, execution_id: str, updates: dict[str, Any]) -> None:
        if not updates:
            return
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        assignments: list[str] = []
        for index, (key, value) in enumerate(updates.items()):
            name_key = f"#n{index}"
            value_key = f":v{index}"
            names[name_key] = key
            values[value_key] = DynamoDbCodec.to_dynamodb(value)
            assignments.append(f"{name_key} = {value_key}")
        self.table.update_item(
            Key={"pk": f"EVENT#{execution_id}", "sk": DynamoKeys.STATUS.value},
            UpdateExpression="SET " + ", ".join(assignments),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def put_reset_marker(self, reset_epoch_millis: int, reset_at: str) -> None:
        self.table.put_item(
            Item=DynamoDbCodec.to_dynamodb(
                {
                    "pk": "CONTROL",
                    "sk": "RESET",
                    "resetEpochMillis": reset_epoch_millis,
                    "resetAt": reset_at,
                }
            )
        )

    def begin_reset_job(self, session_id: str, reset_id: str, queued_at: str) -> tuple[dict[str, Any], bool]:
        """Create one active reset per browser session.

        Lambda async delivery and HTTP retries are both at-least-once.  The
        conditional write makes duplicate POST requests converge on the same
        active job instead of starting overlapping Kubernetes cleanup runs.
        """
        key = {"pk": "CONTROL", "sk": f"RESET_JOB#{session_id}"}
        try:
            result = self.table.update_item(
                Key=key,
                UpdateExpression=(
                    "SET #reset_id = :reset_id, #session_id = :session_id, #status = :queued, "
                    "#stage = :stage, #progress = :progress, #message = :message, "
                    "#queued_at = :queued_at, #updated_at = :queued_at, #deadline = :deadline"
                ),
                ConditionExpression=(
                    "attribute_not_exists(#status) OR #status = :success OR #status = :failed"
                ),
                ExpressionAttributeNames={
                    "#reset_id": "resetId",
                    "#session_id": "sessionId",
                    "#status": "status",
                    "#stage": "progressStage",
                    "#progress": "progressPercent",
                    "#message": "message",
                    "#queued_at": "queuedAt",
                    "#updated_at": "updatedAt",
                    "#deadline": "deadlineEpochSeconds",
                },
                ExpressionAttributeValues={
                    ":reset_id": reset_id,
                    ":session_id": session_id,
                    ":queued": "queued",
                    ":success": "success",
                    ":failed": "failed",
                    ":stage": "queued",
                    ":progress": 0,
                    ":message": "Reset queued",
                    ":queued_at": queued_at,
                    ":deadline": int(time.time()) + 900,
                },
                ReturnValues="ALL_NEW",
            )
            return DynamoDbCodec.from_dynamodb(result.get("Attributes") or {}), True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            current = self.get_reset_job(session_id)
            if not current:
                raise RuntimeError("Reset idempotency record disappeared after a conditional conflict") from exc
            return current, False

    def get_reset_job(self, session_id: str, reset_id: str | None = None) -> dict[str, Any] | None:
        result = self.table.get_item(
            Key={"pk": "CONTROL", "sk": f"RESET_JOB#{session_id}"},
            ConsistentRead=True,
        )
        item = result.get("Item")
        if not item:
            return None
        decoded = DynamoDbCodec.from_dynamodb(item)
        decoded.pop("pk", None)
        decoded.pop("sk", None)
        if reset_id and decoded.get("resetId") != reset_id:
            return None
        return decoded

    def update_reset_job(self, session_id: str, reset_id: str, updates: dict[str, Any]) -> bool:
        """Update only the worker generation that owns ``reset_id``.

        A delayed retry from an older async invocation must never overwrite a
        newer reset's progress or terminal state.
        """
        if not updates:
            return True
        names: dict[str, str] = {"#reset_id": "resetId"}
        values: dict[str, Any] = {":reset_id": reset_id}
        assignments: list[str] = []
        for index, (key, value) in enumerate(updates.items()):
            name_key = f"#n{index}"
            value_key = f":v{index}"
            names[name_key] = key
            values[value_key] = DynamoDbCodec.to_dynamodb(value)
            assignments.append(f"{name_key} = {value_key}")
        try:
            self.table.update_item(
                Key={"pk": "CONTROL", "sk": f"RESET_JOB#{session_id}"},
                UpdateExpression="SET " + ", ".join(assignments),
                ConditionExpression="#reset_id = :reset_id",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def claim_reset_job(self, session_id: str, reset_id: str, started_at: str) -> bool:
        """Atomically let exactly one async delivery execute the cleanup."""
        try:
            self.table.update_item(
                Key={"pk": "CONTROL", "sk": f"RESET_JOB#{session_id}"},
                UpdateExpression=(
                    "SET #status = :running, #stage = :stage, #progress = :progress, "
                    "#message = :message, #started_at = :started_at, #updated_at = :started_at, "
                    "#deadline = :deadline"
                ),
                ConditionExpression="#reset_id = :reset_id AND #status = :queued",
                ExpressionAttributeNames={
                    "#reset_id": "resetId",
                    "#status": "status",
                    "#stage": "progressStage",
                    "#progress": "progressPercent",
                    "#message": "message",
                    "#started_at": "startedAt",
                    "#updated_at": "updatedAt",
                    "#deadline": "deadlineEpochSeconds",
                },
                ExpressionAttributeValues={
                    ":reset_id": reset_id,
                    ":queued": "queued",
                    ":running": "running",
                    ":stage": "runtime_cleanup",
                    ":progress": 10,
                    ":message": "Removing scenario traffic runtime",
                    ":started_at": started_at,
                    # Lambda has a 900s hard timeout. The extra ten seconds
                    # let the final DynamoDB write win before status polling
                    # converts a hard-killed worker into an observable failure.
                    ":deadline": int(time.time()) + 910,
                },
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def latest_reset_epoch_millis(self) -> int:
        result = self.table.get_item(Key={"pk": "CONTROL", "sk": "RESET"})
        item = result.get("Item")
        if not item:
            return 0
        decoded = DynamoDbCodec.from_dynamodb(item)
        try:
            return int(decoded.get("resetEpochMillis") or 0)
        except (TypeError, ValueError):
            return 0

    def record_nef_tool_hit(self, hit: dict[str, Any]) -> None:
        """Persist a successful NEF-backed tool call so it survives across the
        separate Lambda containers that run event execution (async) and serve
        /free5gc/status (API), which do not share in-process memory."""
        now_millis = TimeUtils.epoch_millis()
        hits = self._read_nef_hits(now_millis)
        hits.append(hit)
        if len(hits) > NEF_HITS_LIMIT:
            hits = hits[-NEF_HITS_LIMIT:]
        self.table.put_item(
            Item=DynamoDbCodec.to_dynamodb({"pk": "CONTROL", "sk": DynamoKeys.NEF_HITS.value, "hits": hits})
        )

    def recent_nef_tool_hits(self) -> list[dict[str, Any]]:
        return self._read_nef_hits(TimeUtils.epoch_millis())

    def _read_nef_hits(self, now_millis: int) -> list[dict[str, Any]]:
        result = self.table.get_item(Key={"pk": "CONTROL", "sk": DynamoKeys.NEF_HITS.value})
        item = result.get("Item")
        if not item:
            return []
        decoded = DynamoDbCodec.from_dynamodb(item)
        hits = decoded.get("hits")
        if not isinstance(hits, list):
            return []
        window_start = now_millis - NEF_HITS_WINDOW_SECONDS * 1000
        return [hit for hit in hits if isinstance(hit, dict) and int(hit.get("at") or 0) >= window_start]
