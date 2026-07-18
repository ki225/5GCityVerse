variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "ap-northeast-1"
}

variable "project_name" {
  description = "Project name used as resource prefix"
  type        = string
  default     = "5gcityverse"
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "prod"
}

variable "node_instance_type" {
  description = "EKS worker node EC2 instance type (needs enough CPU for free5GC + iperf3)"
  type        = string
  default     = "t3.large"
}

variable "custom_ami_id" {
  description = "Custom EKS-optimized AMI with gtp5g kernel module pre-installed. Leave empty to use default (requires DaemonSet loader)."
  type        = string
  default     = ""
}

variable "eks_version" {
  description = "EKS Kubernetes control plane version"
  type        = string
  default     = "1.36"
}

variable "ueransim_image_digest" {
  description = "Immutable digest of the reviewed UERANSIM image in the project ECR repository"
  type        = string
  default     = "sha256:58909d22fe2b1d24893fe26eb9502dac1056c85e4135fa87902bf3a1d1eb3e0b"

  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.ueransim_image_digest))
    error_message = "ueransim_image_digest must be a sha256 digest."
  }
}

variable "vpc_cni_addon_version" {
  description = "Pinned EKS VPC CNI add-on version used to enforce Kubernetes NetworkPolicy"
  type        = string
  default     = "v1.21.2-eksbuild.2"
}

variable "eks_public_access_cidrs" {
  description = "Explicit administrator CIDRs allowed to reach the public EKS API; empty keeps it private-only"
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.eks_public_access_cidrs : cidr != "0.0.0.0/0" && can(cidrnetmask(cidr))])
    error_message = "Use valid, explicit administrator CIDRs; 0.0.0.0/0 is prohibited."
  }
}

variable "eks_deployer_principal_arn" {
  description = "Dedicated IAM role/user ARN allowed to perform cluster-scoped installation"
  type        = string
  default     = ""
}

variable "validation_runner_enabled" {
  description = "Create the temporary private SSM runner used for EKS and data-plane validation"
  type        = bool
  default     = false
}

variable "validation_runner_instance_type" {
  description = "EC2 instance type for the temporary private validation runner"
  type        = string
  default     = "t3.micro"
}

variable "eks_node_min_size" {
  description = "Minimum number of EKS worker nodes"
  type        = number
  default     = 1
}

variable "eks_node_desired_size" {
  description = "Desired number of EKS worker nodes (2x t3.large needed: free5GC CP pods require ~3 vCPU total)"
  type        = number
  default     = 2
}

variable "eks_node_max_size" {
  description = "Maximum number of EKS control-plane worker nodes"
  type        = number
  default     = 3
}

variable "up_node_instance_type" {
  description = "EKS user-plane worker node EC2 instance type (runs UPF; benefits from network-optimized instances)"
  type        = string
  default     = "c5n.xlarge"
}

variable "up_node_min_size" {
  description = "Minimum number of EKS user-plane worker nodes"
  type        = number
  default     = 1
}

variable "up_node_desired_size" {
  description = "Desired number of EKS user-plane worker nodes"
  type        = number
  default     = 1
}

variable "up_node_max_size" {
  description = "Maximum number of EKS user-plane worker nodes (UPF HPA may add replicas beyond this)"
  type        = number
  default     = 4
}

variable "free5gc_webui_url" {
  description = "Private free5GC WebUI NLB URL discovered from Kubernetes and used by the VPC-attached backend Lambda"
  type        = string
  default     = ""

  validation {
    condition     = var.free5gc_webui_url == "" || can(regex("^https?://[A-Za-z0-9][A-Za-z0-9.-]*\\.elb\\.[A-Za-z0-9-]+\\.amazonaws\\.com(\\.cn)?(:[0-9]+)?$", var.free5gc_webui_url))
    error_message = "free5gc_webui_url must be empty or an AWS ELB http(s) URL without a path; deployment also verifies Scheme=internal."
  }
}

variable "free5gc_webui_username" {
  description = "free5GC WebUI username"
  type        = string
  default     = "admin"
}

variable "api_auth_enabled" {
  description = "Enable the deployable HTTP/WebSocket shared-token Lambda authorizer"
  type        = bool
  default     = true
}

variable "free5gc_plmn_id" {
  description = "PLMN ID used for generated free5GC subscriber records"
  type        = string
  default     = "20893"
}

variable "prometheus_url" {
  description = "Optional Prometheus HTTP endpoint for real UPF/SMF/AMF metrics. Leave empty to use estimated dashboard values."
  type        = string
  default     = ""
}

variable "nef_base_url" {
  description = "Private free5GC NEF NLB URL used by NEF tool Lambdas; empty keeps all NEF actions fail-closed until the post-Helm reviewed apply"
  type        = string
  default     = ""

  validation {
    condition     = var.nef_base_url == "" || can(regex("^https?://[A-Za-z0-9][A-Za-z0-9.-]*\\.elb\\.[A-Za-z0-9-]+\\.amazonaws\\.com(\\.cn)?(:[0-9]+)?$", var.nef_base_url))
    error_message = "nef_base_url must be empty or an AWS ELB http(s) URL without a path; deployment also verifies Scheme=internal."
  }
}

variable "nef_af_id" {
  description = "Application Function ID used when creating NEF subscriptions"
  type        = string
  default     = "cityverse-af"
}
