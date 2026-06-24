moved {
  from = aws_cloudwatch_log_group.backend
  to   = module.backend_lambda.aws_cloudwatch_log_group.lambda[0]
}

moved {
  from = aws_lambda_function.backend
  to   = module.backend_lambda.aws_lambda_function.this[0]
}

moved {
  from = aws_cloudwatch_log_group.nef_pfd
  to   = module.nef_pfd_lambda.aws_cloudwatch_log_group.lambda[0]
}

moved {
  from = aws_lambda_function.nef_pfd
  to   = module.nef_pfd_lambda.aws_lambda_function.this[0]
}

moved {
  from = aws_cloudwatch_log_group.nef_qos
  to   = module.nef_qos_lambda.aws_cloudwatch_log_group.lambda[0]
}

moved {
  from = aws_lambda_function.nef_qos
  to   = module.nef_qos_lambda.aws_lambda_function.this[0]
}

moved {
  from = aws_cloudwatch_log_group.nef_traffic_influence
  to   = module.nef_traffic_influence_lambda.aws_cloudwatch_log_group.lambda[0]
}

moved {
  from = aws_lambda_function.nef_traffic_influence
  to   = module.nef_traffic_influence_lambda.aws_lambda_function.this[0]
}
