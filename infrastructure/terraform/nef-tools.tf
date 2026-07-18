locals {
  nef_pfd_lambda_name               = "${local.name_prefix}-nef-pfd-create"
  nef_qos_lambda_name               = "${local.name_prefix}-nef-qos-subscription"
  nef_traffic_influence_lambda_name = "${local.name_prefix}-nef-traffic-influence"
  nef_lambda_package_patterns = [
    "!.*__pycache__/.*",
    "!.*\\.py[cod]$",
  ]
}

module "nef_qos_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.8.0"

  depends_on = [aws_iam_role_policy.lambda_vpc_eni]

  create_role = false
  lambda_role = aws_iam_role.lambda.arn

  cloudwatch_logs_retention_in_days = 14
  function_name                     = local.nef_qos_lambda_name
  handler                           = "secret_bootstrap.lambda_handler"
  logging_log_format                = "Text"
  runtime                           = "python3.12"
  source_path = {
    path     = abspath("${path.module}/../../backend/nef-tools/fn-nef-qos-subscription")
    patterns = local.nef_lambda_package_patterns
  }
  timeout = 30

  vpc_subnet_ids         = aws_subnet.eks_private[*].id
  vpc_security_group_ids = [aws_security_group.lambda_private.id]
  attach_network_policy  = false

  environment_variables = {
    FREE5GC_WEBUI_SECRET_ARN = aws_secretsmanager_secret.free5gc_webui.arn
    FREE5GC_WEBUI_URL        = var.free5gc_webui_url
    FREE5GC_PLMN_ID          = var.free5gc_plmn_id
    NEF_AF_ID                = var.nef_af_id
    NEF_BASE_URL             = var.nef_base_url
  }
}

module "nef_traffic_influence_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.8.0"

  depends_on = [aws_iam_role_policy.lambda_vpc_eni]

  create_role = false
  lambda_role = aws_iam_role.lambda.arn

  cloudwatch_logs_retention_in_days = 14
  function_name                     = local.nef_traffic_influence_lambda_name
  handler                           = "index.lambda_handler"
  logging_log_format                = "Text"
  runtime                           = "python3.12"
  source_path = {
    path     = abspath("${path.module}/../../backend/nef-tools/fn-nef-traffic-influence")
    patterns = local.nef_lambda_package_patterns
  }
  timeout = 30

  vpc_subnet_ids         = aws_subnet.eks_private[*].id
  vpc_security_group_ids = [aws_security_group.lambda_private.id]
  attach_network_policy  = false

  environment_variables = {
    NEF_AF_ID    = var.nef_af_id
    NEF_BASE_URL = var.nef_base_url
  }
}

module "nef_pfd_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.8.0"

  depends_on = [aws_iam_role_policy.lambda_vpc_eni]

  create_role = false
  lambda_role = aws_iam_role.lambda.arn

  cloudwatch_logs_retention_in_days = 14
  function_name                     = local.nef_pfd_lambda_name
  handler                           = "index.lambda_handler"
  logging_log_format                = "Text"
  runtime                           = "python3.12"
  source_path = {
    path     = abspath("${path.module}/../../backend/nef-tools/fn-nef-pfd-create")
    patterns = local.nef_lambda_package_patterns
  }
  timeout = 30

  vpc_subnet_ids         = aws_subnet.eks_private[*].id
  vpc_security_group_ids = [aws_security_group.lambda_private.id]
  attach_network_policy  = false

  environment_variables = {
    NEF_AF_ID    = var.nef_af_id
    NEF_BASE_URL = var.nef_base_url
  }
}
