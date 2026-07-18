from __future__ import annotations

from typing import Any

from event_repository import NEF_HITS_LIMIT, NEF_HITS_WINDOW_SECONDS, EventRepository
from time_utils import TimeUtils


class _FakeTable:
    """Minimal in-memory stand-in for the boto3 DynamoDB Table resource, keyed
    like the real table by (pk, sk)."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    def put_item(self, Item: dict[str, Any]) -> None:
        self._items[(Item["pk"], Item["sk"])] = Item

    def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:
        item = self._items.get((Key["pk"], Key["sk"]))
        return {"Item": item} if item is not None else {}


def test_record_nef_tool_hit_persists_and_is_readable() -> None:
    """A hit recorded via record_nef_tool_hit (the write path called from
    ToolGateway) must be readable back via recent_nef_tool_hits (the read path
    used by control_plane_runtime_metrics) -- this is what lets the hit cross
    from the async event-execution Lambda container to the API container."""
    repo = EventRepository(_FakeTable())

    repo.record_nef_tool_hit({"protocol": "Nnef PFD management", "executionId": "exec-1", "at": TimeUtils.epoch_millis()})

    hits = repo.recent_nef_tool_hits()
    assert len(hits) == 1
    assert hits[0]["protocol"] == "Nnef PFD management"
    assert hits[0]["executionId"] == "exec-1"


def test_recent_nef_tool_hits_returns_empty_list_when_nothing_recorded() -> None:
    repo = EventRepository(_FakeTable())

    assert repo.recent_nef_tool_hits() == []


def test_record_nef_tool_hit_trims_to_limit() -> None:
    """Only the most recent NEF_HITS_LIMIT hits are kept, matching ToolGateway's
    prior in-memory NEF_TOOL_HITS_LIMIT bound."""
    repo = EventRepository(_FakeTable())
    now = TimeUtils.epoch_millis()

    for index in range(NEF_HITS_LIMIT + 5):
        repo.record_nef_tool_hit({"protocol": "Nnef PFD management", "executionId": f"exec-{index}", "at": now})

    hits = repo.recent_nef_tool_hits()
    assert len(hits) == NEF_HITS_LIMIT
    # The oldest entries were dropped; the most recent ones survive.
    assert hits[-1]["executionId"] == f"exec-{NEF_HITS_LIMIT + 4}"
    assert hits[0]["executionId"] == "exec-5"


def test_recent_nef_tool_hits_filters_out_entries_older_than_window() -> None:
    """A hit older than the 5-minute window must not be surfaced, even though it
    is still physically stored until the next write trims it."""
    repo = EventRepository(_FakeTable())
    now = TimeUtils.epoch_millis()
    stale_at = now - (NEF_HITS_WINDOW_SECONDS + 30) * 1000
    fresh_at = now - 10_000

    repo.table.put_item(
        Item={
            "pk": "CONTROL",
            "sk": "NEF_HITS",
            "hits": [
                {"protocol": "Nnef PFD management", "executionId": "stale", "at": stale_at},
                {"protocol": "Nnef PFD management", "executionId": "fresh", "at": fresh_at},
            ],
        }
    )

    hits = repo.recent_nef_tool_hits()

    assert [hit["executionId"] for hit in hits] == ["fresh"]


def test_record_nef_tool_hit_drops_stale_entries_before_persisting() -> None:
    """Window filtering also applies on write, so the stored list doesn't grow
    unbounded with entries nothing will ever read again."""
    repo = EventRepository(_FakeTable())
    now = TimeUtils.epoch_millis()
    stale_at = now - (NEF_HITS_WINDOW_SECONDS + 60) * 1000
    repo.table.put_item(
        Item={
            "pk": "CONTROL",
            "sk": "NEF_HITS",
            "hits": [{"protocol": "Nnef PFD management", "executionId": "stale", "at": stale_at}],
        }
    )

    repo.record_nef_tool_hit({"protocol": "Nnef PFD management", "executionId": "new", "at": now})

    stored = repo.table.get_item(Key={"pk": "CONTROL", "sk": "NEF_HITS"})["Item"]
    assert [hit["executionId"] for hit in stored["hits"]] == ["new"]
