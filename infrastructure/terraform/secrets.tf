resource "aws_secretsmanager_secret" "free5gc_webui" {
  name                    = "${local.name_prefix}/free5gc-webui"
  description             = "Runtime-only free5GC WebUI credentials; value is populated out-of-band by deploy.sh"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "api_access" {
  name                    = "${local.name_prefix}/api-access-token"
  description             = "Runtime-only token for the HTTP and WebSocket API authorizer"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "smf_qer_actuator" {
  name                    = "${local.name_prefix}/smf-qer-actuator-token"
  description             = "Runtime-only token authorizing PFCP QER actuation through the private SMF OAM endpoint"
  recovery_window_in_days = 7
}

output "free5gc_webui_secret_arn" {
  description = "Secret container ARN populated out-of-band by deploy.sh"
  value       = aws_secretsmanager_secret.free5gc_webui.arn
}

output "api_access_secret_arn" {
  description = "API access token secret container ARN populated out-of-band by deploy.sh"
  value       = aws_secretsmanager_secret.api_access.arn
}

output "smf_qer_actuator_secret_arn" {
  description = "SMF QER actuator token secret container populated out-of-band by deploy.sh"
  value       = aws_secretsmanager_secret.smf_qer_actuator.arn
}
