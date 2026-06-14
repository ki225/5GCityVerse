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
  default     = "http://adf69506a54e24c9ab3bbc31c1d42a2d-983101909.ap-northeast-1.elb.amazonaws.com:5000"
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
