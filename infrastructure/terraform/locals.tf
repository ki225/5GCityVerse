locals {
  frontend_bucket_name = "${local.name_prefix}-frontend-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  http_stage_name      = "$default"
  lambda_name          = "${local.name_prefix}-backend"
  name_prefix          = "${var.project_name}-${var.environment}"
  ws_stage_name        = "prod"
}
