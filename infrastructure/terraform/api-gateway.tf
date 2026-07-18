resource "aws_apigatewayv2_api" "http" {
  name          = "${local.name_prefix}-http"
  protocol_type = "HTTP"

  cors_configuration {
    allow_headers = ["authorization", "content-type", "x-session-id"]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_origins = ["https://${aws_cloudfront_distribution.frontend.domain_name}"]
    max_age       = 300
  }
}

resource "aws_apigatewayv2_integration" "http_lambda" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.backend_lambda.lambda_function_invoke_arn
  payload_format_version = "2.0"
}

locals {
  api_authorizer_name = "${local.name_prefix}-api-authorizer"
}

data "aws_iam_policy_document" "api_authorizer_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api_authorizer" {
  name               = "${local.api_authorizer_name}-role"
  assume_role_policy = data.aws_iam_policy_document.api_authorizer_assume_role.json
}

data "aws_iam_policy_document" "api_authorizer" {
  statement {
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.api_authorizer_name}:*",
    ]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.api_access.arn]
  }
}

resource "aws_iam_role_policy" "api_authorizer" {
  name   = "${local.api_authorizer_name}-policy"
  role   = aws_iam_role.api_authorizer.id
  policy = data.aws_iam_policy_document.api_authorizer.json
}

module "api_authorizer_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.8.0"

  create_role                       = false
  lambda_role                       = aws_iam_role.api_authorizer.arn
  cloudwatch_logs_retention_in_days = 14
  function_name                     = local.api_authorizer_name
  handler                           = "index.lambda_handler"
  runtime                           = "python3.12"
  source_path                       = abspath("${path.module}/../lambda-authorizer")
  timeout                           = 10

  environment_variables = {
    API_ACCESS_SECRET_ARN   = aws_secretsmanager_secret.api_access.arn
    TOKEN_CACHE_TTL_SECONDS = "30"
  }
}

resource "aws_lambda_permission" "http_api_authorizer" {
  statement_id  = "AllowHTTPAPIGatewayAuthorizerInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.api_authorizer_lambda.lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/authorizers/*"
}

resource "aws_lambda_permission" "websocket_api_authorizer" {
  statement_id  = "AllowWebSocketAPIGatewayAuthorizerInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.api_authorizer_lambda.lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.websocket.execution_arn}/authorizers/*"
}

resource "aws_apigatewayv2_authorizer" "shared_token" {
  count = var.api_auth_enabled ? 1 : 0

  api_id                            = aws_apigatewayv2_api.http.id
  authorizer_type                   = "REQUEST"
  authorizer_uri                    = module.api_authorizer_lambda.lambda_function_invoke_arn
  authorizer_payload_format_version = "2.0"
  authorizer_result_ttl_in_seconds  = 0
  enable_simple_responses           = true
  identity_sources                  = ["$request.header.Authorization"]
  name                              = "shared-token"
}

resource "aws_apigatewayv2_authorizer" "websocket_shared_token" {
  count = var.api_auth_enabled ? 1 : 0

  api_id           = aws_apigatewayv2_api.websocket.id
  authorizer_type  = "REQUEST"
  authorizer_uri   = module.api_authorizer_lambda.lambda_function_invoke_arn
  identity_sources = ["route.request.querystring.token"]
  name             = "shared-token"
}

resource "aws_apigatewayv2_route" "http_default" {
  api_id             = aws_apigatewayv2_api.http.id
  route_key          = "$default"
  target             = "integrations/${aws_apigatewayv2_integration.http_lambda.id}"
  authorization_type = var.api_auth_enabled ? "CUSTOM" : "NONE"
  authorizer_id      = var.api_auth_enabled ? aws_apigatewayv2_authorizer.shared_token[0].id : null
}

# A $default route authorizer also catches browser preflight requests. AWS
# recommends a more-specific unauthenticated OPTIONS route so managed CORS can
# answer the preflight before the custom authorizer is evaluated.
resource "aws_apigatewayv2_route" "http_options" {
  api_id             = aws_apigatewayv2_api.http.id
  route_key          = "OPTIONS /{proxy+}"
  target             = "integrations/${aws_apigatewayv2_integration.http_lambda.id}"
  authorization_type = "NONE"
}

resource "aws_apigatewayv2_stage" "http_default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = local.http_stage_name
  auto_deploy = true
}

resource "aws_apigatewayv2_api" "websocket" {
  name                       = "${local.name_prefix}-ws"
  protocol_type              = "WEBSOCKET"
  route_selection_expression = "$request.body.action"
}

resource "aws_apigatewayv2_integration" "ws_lambda" {
  api_id           = aws_apigatewayv2_api.websocket.id
  integration_type = "AWS_PROXY"
  integration_uri  = module.backend_lambda.lambda_function_invoke_arn
}

resource "aws_apigatewayv2_route" "ws_connect" {
  api_id             = aws_apigatewayv2_api.websocket.id
  route_key          = "$connect"
  target             = "integrations/${aws_apigatewayv2_integration.ws_lambda.id}"
  authorization_type = var.api_auth_enabled ? "CUSTOM" : "NONE"
  authorizer_id      = var.api_auth_enabled ? aws_apigatewayv2_authorizer.websocket_shared_token[0].id : null
}

resource "aws_apigatewayv2_route" "ws_disconnect" {
  api_id    = aws_apigatewayv2_api.websocket.id
  route_key = "$disconnect"
  target    = "integrations/${aws_apigatewayv2_integration.ws_lambda.id}"
}

resource "aws_apigatewayv2_route" "ws_default" {
  api_id    = aws_apigatewayv2_api.websocket.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.ws_lambda.id}"
}

resource "aws_apigatewayv2_stage" "websocket" {
  api_id      = aws_apigatewayv2_api.websocket.id
  name        = local.ws_stage_name
  auto_deploy = true
}

resource "aws_lambda_permission" "http" {
  statement_id  = "AllowHTTPAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.backend_lambda.lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}

resource "aws_lambda_permission" "websocket" {
  statement_id  = "AllowWebSocketAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.backend_lambda.lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.websocket.execution_arn}/*/*"
}
