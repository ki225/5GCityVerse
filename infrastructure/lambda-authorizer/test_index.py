import importlib
import json
import os
import unittest
from unittest.mock import patch

authorizer = importlib.import_module("index")


class FakeSecretsManager:
    def __init__(self, tokens):
        self.tokens = iter(tokens)
        self.calls = 0

    def get_secret_value(self, **_kwargs):
        self.calls += 1
        return {"SecretString": json.dumps({"token": next(self.tokens)})}


class AuthorizerTests(unittest.TestCase):
    def setUp(self):
        os.environ["API_ACCESS_SECRET_ARN"] = "test-secret"
        os.environ["TOKEN_CACHE_TTL_SECONDS"] = "30"
        authorizer._expected_token = None
        authorizer._secret_expires_at = 0.0

    def test_http_bearer_token_is_required(self):
        client = FakeSecretsManager(["correct"])
        event = {"version": "2.0", "headers": {"Authorization": "Bearer correct"}}
        with patch.object(authorizer.boto3, "client", return_value=client), patch.object(
            authorizer.time, "monotonic", return_value=100.0
        ):
            self.assertTrue(authorizer.lambda_handler(event, None)["isAuthorized"])
            event["headers"]["Authorization"] = "Bearer wrong"
            self.assertFalse(authorizer.lambda_handler(event, None)["isAuthorized"])

    def test_http_without_token_is_denied(self):
        client = FakeSecretsManager(["correct"])
        event = {"version": "2.0", "headers": {}}
        with patch.object(authorizer.boto3, "client", return_value=client), patch.object(
            authorizer.time, "monotonic", return_value=100.0
        ):
            self.assertFalse(authorizer.lambda_handler(event, None)["isAuthorized"])

    def test_rotated_secret_is_loaded_after_bounded_ttl(self):
        client = FakeSecretsManager(["old", "new"])
        event = {"version": "2.0", "headers": {"authorization": "Bearer old"}}
        with patch.object(authorizer.boto3, "client", return_value=client), patch.object(
            authorizer.time, "monotonic", side_effect=[100.0, 120.0, 131.0, 132.0]
        ):
            self.assertTrue(authorizer.lambda_handler(event, None)["isAuthorized"])
            self.assertTrue(authorizer.lambda_handler(event, None)["isAuthorized"])
            self.assertFalse(authorizer.lambda_handler(event, None)["isAuthorized"])
            event["headers"]["authorization"] = "Bearer new"
            self.assertTrue(authorizer.lambda_handler(event, None)["isAuthorized"])
        self.assertEqual(client.calls, 2)

    def test_websocket_query_token_returns_scoped_policy(self):
        client = FakeSecretsManager(["socket-token"])
        event = {"methodArn": "arn:aws:execute-api:r:a:api/prod/$connect", "queryStringParameters": {"token": "socket-token"}}
        with patch.object(authorizer.boto3, "client", return_value=client), patch.object(
            authorizer.time, "monotonic", return_value=1.0
        ):
            result = authorizer.lambda_handler(event, None)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Allow")
        self.assertEqual(result["policyDocument"]["Statement"][0]["Resource"], event["methodArn"])

    def test_websocket_without_token_is_denied(self):
        client = FakeSecretsManager(["socket-token"])
        event = {"methodArn": "arn:aws:execute-api:r:a:api/prod/$connect"}
        with patch.object(authorizer.boto3, "client", return_value=client), patch.object(
            authorizer.time, "monotonic", return_value=1.0
        ):
            result = authorizer.lambda_handler(event, None)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Deny")

    def test_websocket_wrong_token_is_denied(self):
        client = FakeSecretsManager(["socket-token"])
        event = {
            "methodArn": "arn:aws:execute-api:r:a:api/prod/$connect",
            "queryStringParameters": {"token": "wrong"},
        }
        with patch.object(authorizer.boto3, "client", return_value=client), patch.object(
            authorizer.time, "monotonic", return_value=1.0
        ):
            result = authorizer.lambda_handler(event, None)
        self.assertEqual(result["policyDocument"]["Statement"][0]["Effect"], "Deny")


if __name__ == "__main__":
    unittest.main()
