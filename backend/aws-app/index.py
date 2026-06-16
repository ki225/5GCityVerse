from __future__ import annotations
from typing import Any
from app import CityVerseBackendApp


_APP: CityVerseBackendApp | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _APP
    if _APP is None:
        _APP = CityVerseBackendApp()
    return _APP.handle(event, context)
