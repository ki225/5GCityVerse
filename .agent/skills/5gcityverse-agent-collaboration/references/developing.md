# Developing Rules

## Environment

- If the host is Windows, run project commands through WSL unless the task is explicitly Windows-specific.
- Use the WSL project path, for example `/mnt/d/projects/5GCityVerse`, when running shell scripts, Terraform, npm builds, Python tooling, kubectl, Helm, or AWS CLI commands.
- Prefer the repository scripts over ad hoc command sequences:
  - `scripts/deploy.sh` for full cloud deployment.
  - `scripts/start.sh` for already-provisioned runtime startup.
  - `scripts/stop.sh` for stopping Kubernetes runtime without destroying AWS infrastructure.
  - `scripts/destroy.sh` for Terraform-managed teardown.
- Keep `AWS_PROFILE` and `AWS_REGION` explicit. The README default profile is `kiki`; do not bake that into code.

## Local Commands

Run commands from WSL when possible:

```bash
cd /mnt/d/projects/5GCityVerse
chmod +x scripts/*.sh
AWS_PROFILE=<profile> AWS_REGION=ap-northeast-1 ./scripts/start.sh
```

For frontend work, inspect `frontend/package.json` and use the existing package manager lock/config. Do not rely on the checked-in `node_modules.windows-*` directories as source.

For Python backend work, keep runtime compatibility with Lambda Python versions configured in Terraform. Validate syntax or unit-level behavior before suggesting deployment.

## Validation

- Frontend: run the existing build/test/lint command if present in `frontend/package.json`.
- Backend: run targeted Python syntax/tests for changed modules. Avoid live AWS calls unless the user asked for deployment or integration validation.
- Kubernetes: validate YAML shape and object names before applying. Avoid `kubectl apply` unless explicitly requested.
- Scripts: run shell syntax checks when available, then a dry-run or non-mutating command path if the script supports it.

## Editing Discipline

- Do not edit generated dependency folders such as `node_modules.windows-*`.
- Do not create local-only backend or frontend proxy paths unless the requested task is specifically local development.
- Keep logs and generated build artifacts out of committed changes.
