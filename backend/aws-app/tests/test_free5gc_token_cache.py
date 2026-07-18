from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from free5gc_utils import Free5gcClient
from metrics_service import PrometheusMetricsService


class FakeHttpClient:
    """Fake HTTP client for testing Free5gcClient token caching without network."""

    def __init__(self) -> None:
        self.login_call_count = 0
        self.all_calls: list[tuple[str, str, Any]] = []
        self.registered_ues_call_count = 0

    def request(
        self,
        method: str,
        url: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        """Fake request that tracks calls and returns canned responses."""
        self.all_calls.append((method, url, body))

        # Track login calls
        if "/api/login" in url:
            self.login_call_count += 1
            if method == "POST":
                return 200, {"access_token": f"token-{self.login_call_count}"}

        # Track registered_ues calls
        if "/api/registered-ue-context" in url:
            self.registered_ues_call_count += 1
            # Simulate 401 on first call if requested via side effect
            if hasattr(self, "simulate_401_on_ues") and self.simulate_401_on_ues and self.registered_ues_call_count == 1:
                return 401, {"error": "Unauthorized"}
            return 200, [{"ueId": "ue-1", "plmnID": "00101"}]

        return 200, {}


class FakeMetrics:
    """Minimal stub metrics for testing."""

    def unavailable_metrics(self) -> dict[str, Any]:
        return {}


@pytest.fixture
def fake_http_client() -> FakeHttpClient:
    return FakeHttpClient()


@pytest.fixture
def fake_metrics() -> FakeMetrics:
    return FakeMetrics()


@pytest.fixture
def free5gc_client(fake_http_client: FakeHttpClient, fake_metrics: FakeMetrics) -> Free5gcClient:
    """Create Free5gcClient with fake HTTP client for testing."""
    return Free5gcClient(
        webui_url="http://localhost:5000",
        username="admin",
        password="free5gc",
        plmn_id="00101",
        metrics=fake_metrics,
        http=fake_http_client,
    )


def test_consecutive_registered_ues_calls_cache_token_after_first_login(
    free5gc_client: Free5gcClient,
    fake_http_client: FakeHttpClient,
) -> None:
    """Test that calling registered_ues() twice only invokes login once (token is cached)."""
    # First call
    result1 = free5gc_client.registered_ues()
    assert result1 == [{"ueId": "ue-1", "plmnID": "00101"}]
    assert fake_http_client.login_call_count == 1

    # Second call should use cached token, no new login
    result2 = free5gc_client.registered_ues()
    assert result2 == [{"ueId": "ue-1", "plmnID": "00101"}]
    assert fake_http_client.login_call_count == 1  # Still 1, not 2


def test_registered_ues_reauths_and_succeeds_after_401(
    free5gc_client: Free5gcClient,
    fake_http_client: FakeHttpClient,
) -> None:
    """registered_ues() itself (not just the isolated helper) must recover from a 401
    by invalidating the cached token, logging in again, and returning data."""
    fake_http_client.simulate_401_on_ues = True

    result = free5gc_client.registered_ues()

    # Login happened twice: initial token, then reauth after the 401.
    assert fake_http_client.login_call_count == 2
    assert result == [{"ueId": "ue-1", "plmnID": "00101"}]


def test_request_with_reauth_handles_401_and_retries(
    free5gc_client: Free5gcClient,
    fake_http_client: FakeHttpClient,
) -> None:
    """Test that request_with_reauth() handles 401 by invalidating token and retrying."""
    fake_http_client.simulate_401_on_ues = True

    # First call to login
    status, data = free5gc_client.request_with_reauth("GET", "/api/registered-ue-context")

    # Should have called login twice (first attempt got 401, second succeeded after reauth)
    assert fake_http_client.login_call_count == 2
    # Second attempt (after 401) should return 200 with data
    assert status == 200
    assert data == [{"ueId": "ue-1", "plmnID": "00101"}]


def test_token_invalidation_after_401_forces_new_login(
    free5gc_client: Free5gcClient,
    fake_http_client: FakeHttpClient,
) -> None:
    """Test that _invalidate_token() clears cache and forces new login."""
    # First login
    token1 = free5gc_client.login()
    assert fake_http_client.login_call_count == 1
    assert token1 == "token-1"

    # Use cached token
    token2 = free5gc_client.login()
    assert fake_http_client.login_call_count == 1  # Still 1
    assert token2 == "token-1"

    # Invalidate token
    free5gc_client._invalidate_token()

    # Next login should fetch new token
    token3 = free5gc_client.login()
    assert fake_http_client.login_call_count == 2
    assert token3 == "token-2"


def test_multiple_methods_using_cached_token(
    free5gc_client: Free5gcClient,
    fake_http_client: FakeHttpClient,
) -> None:
    """Test that list_subscribers, profiles, etc. all benefit from token cache."""
    # Configure fake response for subscriber endpoint
    def mock_request(method, url, body=None, headers=None, timeout=None):
        if "/api/subscriber" in url:
            return 200, [{"ueId": "ue-1"}]
        elif "/api/profile" in url:
            return 200, [{"profileName": "test"}]
        elif "/api/login" in url:
            fake_http_client.login_call_count += 1
            return 200, {"access_token": f"token-{fake_http_client.login_call_count}"}
        return 200, {}

    fake_http_client.request = mock_request

    # Call multiple methods
    subs = free5gc_client.list_subscribers()
    profs = free5gc_client.profiles()

    # Should only login once, both methods use cached token
    assert fake_http_client.login_call_count == 1
    assert len(subs) == 1
    assert len(profs) == 1
