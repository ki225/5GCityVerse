data "aws_ssm_parameter" "validation_runner_al2023_ami" {
  count = var.validation_runner_enabled ? 1 : 0

  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_iam_policy_document" "validation_runner_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "validation_runner_eks" {
  count = var.validation_runner_enabled ? 1 : 0

  statement {
    actions   = ["eks:DescribeCluster"]
    resources = [aws_eks_cluster.free5gc.arn]
  }

  statement {
    actions   = ["s3:GetObject"]
    resources = [aws_s3_object.validation_runner_kubectl[0].arn]
  }

  statement {
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.frontend.arn]
  }
}

data "aws_prefix_list" "validation_runner_s3" {
  count = var.validation_runner_enabled ? 1 : 0

  name = "com.amazonaws.${var.aws_region}.s3"
}

resource "aws_iam_role" "validation_runner" {
  count = var.validation_runner_enabled ? 1 : 0

  name               = "${local.name_prefix}-validation-runner"
  assume_role_policy = data.aws_iam_policy_document.validation_runner_assume_role.json
}

resource "aws_iam_role_policy" "validation_runner_eks" {
  count = var.validation_runner_enabled ? 1 : 0

  name   = "${local.name_prefix}-validation-runner-eks"
  role   = aws_iam_role.validation_runner[0].id
  policy = data.aws_iam_policy_document.validation_runner_eks[0].json
}

resource "aws_iam_role_policy_attachment" "validation_runner_ssm" {
  count = var.validation_runner_enabled ? 1 : 0

  role       = aws_iam_role.validation_runner[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "validation_runner" {
  count = var.validation_runner_enabled ? 1 : 0

  name = "${local.name_prefix}-validation-runner"
  role = aws_iam_role.validation_runner[0].name
}

resource "aws_security_group" "validation_runner" {
  count = var.validation_runner_enabled ? 1 : 0

  name_prefix            = "${local.name_prefix}-validation-runner-"
  description            = "Egress-only temporary EKS validation runner"
  vpc_id                 = aws_vpc.eks.id
  revoke_rules_on_delete = true

  tags = {
    Name      = "${local.name_prefix}-validation-runner"
    Temporary = "true"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "validation_runner_endpoints" {
  count = var.validation_runner_enabled ? 1 : 0

  name_prefix            = "${local.name_prefix}-validation-endpoints-"
  description            = "Private AWS API endpoints for the temporary validation runner"
  vpc_id                 = aws_vpc.eks.id
  revoke_rules_on_delete = true

  tags = {
    Name      = "${local.name_prefix}-validation-endpoints"
    Temporary = "true"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "validation_runner_dns_udp" {
  count = var.validation_runner_enabled ? 1 : 0

  security_group_id = aws_security_group.validation_runner[0].id
  description       = "DNS to the VPC resolver"
  ip_protocol       = "udp"
  from_port         = 53
  to_port           = 53
  cidr_ipv4         = aws_vpc.eks.cidr_block
}

resource "aws_vpc_security_group_egress_rule" "validation_runner_dns_tcp" {
  count = var.validation_runner_enabled ? 1 : 0

  security_group_id = aws_security_group.validation_runner[0].id
  description       = "DNS fallback to the VPC resolver"
  ip_protocol       = "tcp"
  from_port         = 53
  to_port           = 53
  cidr_ipv4         = aws_vpc.eks.cidr_block
}

resource "aws_vpc_security_group_egress_rule" "validation_runner_interface_endpoints" {
  count = var.validation_runner_enabled ? 1 : 0

  security_group_id            = aws_security_group.validation_runner[0].id
  description                  = "TLS only to private AWS API endpoints"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.validation_runner_endpoints[0].id
}

resource "aws_vpc_security_group_egress_rule" "validation_runner_s3" {
  count = var.validation_runner_enabled ? 1 : 0

  security_group_id = aws_security_group.validation_runner[0].id
  description       = "TLS only to the regional S3 prefix list"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = data.aws_prefix_list.validation_runner_s3[0].id
}

resource "aws_vpc_security_group_egress_rule" "validation_runner_eks_api" {
  count = var.validation_runner_enabled ? 1 : 0

  security_group_id            = aws_security_group.validation_runner[0].id
  description                  = "TLS only to the private Kubernetes API"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_eks_cluster.free5gc.vpc_config[0].cluster_security_group_id
}

resource "aws_vpc_security_group_ingress_rule" "validation_endpoints_from_runner" {
  count = var.validation_runner_enabled ? 1 : 0

  security_group_id            = aws_security_group.validation_runner_endpoints[0].id
  description                  = "TLS from the temporary validation runner"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.validation_runner[0].id
}

resource "aws_vpc_endpoint" "validation_runner_interface" {
  for_each = var.validation_runner_enabled ? toset(["ec2messages", "eks", "ssm", "ssmmessages"]) : toset([])

  vpc_id              = aws_vpc.eks.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = aws_subnet.eks_private[*].id
  security_group_ids  = [aws_security_group.validation_runner_endpoints[0].id]

  tags = {
    Name      = "${local.name_prefix}-validation-${each.value}"
    Temporary = "true"
  }
}

resource "aws_vpc_endpoint" "validation_runner_s3" {
  count = var.validation_runner_enabled ? 1 : 0

  vpc_id            = aws_vpc.eks.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.eks_private.id]

  tags = {
    Name      = "${local.name_prefix}-validation-s3"
    Temporary = "true"
  }
}

resource "aws_s3_object" "validation_runner_kubectl" {
  count = var.validation_runner_enabled ? 1 : 0

  bucket       = aws_s3_bucket.frontend.id
  key          = "validation-tools/kubectl-v1.36.2-linux-amd64"
  source       = abspath("${path.module}/../../.tools/kubectl-linux-1.36.2/kubectl")
  source_hash  = filemd5(abspath("${path.module}/../../.tools/kubectl-linux-1.36.2/kubectl"))
  content_type = "application/octet-stream"

  tags = {
    Temporary = "true"
  }
}

resource "aws_vpc_security_group_ingress_rule" "eks_api_from_validation_runner" {
  count = var.validation_runner_enabled ? 1 : 0

  security_group_id            = aws_eks_cluster.free5gc.vpc_config[0].cluster_security_group_id
  description                  = "Temporary private validation runner access to Kubernetes API"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.validation_runner[0].id
}

resource "aws_eks_access_entry" "validation_runner" {
  count = var.validation_runner_enabled ? 1 : 0

  cluster_name  = aws_eks_cluster.free5gc.name
  principal_arn = aws_iam_role.validation_runner[0].arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "validation_runner_cluster_admin" {
  count = var.validation_runner_enabled ? 1 : 0

  cluster_name  = aws_eks_cluster.free5gc.name
  principal_arn = aws_iam_role.validation_runner[0].arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.validation_runner]
}

resource "aws_instance" "validation_runner" {
  count = var.validation_runner_enabled ? 1 : 0

  ami                         = data.aws_ssm_parameter.validation_runner_al2023_ami[0].value
  ebs_optimized               = true
  instance_type               = var.validation_runner_instance_type
  subnet_id                   = aws_subnet.eks_private[0].id
  associate_public_ip_address = false
  iam_instance_profile        = aws_iam_instance_profile.validation_runner[0].name
  vpc_security_group_ids      = [aws_security_group.validation_runner[0].id]
  user_data_replace_on_change = true
  monitoring                  = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    delete_on_termination = true
    encrypted             = true
    volume_size           = 16
    volume_type           = "gp3"
  }

  user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail
    aws s3 cp \
      s3://${aws_s3_bucket.frontend.id}/${aws_s3_object.validation_runner_kubectl[0].key} \
      /usr/local/bin/kubectl
    echo '1e9045ec32bea85da43de85f0065358529ea7c7a152eca78154fba5b58c27d82  /usr/local/bin/kubectl' | sha256sum --check
    chmod 0755 /usr/local/bin/kubectl
    aws eks update-kubeconfig --region ${var.aws_region} --name ${aws_eks_cluster.free5gc.name}
  EOT

  tags = {
    Name      = "${local.name_prefix}-validation-runner"
    Temporary = "true"
  }

  depends_on = [
    aws_eks_access_policy_association.validation_runner_cluster_admin,
    aws_iam_role_policy.validation_runner_eks,
    aws_iam_role_policy_attachment.validation_runner_ssm,
    aws_vpc_endpoint.validation_runner_interface,
    aws_vpc_endpoint.validation_runner_s3,
  ]
}
