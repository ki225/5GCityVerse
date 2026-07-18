module "backend_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.8.0"

  # The function is VPC-attached, so AWS validates ENI permissions during
  # CreateFunction.  Make the inline execution-role policy a hard dependency
  # instead of relying on IAM propagation racing the Lambda API call.
  depends_on = [aws_iam_role_policy.lambda_vpc_eni]

  create_role = false
  lambda_role = aws_iam_role.lambda.arn

  cloudwatch_logs_retention_in_days = 14
  function_name                     = local.lambda_name
  handler                           = "secret_bootstrap.lambda_handler"
  logging_log_format                = "Text"
  runtime                           = "python3.12"
  source_path                       = abspath("${path.module}/../../backend/aws-app")
  # Also bounds the asynchronous reset worker. Its verified cleanup sequence
  # can wait for four UPFs, SMF, AMF, gNB and resident UEs to roll in order;
  # the former 420s ceiling could terminate it before lease release/status.
  timeout = 900

  # Lambda must use the VPC-private EKS endpoint.  Keep ENI permissions on the
  # pre-existing execution role instead of letting the module attach its broad
  # managed VPC policy.
  vpc_subnet_ids         = aws_subnet.eks_private[*].id
  vpc_security_group_ids = [aws_security_group.lambda_private.id]
  attach_network_policy  = false

  environment_variables = {
    APIGW_WS_ENDPOINT                   = "https://${aws_apigatewayv2_api.websocket.id}.execute-api.${var.aws_region}.amazonaws.com/${local.ws_stage_name}"
    ASYNC_TRIGGER_REVISION              = "20260618-self-invoke"
    DYNAMODB_TABLE                      = aws_dynamodb_table.state.name
    EKS_CLUSTER_CA_DATA                 = aws_eks_cluster.free5gc.certificate_authority[0].data
    EKS_CLUSTER_ENDPOINT                = aws_eks_cluster.free5gc.endpoint
    EKS_CLUSTER_NAME                    = aws_eks_cluster.free5gc.name
    FREE5GC_IMSI_PREFIX                 = "20893000000"
    FREE5GC_NAMESPACE                   = "free5gc"
    FREE5GC_PLMN_ID                     = var.free5gc_plmn_id
    FREE5GC_SCENARIO_UE_ID              = "imsi-208930000000001"
    FREE5GC_STATUS_HTTP_TIMEOUT_SECONDS = "5"
    FREE5GC_WEBUI_SECRET_ARN            = aws_secretsmanager_secret.free5gc_webui.arn
    FREE5GC_WEBUI_URL                   = var.free5gc_webui_url
    HTTP_JSON_TIMEOUT_SECONDS           = "5"
    IPERF3_IMAGE                        = "networkstatic/iperf3@sha256:c1e4a239a83d1d60975bce1c9b7661af5517e362bf335f66a2c5b6adaeb4f19f"
    KUBERNETES_REQUEST_TIMEOUT_SECONDS  = "15"
    NEF_PFD_LAMBDA_NAME                 = module.nef_pfd_lambda.lambda_function_name
    NEF_QOS_LAMBDA_NAME                 = module.nef_qos_lambda.lambda_function_name
    NEF_TRAFFIC_INFLUENCE_LAMBDA_NAME   = module.nef_traffic_influence_lambda.lambda_function_name
    PROMETHEUS_QUERY_TIMEOUT_SECONDS    = "0.8"
    PROMETHEUS_URL                      = var.prometheus_url
    RUNTIME_SUBSCRIBER_UPSERT_LIMIT     = "10"
    SMF_QER_ACTUATOR_SECRET_ARN         = aws_secretsmanager_secret.smf_qer_actuator.arn
    STATUS_INCLUDE_EKS                  = "false"
    UERANSIM_UE_DEPLOYMENT              = "ueransim-city-ue"
    UERANSIM_IMAGE                      = "${aws_ecr_repository.ueransim.repository_url}@${var.ueransim_image_digest}"
  }
}
