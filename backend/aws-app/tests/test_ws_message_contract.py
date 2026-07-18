from __future__ import annotations

import re
from pathlib import Path

from constants import WsMessageType

APP_PY = Path(__file__).resolve().parents[1] / "app.py"


def _broadcast_type_usages() -> set[str]:
    """Every `"type": <expr>` used in a `self.broadcast({...})` call site in app.py."""
    text = APP_PY.read_text(encoding="utf-8")
    return set(re.findall(r'"type":\s*([^,\n}]+)', text))


def test_app_py_broadcasts_only_use_enum_values() -> None:
    """Every broadcast type in app.py must reference WsMessageType, never a raw string
    literal, so the enum stays the single source of truth for the WS contract."""
    usages = _broadcast_type_usages()
    assert usages, "expected at least one broadcast type usage in app.py"
    for usage in usages:
        assert usage.startswith("WsMessageType."), f"raw literal type found: {usage!r}"


def test_enum_covers_every_type_referenced_in_app_py() -> None:
    """Every WsMessageType member referenced in app.py must exist on the enum (i.e.
    catches typos like WsMessageType.EVENT_RESSET)."""
    usages = _broadcast_type_usages()
    referenced_members = {usage.split(".")[1].split(".value")[0] for usage in usages}
    enum_members = {member.name for member in WsMessageType}
    assert referenced_members <= enum_members


def test_enum_has_no_unused_members() -> None:
    """Every WsMessageType member should be referenced by at least one broadcast in
    app.py, so the enum doesn't silently drift ahead of the actual live contract."""
    usages = _broadcast_type_usages()
    referenced_members = {usage.split(".")[1].split(".value")[0] for usage in usages}
    enum_members = {member.name for member in WsMessageType}
    assert enum_members <= referenced_members
