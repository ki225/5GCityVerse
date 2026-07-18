data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  eks_cluster_name = "${local.name_prefix}-eks"
  eks_vpc_cidr     = "10.60.0.0/16"
  # 3GPP 5G Core reference point subnets (TS 23.501)
  multus_n2_cidr = "10.60.12.0/24" # NGAP  (N2): AMF  ↔ gNB
  multus_n3_cidr = "10.60.10.0/24" # GTP-U (N3): gNB  ↔ UPF
  multus_n4_cidr = "10.60.11.0/24" # PFCP  (N4): SMF  ↔ UPF
  multus_n6_cidr = "10.60.13.0/24" # IP    (N6): UPF  ↔ Data Network
}

resource "aws_vpc" "eks" {
  cidr_block           = local.eks_vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${local.name_prefix}-eks-vpc"
  }
}

resource "aws_internet_gateway" "eks" {
  vpc_id = aws_vpc.eks.id

  tags = {
    Name = "${local.name_prefix}-eks-igw"
  }
}

resource "aws_subnet" "eks_public" {
  count = 2

  vpc_id            = aws_vpc.eks.id
  availability_zone = data.aws_availability_zones.available.names[count.index]
  cidr_block        = cidrsubnet(aws_vpc.eks.cidr_block, 8, count.index)
  # Internet-facing load balancers receive managed public addresses; arbitrary
  # instances launched in this subnet must not receive one automatically.
  map_public_ip_on_launch = false

  tags = {
    Name                                              = "${local.name_prefix}-eks-public-${count.index + 1}"
    "kubernetes.io/cluster/${local.eks_cluster_name}" = "shared"
    "kubernetes.io/role/elb"                          = "1"
  }
}

# ─── Reserved secondary subnets (3GPP 5G reference points) ──────────────────
# These subnets document the intended N2/N3/N4/N6 address plan. The current
# EKS managed-node deployment does not attach a dedicated unmanaged eth1 to
# worker nodes, so free5GC chart-level Multus is disabled by default.
#
# To enable chart-level Multus later, add node bootstrap/Terraform automation
# that creates and verifies a stable host interface before Helm installs
# AMF/UPF with ipvlan NADs.
#
# N2: NGAP/SCTP between gNB and AMF  (control-plane NF, CP NodeGroup)
# N3: GTP-U between gNB and UPF      (user-plane NF, UP NodeGroup)
# N4: PFCP between SMF and UPF       (user-plane NF, UP NodeGroup)
# N6: IP to Data Network / internet  (user-plane NF, UP NodeGroup)
resource "aws_subnet" "multus_n2" {
  vpc_id            = aws_vpc.eks.id
  availability_zone = data.aws_availability_zones.available.names[0]
  cidr_block        = local.multus_n2_cidr

  tags = {
    Name = "${local.name_prefix}-multus-n2"
  }
}

resource "aws_subnet" "multus_n3" {
  vpc_id            = aws_vpc.eks.id
  availability_zone = data.aws_availability_zones.available.names[0]
  cidr_block        = local.multus_n3_cidr

  tags = {
    Name = "${local.name_prefix}-multus-n3"
  }
}

resource "aws_subnet" "multus_n4" {
  vpc_id            = aws_vpc.eks.id
  availability_zone = data.aws_availability_zones.available.names[0]
  cidr_block        = local.multus_n4_cidr

  tags = {
    Name = "${local.name_prefix}-multus-n4"
  }
}

resource "aws_subnet" "multus_n6" {
  vpc_id            = aws_vpc.eks.id
  availability_zone = data.aws_availability_zones.available.names[0]
  cidr_block        = local.multus_n6_cidr

  tags = {
    Name = "${local.name_prefix}-multus-n6"
  }
}

resource "aws_route_table_association" "multus_n2" {
  subnet_id      = aws_subnet.multus_n2.id
  route_table_id = aws_route_table.eks_private.id
}

resource "aws_route_table_association" "multus_n3" {
  subnet_id      = aws_subnet.multus_n3.id
  route_table_id = aws_route_table.eks_private.id
}

resource "aws_route_table_association" "multus_n4" {
  subnet_id      = aws_subnet.multus_n4.id
  route_table_id = aws_route_table.eks_private.id
}

resource "aws_route_table_association" "multus_n6" {
  subnet_id      = aws_subnet.multus_n6.id
  route_table_id = aws_route_table.eks_private.id
}
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_route_table" "eks_public" {
  vpc_id = aws_vpc.eks.id

  tags = {
    Name = "${local.name_prefix}-eks-public-rt"
  }
}

resource "aws_route" "eks_public_internet" {
  route_table_id         = aws_route_table.eks_public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.eks.id
}

resource "aws_route_table_association" "eks_public" {
  count = length(aws_subnet.eks_public)

  subnet_id      = aws_subnet.eks_public[count.index].id
  route_table_id = aws_route_table.eks_public.id
}

data "aws_iam_policy_document" "eks_cluster_assume_role" {
  statement {
    actions = [
      "sts:AssumeRole",
      "sts:TagSession",
    ]

    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eks_cluster" {
  name               = "${local.eks_cluster_name}-cluster-role"
  assume_role_policy = data.aws_iam_policy_document.eks_cluster_assume_role.json
}

resource "aws_iam_role_policy_attachment" "eks_cluster_policy" {
  role       = aws_iam_role.eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_kms_key" "eks_secrets" {
  description             = "Envelope encryption for ${local.eks_cluster_name} Kubernetes secrets"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = { Name = "${local.eks_cluster_name}-secrets" }
}

resource "aws_kms_alias" "eks_secrets" {
  name          = "alias/${local.eks_cluster_name}-secrets"
  target_key_id = aws_kms_key.eks_secrets.key_id
}

resource "aws_eks_cluster" "free5gc" {
  name     = local.eks_cluster_name
  role_arn = aws_iam_role.eks_cluster.arn
  version  = var.eks_version

  encryption_config {
    provider {
      key_arn = aws_kms_key.eks_secrets.arn
    }
    resources = ["secrets"]
  }

  access_config {
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = false
  }

  vpc_config {
    endpoint_private_access = true
    endpoint_public_access  = length(var.eks_public_access_cidrs) > 0
    # EKS retains the last CIDR list while the public endpoint is disabled.
    # Omitting this inactive field avoids perpetual provider drift without
    # reopening the public endpoint.
    public_access_cidrs = length(var.eks_public_access_cidrs) > 0 ? var.eks_public_access_cidrs : null
    subnet_ids          = concat(aws_subnet.eks_public[*].id, aws_subnet.eks_private[*].id)
  }

  depends_on = [
    aws_iam_role_policy_attachment.eks_cluster_policy,
  ]
}

data "aws_iam_policy_document" "eks_node_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eks_node" {
  name               = "${local.eks_cluster_name}-node-role"
  assume_role_policy = data.aws_iam_policy_document.eks_node_assume_role.json
}

resource "aws_iam_role_policy_attachment" "eks_node_worker" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "eks_node_cni" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "eks_node_ecr" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_eks_node_group" "free5gc_cp" {
  cluster_name    = aws_eks_cluster.free5gc.name
  node_group_name = "${local.name_prefix}-free5gc-cp"
  node_role_arn   = aws_iam_role.eks_node.arn
  subnet_ids      = aws_subnet.eks_private[*].id
  version         = aws_eks_cluster.free5gc.version

  ami_type       = "AL2023_x86_64_STANDARD"
  capacity_type  = "ON_DEMAND"
  disk_size      = 50
  instance_types = [var.node_instance_type]

  scaling_config {
    desired_size = var.eks_node_desired_size
    max_size     = var.eks_node_max_size
    min_size     = var.eks_node_min_size
  }

  update_config {
    max_unavailable = 1
  }

  labels = {
    workload = "free5gc"
    plane    = "control-plane"
  }

  depends_on = [
    aws_iam_role_policy_attachment.eks_node_worker,
    aws_iam_role_policy_attachment.eks_node_cni,
    aws_iam_role_policy_attachment.eks_node_ecr,
  ]
}

# ─── User Plane NodeGroup ─────────────────────────────────────────────────────
# Dedicated nodes for UPF and gtp5g. Chart-level Multus remains disabled until
# dedicated host network interfaces are automated and verified.
# Tainted so only UPF pods (with matching toleration) schedule here.
resource "aws_eks_node_group" "free5gc_up" {
  cluster_name    = aws_eks_cluster.free5gc.name
  node_group_name = "${local.name_prefix}-free5gc-up"
  node_role_arn   = aws_iam_role.eks_node.arn
  subnet_ids      = aws_subnet.eks_private[*].id
  version         = aws_eks_cluster.free5gc.version

  ami_type = "AL2023_x86_64_STANDARD"
  # PFCP sessions and kernel QER state are node-local. Spot interruption was
  # observed repeatedly in live validation and tore down every dedicated UPF,
  # so the single stateful user-plane node must use interruption-free capacity.
  capacity_type  = "ON_DEMAND"
  disk_size      = 50
  instance_types = [var.up_node_instance_type]

  scaling_config {
    desired_size = var.up_node_desired_size
    max_size     = var.up_node_max_size
    min_size     = var.up_node_min_size
  }

  update_config {
    max_unavailable = 1
  }

  labels = {
    workload = "free5gc"
    plane    = "user-plane"
  }

  taint {
    key    = "plane"
    value  = "user-plane"
    effect = "NO_SCHEDULE"
  }

  depends_on = [
    aws_iam_role_policy_attachment.eks_node_worker,
    aws_iam_role_policy_attachment.eks_node_cni,
    aws_iam_role_policy_attachment.eks_node_ecr,
  ]
}
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_eks_access_entry" "lambda" {
  cluster_name  = aws_eks_cluster.free5gc.name
  principal_arn = aws_iam_role.lambda.arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "lambda_namespace_edit" {
  cluster_name  = aws_eks_cluster.free5gc.name
  principal_arn = aws_iam_role.lambda.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"

  access_scope {
    type       = "namespace"
    namespaces = ["free5gc"]
  }

  depends_on = [aws_eks_access_entry.lambda]
}

# NetworkPolicy objects are not enforcement by themselves. Enable the VPC CNI
# policy agent explicitly and pin the EKS-compatible build queried for 1.36.
resource "aws_eks_addon" "vpc_cni" {
  cluster_name                = aws_eks_cluster.free5gc.name
  addon_name                  = "vpc-cni"
  addon_version               = var.vpc_cni_addon_version
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"
  configuration_values = jsonencode({
    enableNetworkPolicy = "true"
  })
}

# Public subnets are retained only for internet-facing teaching UI load
# balancers. EKS nodes and 5GC workloads run in these private subnets.
resource "aws_subnet" "eks_private" {
  count = 2

  vpc_id                  = aws_vpc.eks.id
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  cidr_block              = cidrsubnet(aws_vpc.eks.cidr_block, 8, count.index + 20)
  map_public_ip_on_launch = false

  tags = {
    Name                                              = "${local.name_prefix}-eks-private-${count.index + 1}"
    "kubernetes.io/cluster/${local.eks_cluster_name}" = "shared"
    "kubernetes.io/role/internal-elb"                 = "1"
  }
}

resource "aws_eip" "eks_nat" {
  domain = "vpc"
  tags   = { Name = "${local.name_prefix}-eks-nat" }
}

resource "aws_nat_gateway" "eks" {
  allocation_id = aws_eip.eks_nat.id
  subnet_id     = aws_subnet.eks_public[0].id

  depends_on = [aws_internet_gateway.eks]
  tags       = { Name = "${local.name_prefix}-eks-nat" }
}

resource "aws_route_table" "eks_private" {
  vpc_id = aws_vpc.eks.id
  tags   = { Name = "${local.name_prefix}-eks-private-rt" }
}

resource "aws_route" "eks_private_internet" {
  route_table_id         = aws_route_table.eks_private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.eks.id
}

resource "aws_route_table_association" "eks_private" {
  count = length(aws_subnet.eks_private)

  subnet_id      = aws_subnet.eks_private[count.index].id
  route_table_id = aws_route_table.eks_private.id
}

resource "aws_eks_access_entry" "deployer" {
  count = var.eks_deployer_principal_arn == "" ? 0 : 1

  cluster_name  = aws_eks_cluster.free5gc.name
  principal_arn = var.eks_deployer_principal_arn
  type          = "STANDARD"
}

# Cluster scope is isolated to a dedicated deployment identity because the
# installer creates CRDs, Multus and metrics-server resources.
resource "aws_eks_access_policy_association" "deployer_cluster_admin" {
  count = var.eks_deployer_principal_arn == "" ? 0 : 1

  cluster_name  = aws_eks_cluster.free5gc.name
  principal_arn = var.eks_deployer_principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.deployer]
}
