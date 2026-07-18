"""Load free5GC credentials at Lambda runtime without Terraform/Lambda env plaintext."""

import importlib
import json
import os
from typing import Any

import boto3

_delegate = None


def _load_delegate():
    global _delegate
    if _delegate is None:
        secret_arn = os.environ["FREE5GC_WEBUI_SECRET_ARN"]
        response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
        credentials = json.loads(response["SecretString"])
        os.environ["FREE5GC_WEBUI_USERNAME"] = credentials["username"]
        os.environ["FREE5GC_WEBUI_PASSWORD"] = credentials["password"]
        actuator_secret_arn = os.environ.get("SMF_QER_ACTUATOR_SECRET_ARN", "").strip()
        if actuator_secret_arn:
            actuator_response = boto3.client("secretsmanager").get_secret_value(SecretId=actuator_secret_arn)
            actuator_secret = json.loads(actuator_response["SecretString"])
            os.environ["SMF_QER_ACTUATOR_TOKEN"] = actuator_secret["token"]
        _delegate = importlib.import_module("index").lambda_handler
    return _delegate


def lambda_handler(event: dict[str, Any], context: Any) -> Any:
    return _load_delegate()(event, context)
