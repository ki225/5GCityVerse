from __future__ import annotations

import base64
import os
from typing import Any
from unittest import mock

import pytest

import eks_kubernetes_client
from eks_kubernetes_client import EksKubernetesClient, get_eks_client


@pytest.fixture(autouse=True)
def reset_eks_client_cache() -> Any:
    """The factory cache is module-level state; clear it before/after each test
    so tests don't leak cached clients into one another. Also removes any
    /tmp/{cluster}-ca.crt files created by the tests below."""
    eks_kubernetes_client._eks_client_cache.clear()
    yield
    for cluster_name in list(eks_kubernetes_client._eks_client_cache.keys()):
        ca_path = f"/tmp/{cluster_name}-ca.crt"
        if os.path.exists(ca_path):
            os.remove(ca_path)
    eks_kubernetes_client._eks_client_cache.clear()


@pytest.fixture(autouse=True)
def clear_static_cluster_connection(monkeypatch) -> None:
    monkeypatch.delenv("EKS_CLUSTER_ENDPOINT", raising=False)
    monkeypatch.delenv("EKS_CLUSTER_CA_DATA", raising=False)
    monkeypatch.delenv("EKS_CLUSTER_NAME", raising=False)


def _fake_describe_cluster_response() -> dict[str, Any]:
    ca_data = base64.b64encode(b"fake-ca-cert-bytes").decode("utf-8")
    return {
        "cluster": {
            "endpoint": "https://example.eks.amazonaws.com",
            "certificateAuthority": {"data": ca_data},
        }
    }


@pytest.fixture
def patched_eks(monkeypatch) -> Any:
    """Patches boto3.client('eks').describe_cluster and short-circuits bearer
    token generation so tests never touch AWS credentials or the network."""
    describe_cluster_mock = mock.Mock(return_value=_fake_describe_cluster_response())

    fake_eks_client = mock.Mock()
    fake_eks_client.describe_cluster = describe_cluster_mock

    def fake_boto3_client(service_name: str, *args: Any, **kwargs: Any) -> Any:
        if service_name == "eks":
            return fake_eks_client
        raise AssertionError(f"unexpected boto3.client call: {service_name}")

    monkeypatch.setattr(eks_kubernetes_client.boto3, "client", fake_boto3_client)
    monkeypatch.setattr(eks_kubernetes_client.ssl, "create_default_context", lambda cafile=None: mock.Mock())

    token_counter = {"n": 0}

    def fake_bearer_token(self: EksKubernetesClient) -> str:
        token_counter["n"] += 1
        return f"fake-token-{token_counter['n']}"

    monkeypatch.setattr(EksKubernetesClient, "eks_bearer_token", fake_bearer_token)

    return {
        "describe_cluster": describe_cluster_mock,
        "token_counter": token_counter,
    }


def test_get_eks_client_caches_instance_per_cluster_name(patched_eks) -> None:
    cluster_name = "test-cluster-cache"

    client_a = get_eks_client(cluster_name)
    client_b = get_eks_client(cluster_name)

    assert client_a is client_b
    assert patched_eks["describe_cluster"].call_count == 1


def test_get_eks_client_different_cluster_names_create_separate_instances(patched_eks) -> None:
    client_a = get_eks_client("cluster-a")
    client_b = get_eks_client("cluster-b")

    assert client_a is not client_b
    assert patched_eks["describe_cluster"].call_count == 2


def test_static_cluster_connection_skips_public_eks_api(patched_eks, monkeypatch) -> None:
    cluster_name = "private-cluster"
    ca_data = base64.b64encode(b"terraform-provided-ca").decode("utf-8")
    monkeypatch.setenv("EKS_CLUSTER_NAME", cluster_name)
    monkeypatch.setenv("EKS_CLUSTER_ENDPOINT", "https://private.eks.local")
    monkeypatch.setenv("EKS_CLUSTER_CA_DATA", ca_data)

    client = get_eks_client(cluster_name)

    assert client.endpoint == "https://private.eks.local"
    assert patched_eks["describe_cluster"].call_count == 0


def test_static_cluster_connection_rejects_partial_configuration(patched_eks, monkeypatch) -> None:
    monkeypatch.setenv("EKS_CLUSTER_ENDPOINT", "https://private.eks.local")

    with pytest.raises(RuntimeError, match="must be configured together"):
        get_eks_client("private-cluster")

    assert patched_eks["describe_cluster"].call_count == 0


def test_token_is_reused_within_ttl(patched_eks, monkeypatch) -> None:
    fake_time = {"t": 1000.0}
    monkeypatch.setattr(eks_kubernetes_client.time, "monotonic", lambda: fake_time["t"])

    client = get_eks_client("cluster-ttl-reuse")
    assert patched_eks["token_counter"]["n"] == 1
    first_token = client.token

    fake_time["t"] += 60.0  # well within the 600s default TTL
    client._ensure_fresh_token()

    assert client.token == first_token
    assert patched_eks["token_counter"]["n"] == 1


def test_token_is_regenerated_after_ttl_expires(patched_eks, monkeypatch) -> None:
    fake_time = {"t": 1000.0}
    monkeypatch.setattr(eks_kubernetes_client.time, "monotonic", lambda: fake_time["t"])

    client = get_eks_client("cluster-ttl-expiry")
    assert patched_eks["token_counter"]["n"] == 1
    first_token = client.token

    fake_time["t"] += 601.0  # past the 600s default TTL
    client._ensure_fresh_token()

    assert client.token != first_token
    assert patched_eks["token_counter"]["n"] == 2


def test_token_ttl_env_override(patched_eks, monkeypatch) -> None:
    monkeypatch.setenv("EKS_BEARER_TOKEN_TTL_SECONDS", "5")
    fake_time = {"t": 2000.0}
    monkeypatch.setattr(eks_kubernetes_client.time, "monotonic", lambda: fake_time["t"])

    client = get_eks_client("cluster-ttl-override")
    assert client.token_ttl_seconds == 5.0
    assert patched_eks["token_counter"]["n"] == 1

    fake_time["t"] += 6.0
    client._ensure_fresh_token()

    assert patched_eks["token_counter"]["n"] == 2


def test_request_reuses_token_within_ttl(patched_eks, monkeypatch) -> None:
    fake_time = {"t": 3000.0}
    monkeypatch.setattr(eks_kubernetes_client.time, "monotonic", lambda: fake_time["t"])

    client = get_eks_client("cluster-request-reuse")
    assert patched_eks["token_counter"]["n"] == 1

    fake_urlopen = mock.MagicMock()
    fake_urlopen.__enter__.return_value.status = 200
    fake_urlopen.__enter__.return_value.read.return_value = b"{}"
    monkeypatch.setattr(eks_kubernetes_client.urllib.request, "urlopen", lambda *a, **k: fake_urlopen)

    fake_time["t"] += 60.0
    client.request("GET", "/api/v1/namespaces")

    assert patched_eks["token_counter"]["n"] == 1


def test_ca_file_not_rewritten_if_it_already_exists(patched_eks) -> None:
    cluster_name = "cluster-existing-ca"
    ca_path = f"/tmp/{cluster_name}-ca.crt"

    # Pre-create the CA file with sentinel content.
    with open(ca_path, "wb") as f:
        f.write(b"sentinel-existing-ca-content")

    with mock.patch.object(eks_kubernetes_client.ssl, "create_default_context") as fake_ssl_ctx:
        get_eks_client(cluster_name)
        fake_ssl_ctx.assert_called_once_with(cafile=ca_path)

    with open(ca_path, "rb") as f:
        content = f.read()
    assert content == b"sentinel-existing-ca-content"
