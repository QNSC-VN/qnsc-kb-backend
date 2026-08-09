// Consumed by the environment stacks, and published from there into the GitHub
// environments the deploy workflows read (see .github/workflows/infra-apply.yml).

output "ecs_cluster_name" { value = module.ecs_cluster.cluster_name }
output "ecs_api_service" { value = module.api.service_name }
output "ecs_worker_service" { value = module.worker.service_name }
output "ecs_migrator_task_def" { value = module.migrator.family }

output "private_subnet_ids" {
  value       = data.terraform_remote_state.runtime.outputs.private_subnet_ids
  description = "Where the deploy pipeline runs the one-shot migrator task."
}

output "sg_app_id" {
  value       = data.terraform_remote_state.runtime.outputs.sg_app_id
  description = "Security group for the one-shot migrator task."
}

output "rds_instance_id" {
  value       = module.rds.identifier
  description = "Read by the deploy reusable's ensure_rds step to wake a stopped develop database."
}

output "rds_address" { value = module.rds.address }

output "web_pages_project" {
  value       = one(module.web[*].project_name)
  description = "Cloudflare Pages project name — published to the qnsc-kb-frontend repo as PAGES_PROJECT."
}

output "api_url" {
  value       = local.api_base_url
  description = "Published as APP_URL; the deploy pipeline appends the readiness path to it."
}

output "app_url" { value = local.app_base_url }
