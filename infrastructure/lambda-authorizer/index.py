"""Shared-token authorizer for API Gateway HTTP API and WebSocket $connect."""

import hmac
import json
import os
import time

import boto3

_expected_token = None
_secret_expires_at = 0.0


def _secret_token():
    global _expected_token, _secret_expires_at
    now = time.monotonic()
    if _expected_token is None or now >= _secret_expires_at:
        value = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["API_ACCESS_SECRET_ARN"]
        )
        _expected_token = json.loads(value["SecretString"])["token"]
        _secret_expires_at = now + max(1, int(os.environ.get("TOKEN_CACHE_TTL_SECONDS", "30")))
    return _expected_token


def _presented_token(event):
    headers = {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:]
    return str((event.get("queryStringParameters") or {}).get("token", ""))


def lambda_handler(event, _context):
    allowed = hmac.compare_digest(_presented_token(event), _secret_token())
    if event.get("version") == "2.0":
        return {"isAuthorized": allowed, "context": {"auth": "shared-token"}}

    effect = "Allow" if allowed else "Deny"
    return {
        "principalId": "cityverse-client" if allowed else "unauthorized",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Action": "execute-api:Invoke", "Effect": effect, "Resource": event["methodArn"]}],
        },
    }
