# Terraform Rules

## Version And Provider Policy

- Use a modern stable Terraform CLI. Do not downgrade the repository below the current `required_version` constraint.
- Keep provider sources official:
  - AWS provider: `hashicorp/aws`.
  - Archive provider: `hashicorp/archive`.
- Prefer official HashiCorp/AWS-maintained modules and well-established `terraform-aws-modules/*` registry modules when introducing managed AWS building blocks.
- Do not hand-roll AWS service resources when a mature official module exists, unless there is a documented limitation or migration risk. Lambda functions must use `terraform-aws-modules/lambda/aws`; EKS should use `terraform-aws-modules/eks/aws`; VPC/networking should use `terraform-aws-modules/vpc/aws`; S3 buckets should use `terraform-aws-modules/s3-bucket/aws` when the module can express the required behavior.
- Pin module versions explicitly. For production-like infrastructure, use exact module versions after checking the Terraform Registry; for lower-risk development-only modules, a pessimistic minor constraint is acceptable.
- When converting direct resources to module-managed resources, add `moved` blocks for every stateful resource whose address changes and verify `terraform plan` shows moves instead of destroy/create.
- Before adding or upgrading providers/modules, check the Terraform Registry and release notes, then pin versions conservatively with compatible constraints.
- Keep `.terraform.lock.hcl` platform-aware. If Terraform was initialized from Windows and later used from WSL, refresh Linux locks with:

```bash
cd /mnt/d/projects/5GCityVerse/infrastructure/terraform
terraform providers lock -platform=linux_amd64
terraform init
```

## File Organization

Organize Terraform by resource responsibility, not by convenience:

- `providers.tf`: Terraform block, required providers, provider configuration.
- `variables.tf`: input variables only.
- `outputs.tf`: outputs only.
- `main.tf`: shared data sources, locals, and cross-cutting app resources that do not have a better domain file yet.
- `eks.tf`: VPC, networking, EKS cluster, node groups, and EKS IAM.
- `nef-tools.tf`: NEF tool Lambda packaging, functions, logs, and related per-tool configuration.

When adding a new resource group, create a focused file such as `api-gateway.tf`, `frontend-hosting.tf`, `iam.tf`, `observability.tf`, or `bedrock.tf` instead of expanding `main.tf`.

## Resource Design

- Prefer `for_each` for named collections and stable identities. Use `count` only for simple indexed resources where identity churn is acceptable.
- Keep IAM least-privilege and scoped to concrete ARNs where possible.
- Use default tags from the AWS provider and add resource-specific `Name` tags for AWS resources that appear in consoles.
- Keep names derived from `local.name_prefix` unless an AWS service requires a different pattern.
- Keep Lambda runtime, timeout, environment variables, and log groups in sync.
- Do not put secrets in Terraform variables with defaults, outputs, tfvars examples, or logs.

## Validation

Use WSL for Terraform:

```bash
cd /mnt/d/projects/5GCityVerse/infrastructure/terraform
terraform fmt -recursive
terraform validate
terraform plan
```

Only run `apply` or `destroy` when the user explicitly requests a live infrastructure change.
