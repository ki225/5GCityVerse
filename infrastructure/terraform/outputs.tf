output "api_gateway_url" {
  description = "HTTP API Gateway invoke URL for the backend Lambda"
  value       = aws_apigatewayv2_stage.http_default.invoke_url
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID for frontend deployments and invalidations"
  value       = aws_cloudfront_distribution.frontend.id
}

output "eks_cluster_endpoint" {
  description = "EKS cluster API endpoint"
  value       = aws_eks_cluster.free5gc.endpoint
}

output "eks_cluster_name" {
  description = "EKS cluster name used by deployment scripts and backend runtime"
  value       = aws_eks_cluster.free5gc.name
}

output "frontend_bucket" {
  description = "S3 bucket name that hosts the frontend build behind CloudFront"
  value       = aws_s3_bucket.frontend.bucket
}

output "frontend_url" {
  description = "CloudFront HTTPS URL for the frontend"
  value       = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "backend_lambda_name" {
  description = "Backend Lambda function name"
  value       = module.backend_lambda.lambda_function_name
}

output "free5gc_webui_url" {
  description = "Private free5GC WebUI NLB URL last synchronized from Kubernetes by the deployment workflow"
  value       = var.free5gc_webui_url
}

output "nef_base_url" {
  description = "Private free5GC NEF NLB URL last synchronized from Kubernetes by the deployment workflow"
  value       = var.nef_base_url
}

output "nef_pfd_lambda_name" {
  description = "Lambda function name for the NEF PFD creation tool"
  value       = module.nef_pfd_lambda.lambda_function_name
}

output "nef_qos_lambda_name" {
  description = "Lambda function name for the NEF QoS subscription tool"
  value       = module.nef_qos_lambda.lambda_function_name
}

output "nef_traffic_influence_lambda_name" {
  description = "Lambda function name for the NEF traffic influence tool"
  value       = module.nef_traffic_influence_lambda.lambda_function_name
}

output "smf_qer_actuator_ecr_repository_url" {
  description = "Private ECR repository for the patched free5GC SMF QER actuator image"
  value       = aws_ecr_repository.smf_qer_actuator.repository_url
}

output "ueransim_ecr_repository_url" {
  description = "ECR repository URL for the reproducible UERANSIM image"
  value       = aws_ecr_repository.ueransim.repository_url
}

output "ws_endpoint" {
  description = "WebSocket API Gateway invoke URL for live frontend updates"
  value       = aws_apigatewayv2_stage.websocket.invoke_url
}

output "multus_n2_subnet_id" {
  description = "Subnet ID for AMF N2 (NGAP) Multus secondary interface"
  value       = aws_subnet.multus_n2.id
}

output "multus_n3_subnet_id" {
  description = "Subnet ID for UPF N3 (GTP-U) Multus secondary interface"
  value       = aws_subnet.multus_n3.id
}

output "multus_n4_subnet_id" {
  description = "Subnet ID for UPF N4 (PFCP) Multus secondary interface"
  value       = aws_subnet.multus_n4.id
}

output "multus_n6_subnet_id" {
  description = "Subnet ID for UPF N6 (Data Network) Multus secondary interface"
  value       = aws_subnet.multus_n6.id
}

output "up_node_group_name" {
  description = "EKS user-plane node group name"
  value       = aws_eks_node_group.free5gc_up.node_group_name
}

output "cp_node_group_name" {
  description = "EKS control-plane node group name"
  value       = aws_eks_node_group.free5gc_cp.node_group_name
}
