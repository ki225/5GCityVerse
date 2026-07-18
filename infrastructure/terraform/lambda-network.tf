resource "aws_security_group" "lambda_private" {
  name_prefix = "${local.name_prefix}-lambda-private-"
  description = "Private egress for backend and NEF Lambda functions"
  vpc_id      = aws_vpc.eks.id

  tags = {
    Name = "${local.name_prefix}-lambda-private"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# VPC resolver access. The VPC CIDR intentionally includes the Route 53
# resolver address and avoids opening DNS to arbitrary internet destinations.
resource "aws_vpc_security_group_egress_rule" "lambda_dns_udp" {
  security_group_id = aws_security_group.lambda_private.id
  description       = "DNS to the VPC resolver"
  ip_protocol       = "udp"
  from_port         = 53
  to_port           = 53
  cidr_ipv4         = aws_vpc.eks.cidr_block
}

resource "aws_vpc_security_group_egress_rule" "lambda_dns_tcp" {
  security_group_id = aws_security_group.lambda_private.id
  description       = "DNS fallback to the VPC resolver"
  ip_protocol       = "tcp"
  from_port         = 53
  to_port           = 53
  cidr_ipv4         = aws_vpc.eks.cidr_block
}

# AWS APIs (DynamoDB, Secrets Manager, Lambda, API Gateway, Bedrock) currently
# leave through the private subnet NAT. VPC endpoints can narrow this further.
resource "aws_vpc_security_group_egress_rule" "lambda_https" {
  security_group_id = aws_security_group.lambda_private.id
  description       = "TLS to AWS public service endpoints through NAT"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "lambda_nef" {
  security_group_id = aws_security_group.lambda_private.id
  description       = "Private NEF internal NLB"
  ip_protocol       = "tcp"
  from_port         = 8080
  to_port           = 8080
  cidr_ipv4         = aws_vpc.eks.cidr_block
}

# Subscriber/profile evidence is exposed through the private WebUI NLB only.
resource "aws_vpc_security_group_egress_rule" "lambda_webui" {
  security_group_id = aws_security_group.lambda_private.id
  description       = "Private free5GC WebUI subscriber API"
  ip_protocol       = "tcp"
  from_port         = 5000
  to_port           = 5000
  cidr_ipv4         = aws_vpc.eks.cidr_block
}

resource "aws_vpc_security_group_ingress_rule" "eks_api_from_lambda" {
  security_group_id            = aws_eks_cluster.free5gc.vpc_config[0].cluster_security_group_id
  description                  = "Backend Lambda access to the private Kubernetes API"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.lambda_private.id
}
