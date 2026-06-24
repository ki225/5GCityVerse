locals {
  nef_pfd_lambda_name               = "${local.name_prefix}-nef-pfd-create"
  nef_qos_lambda_name               = "${local.name_prefix}-nef-qos-subscription"
  nef_traffic_influence_lambda_name = "${local.name_prefix}-nef-traffic-influence"
}

module "nef_qos_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.8.0"

  create_role = false
  lambda_role = aws_iam_role.lambda.arn

  cloudwatch_logs_retention_in_days = 14
  function_name                     = local.nef_qos_lambda_name
  handler                           = "index.lambda_handler"
  logging_log_format                = "Text"
  runtime                           = "python3.12"
  source_path                       = abspath("${path.module}/../../backend/nef-tools/fn-nef-qos-subscription")
  timeout                           = 30

  environment_variables = {
    FREE5GC_WEBUI_PASSWORD = var.free5gc_webui_password
    FREE5GC_WEBUI_URL      = var.free5gc_webui_url
    FREE5GC_WEBUI_USERNAME = var.free5gc_webui_username
    NEF_AF_ID              = var.nef_af_id
    NEF_BASE_URL           = var.nef_base_url
  }
}

module "nef_traffic_influence_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.8.0"

  create_role = false
  lambda_role = aws_iam_role.lambda.arn

  cloudwatch_logs_retention_in_days = 14
  function_name                     = local.nef_traffic_influence_lambda_name
  handler                           = "index.lambda_handler"
  logging_log_format                = "Text"
  runtime                           = "python3.12"
  source_path                       = abspath("${path.module}/../../backend/nef-tools/fn-nef-traffic-influence")
  timeout                           = 30

  environment_variables = {
    NEF_AF_ID    = var.nef_af_id
    NEF_BASE_URL = var.nef_base_url
  }
}

module "nef_pfd_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.8.0"

  create_role = false
  lambda_role = aws_iam_role.lambda.arn

  cloudwatch_logs_retention_in_days = 14
  function_name                     = local.nef_pfd_lambda_name
  handler                           = "index.lambda_handler"
  logging_log_format                = "Text"
  runtime                           = "python3.12"
  source_path                       = abspath("${path.module}/../../backend/nef-tools/fn-nef-pfd-create")
  timeout                           = 30

  environment_variables = {
    NEF_AF_ID    = var.nef_af_id
    NEF_BASE_URL = var.nef_base_url
  }
}
