output "ecr_repository_urls" { value = module.ecr.repository_urls }
output "ecr_push_role_arn" { value = module.iam_oidc.ecr_push_role_arn }
output "deploy_role_arns" { value = module.iam_oidc.deploy_role_arns }
output "infra_plan_role_arn" { value = module.iam_oidc.infra_plan_role_arn }
output "infra_apply_role_arn" { value = module.iam_oidc.infra_apply_role_arn }

# Platform outputs, re-exported so the environment stacks read one remote state
# (qnsc-kb/shared) instead of reaching into qnsc-infra directly.
output "kms_key_arn" {
  value       = data.terraform_remote_state.platform.outputs.kms_key_arn
  description = "Shared CMK ARN from qnsc-infra — pass to the RDS, cache and secrets modules."
}

output "artifacts_bucket_name" {
  value       = data.terraform_remote_state.platform.outputs.artifacts_bucket_name
  description = "Shared artifacts bucket from qnsc-infra — used by publish-openapi-spec in CI."
}

output "cloudflare_zone_id" {
  value       = try(data.terraform_remote_state.platform.outputs.cloudflare_zone_id, "")
  description = "Cloudflare zone ID for qnsc.vn — environment stacks pass it to the dns-record and pages-web modules."
}

output "cloudflare_ipv4" {
  value       = try(data.terraform_remote_state.platform.outputs.cloudflare_ipv4, [])
  description = "Cloudflare IPv4 ranges from qnsc-infra. Unused while ingress is via Cloudflare Tunnel; needed only if this product ever fronts an ALB."
}
