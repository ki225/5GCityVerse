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

output "ws_endpoint" {
  description = "WebSocket API Gateway invoke URL for live frontend updates"
  value       = aws_apigatewayv2_stage.websocket.invoke_url
}
