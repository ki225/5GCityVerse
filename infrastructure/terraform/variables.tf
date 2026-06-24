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
  default     = "t3.xlarge"
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

variable "eks_node_min_size" {
  description = "Minimum number of EKS worker nodes"
  type        = number
  default     = 1
}

variable "eks_node_desired_size" {
  description = "Desired number of EKS worker nodes"
  type        = number
  default     = 2
}

variable "eks_node_max_size" {
  description = "Maximum number of EKS worker nodes"
  type        = number
  default     = 3
}

variable "free5gc_webui_url" {
  description = "Public free5GC WebUI endpoint used by the demo backend to create subscriber records"
  type        = string
  default     = ""
}

variable "free5gc_webui_username" {
  description = "free5GC WebUI username"
  type        = string
  default     = "admin"
}

variable "free5gc_webui_password" {
  description = "free5GC WebUI password"
  type        = string
  sensitive   = true
  default     = "free5gc"
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
  description = "free5GC NEF base URL used by NEF tool Lambdas. free5GC v4.2.2 exposes NEF PFD/OAM on this in-cluster service; QoS and Traffic Influence are compensated through real subscriber/profile and scenario traffic when unsupported."
  type        = string
  default     = "http://free5gc-free5gc-nef-service.free5gc.svc.cluster.local:8080"
}

variable "nef_af_id" {
  description = "Application Function ID used when creating NEF subscriptions"
  type        = string
  default     = "cityverse-af"
}
