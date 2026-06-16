from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
from typing import Any

import boto3
from botocore.signers import RequestSigner


class EksKubernetesClient:
    def __init__(self, cluster_name: str, aws_session: boto3.session.Session | None = None) -> None:
        self.cluster_name = cluster_name
        self.aws_session = aws_session or boto3.session.Session()
        eks = boto3.client("eks")
        cluster = eks.describe_cluster(name=cluster_name)["cluster"]
        self.endpoint = cluster["endpoint"].rstrip("/")
        ca_path = f"/tmp/{cluster_name}-ca.crt"
        ca_data = base64.b64decode(cluster["certificateAuthority"]["data"])
        with open(ca_path, "wb") as ca_file:
            ca_file.write(ca_data)
        self.ssl_context = ssl.create_default_context(cafile=ca_path)
        self.token = self.eks_bearer_token()

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        content_type: str = "application/json",
        ignore_404: bool = False,
    ) -> tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json", "Content-Type": content_type}
        req = urllib.request.Request(f"{self.endpoint}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10, context=self.ssl_context) as res:
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
        credentials = self.aws_session.get_credentials().get_frozen_credentials()
        region = self.aws_session.region_name or "ap-northeast-1"
        signer = RequestSigner(
            "sts",
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

