module "backend_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.8.0"

  create_role = false
  lambda_role = aws_iam_role.lambda.arn

  cloudwatch_logs_retention_in_days = 14
  function_name                     = local.lambda_name
  handler                           = "index.lambda_handler"
  logging_log_format                = "Text"
  runtime                           = "python3.12"
  source_path                       = abspath("${path.module}/../../backend/aws-app")
  timeout                           = 120

  environment_variables = {
    APIGW_WS_ENDPOINT                   = "https://${aws_apigatewayv2_api.websocket.id}.execute-api.${var.aws_region}.amazonaws.com/${local.ws_stage_name}"
    ASYNC_TRIGGER_REVISION              = "20260618-self-invoke"
    DYNAMODB_TABLE                      = aws_dynamodb_table.state.name
    EKS_CLUSTER_NAME                    = aws_eks_cluster.free5gc.name
    FREE5GC_IMSI_PREFIX                 = "20893000000"
    FREE5GC_NAMESPACE                   = "free5gc"
    FREE5GC_PLMN_ID                     = var.free5gc_plmn_id
    FREE5GC_SCENARIO_UE_ID              = "imsi-208930000000001"
    FREE5GC_STATUS_HTTP_TIMEOUT_SECONDS = "1.5"
    FREE5GC_WEBUI_PASSWORD              = var.free5gc_webui_password
    FREE5GC_WEBUI_URL                   = var.free5gc_webui_url
    FREE5GC_WEBUI_USERNAME              = var.free5gc_webui_username
    HTTP_JSON_TIMEOUT_SECONDS           = "3"
    KUBERNETES_REQUEST_TIMEOUT_SECONDS  = "2"
    NEF_PFD_LAMBDA_NAME                 = module.nef_pfd_lambda.lambda_function_name
    NEF_QOS_LAMBDA_NAME                 = module.nef_qos_lambda.lambda_function_name
    NEF_TRAFFIC_INFLUENCE_LAMBDA_NAME   = module.nef_traffic_influence_lambda.lambda_function_name
    PROMETHEUS_QUERY_TIMEOUT_SECONDS    = "0.8"
    PROMETHEUS_URL                      = var.prometheus_url
    RUNTIME_SUBSCRIBER_UPSERT_LIMIT     = "10"
    STATUS_INCLUDE_EKS                  = "false"
    UERANSIM_UE_DEPLOYMENT              = "ueransim-city-ue"
  }
}
