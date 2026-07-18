from __future__ import annotations

import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

import boto3
from botocore.credentials import Credentials
from botocore.signers import RequestSigner


class EksKubernetesClient:
    def __init__(self, cluster_name: str, aws_session: boto3.session.Session | None = None) -> None:
        self.cluster_name = cluster_name
        self.aws_session = aws_session or boto3.session.Session()
        configured_endpoint = os.environ.get("EKS_CLUSTER_ENDPOINT", "").strip()
        configured_ca = os.environ.get("EKS_CLUSTER_CA_DATA", "").strip()
        configured_name = os.environ.get("EKS_CLUSTER_NAME", "").strip()
        if bool(configured_endpoint) != bool(configured_ca):
            raise RuntimeError("EKS_CLUSTER_ENDPOINT and EKS_CLUSTER_CA_DATA must be configured together")
        if configured_endpoint:
            if configured_name and configured_name != cluster_name:
                raise RuntimeError(
                    f"configured EKS endpoint belongs to {configured_name}, not requested cluster {cluster_name}"
                )
            cluster = {
                "endpoint": configured_endpoint,
                "certificateAuthority": {"data": configured_ca},
            }
        else:
            # Local/developer fallback. Production Lambda receives the private
            # endpoint and CA from Terraform so request handling never depends
            # on reaching the public EKS DescribeCluster API from a VPC subnet.
            eks = boto3.client("eks")
            cluster = eks.describe_cluster(name=cluster_name)["cluster"]
        self.endpoint = cluster["endpoint"].rstrip("/")
        ca_path = f"/tmp/{cluster_name}-ca.crt"
        if not os.path.exists(ca_path):
            ca_data = base64.b64decode(cluster["certificateAuthority"]["data"])
            with open(ca_path, "wb") as ca_file:
                ca_file.write(ca_data)
        self.ssl_context = ssl.create_default_context(cafile=ca_path)
        self.token_ttl_seconds = float(os.environ.get("EKS_BEARER_TOKEN_TTL_SECONDS", "600"))
        self.token = self.eks_bearer_token()
        self.token_fetched_at = time.monotonic()
        self.timeout = float(os.environ.get("KUBERNETES_REQUEST_TIMEOUT_SECONDS", "2"))

    def _ensure_fresh_token(self) -> None:
        if time.monotonic() - self.token_fetched_at >= self.token_ttl_seconds:
            self.token = self.eks_bearer_token()
            self.token_fetched_at = time.monotonic()

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        content_type: str = "application/json",
        ignore_404: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        self._ensure_fresh_token()
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json", "Content-Type": content_type}
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(f"{self.endpoint}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as res:
                raw = res.read().decode("utf-8")
                return res.status, json.loads(raw or "{}")
        except urllib.error.HTTPError as exc:
            if ignore_404 and exc.code == 404:
                return exc.code, {}
            raw = exc.read().decode("utf-8")
            try:
                parsed = json.loads(raw or "{}")
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return exc.code, parsed

    def request_text(self, method: str, path: str, ignore_404: bool = False) -> tuple[int, str]:
        self._ensure_fresh_token()
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json, */*"}
        req = urllib.request.Request(f"{self.endpoint}{path}", headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as res:
                return res.status, res.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if ignore_404 and exc.code == 404:
                return exc.code, ""
            return exc.code, exc.read().decode("utf-8", errors="replace")

    def create_or_patch(self, create_path: str, patch_path: str, manifest: dict[str, Any]) -> tuple[int, Any]:
        status, data = self.request("POST", create_path, manifest)
        if status == 409:
            return self.patch(patch_path, manifest)
        if status >= 300:
            raise RuntimeError(f"Kubernetes create failed: HTTP {status} {data}")
        return status, data

    def patch(self, path: str, body: dict[str, Any], ignore_404: bool = False) -> tuple[int, Any]:
        status, data = self.request("PATCH", path, body, "application/merge-patch+json", ignore_404=ignore_404)
        if status >= 300 and not (ignore_404 and status == 404):
            raise RuntimeError(f"Kubernetes patch failed: HTTP {status} {data}")
        return status, data

    def delete(self, path: str, ignore_404: bool = True) -> tuple[int, Any]:
        status, data = self.request("DELETE", path, ignore_404=ignore_404)
        if status >= 300 and not (ignore_404 and status == 404):
            raise RuntimeError(f"Kubernetes delete failed: HTTP {status} {data}")
        return status, data

    def eks_bearer_token(self) -> str:
        session_credentials = self.aws_session.get_credentials()
        frozen_credentials = (
            session_credentials.get_frozen_credentials()
            if hasattr(session_credentials, "get_frozen_credentials")
            else session_credentials
        )
        credentials = Credentials(
            frozen_credentials.access_key,
            frozen_credentials.secret_key,
            frozen_credentials.token,
        )
        region = self.aws_session.region_name or "ap-northeast-1"
        sts = self.aws_session.client("sts", region_name=region)
        signer = RequestSigner(
            sts.meta.service_model.service_id,
            region,
            "sts",
            "v4",
            credentials,
            self.aws_session.events,
        )
        params = {
            "method": "GET",
            "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
            "body": {},
            "headers": {"x-k8s-aws-id": self.cluster_name},
            "context": {},
        }
        signed_url = signer.generate_presigned_url(
            params,
            region_name=region,
            expires_in=60,
            operation_name="GetCallerIdentity",
        )
        token = base64.urlsafe_b64encode(signed_url.encode("utf-8")).decode("utf-8").rstrip("=")
        return f"k8s-aws-v1.{token}"


_eks_client_cache: dict[str, EksKubernetesClient] = {}


def get_eks_client(cluster_name: str, aws_session: boto3.session.Session | None = None) -> EksKubernetesClient:
    if cluster_name not in _eks_client_cache:
        _eks_client_cache[cluster_name] = EksKubernetesClient(cluster_name, aws_session)
    return _eks_client_cache[cluster_name]
