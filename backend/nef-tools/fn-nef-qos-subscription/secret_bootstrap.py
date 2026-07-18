"""Load free5GC credentials from Secrets Manager before invoking the NEF tool."""

import importlib
import json
import os
from typing import Any

import boto3

_delegate = None


def _load_delegate():
    global _delegate
    if _delegate is None:
        response = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["FREE5GC_WEBUI_SECRET_ARN"]
        )
        credentials = json.loads(response["SecretString"])
        os.environ["FREE5GC_WEBUI_USERNAME"] = credentials["username"]
        os.environ["FREE5GC_WEBUI_PASSWORD"] = credentials["password"]
        _delegate = importlib.import_module("index").lambda_handler
    return _delegate


def lambda_handler(event: dict[str, Any], context: Any) -> Any:
    return _load_delegate()(event, context)
