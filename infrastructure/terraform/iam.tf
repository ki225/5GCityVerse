data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.lambda_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "lambda_policy" {
  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "${module.backend_lambda.lambda_cloudwatch_log_group_arn}:*",
      "${module.nef_pfd_lambda.lambda_cloudwatch_log_group_arn}:*",
      "${module.nef_qos_lambda.lambda_cloudwatch_log_group_arn}:*",
      "${module.nef_traffic_influence_lambda.lambda_cloudwatch_log_group_arn}:*",
    ]
  }

  statement {
    actions = [
      "dynamodb:DeleteItem",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.state.arn]
  }

  statement {
    actions   = ["execute-api:ManageConnections"]
    resources = ["arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:${aws_apigatewayv2_api.websocket.id}/${local.ws_stage_name}/POST/@connections/*"]
  }

  statement {
    actions   = ["eks:DescribeCluster"]
    resources = [aws_eks_cluster.free5gc.arn]
  }

  statement {
    actions = ["lambda:InvokeFunction"]
    resources = [
      module.backend_lambda.lambda_function_arn,
      module.nef_pfd_lambda.lambda_function_arn,
      module.nef_qos_lambda.lambda_function_arn,
      module.nef_traffic_influence_lambda.lambda_function_arn,
    ]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${local.lambda_name}-policy"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_policy.json
}
