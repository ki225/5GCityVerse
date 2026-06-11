output "frontend_url" {
  value = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.frontend.id
}

output "frontend_bucket" {
  value = aws_s3_bucket.frontend.bucket
}

output "api_gateway_url" {
  value = aws_apigatewayv2_stage.http_default.invoke_url
}

output "ws_endpoint" {
  value = aws_apigatewayv2_stage.websocket.invoke_url
}
