// Published into the GitHub `develop` environment by .github/workflows/infra-apply.yml,
// because the deploy workflows read exactly these. Sourced from Terraform outputs rather
// than typed by hand, so a rebuild that changes a subnet or SG id cannot leave the
// deploy pointing at a resource that no longer exists.

output "ecs_cluster_name" { value = module.stack.ecs_cluster_name }
output "ecs_api_service" { value = module.stack.ecs_api_service }
output "ecs_worker_service" { value = module.stack.ecs_worker_service }
output "ecs_migrator_task_def" { value = module.stack.ecs_migrator_task_def }
output "private_subnet_ids" { value = module.stack.private_subnet_ids }
output "sg_app_id" { value = module.stack.sg_app_id }
output "rds_instance_id" { value = module.stack.rds_instance_id }
output "web_pages_project" { value = module.stack.web_pages_project }
output "api_url" { value = module.stack.api_url }
output "app_url" { value = module.stack.app_url }
