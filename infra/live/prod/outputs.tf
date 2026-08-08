// Published into the GitHub `production` environment by infra-apply.yml.
//
// NOTE: `production` is a protected environment, and a GitHub App installation token
// cannot write variables to one. The publish step surfaces each failure as a named
// warning to reconcile by hand rather than failing the apply. These ids are
// deterministic and stable, so they persist between applies.

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
