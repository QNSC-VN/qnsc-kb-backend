// qnsc-kb · develop
//
// Values only. The whole stack lives in ../../modules/stack, so develop and production
// cannot drift structurally — only the numbers below differ. Develop leans on cheap,
// interruptible infrastructure (Fargate Spot, a small database, short log retention);
// production takes the durable settings.
terraform {
  required_version = ">= 1.9"
  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 5.0" }
    cloudflare = { source = "cloudflare/cloudflare", version = "~> 4.0" }
  }

  backend "s3" {
    bucket         = "qnsc-tofu-state"
    key            = "qnsc-kb/develop/terraform.tfstate"
    region         = "ap-southeast-1"
    encrypt        = true
    dynamodb_table = "qnsc-tofu-locks"
  }
}

provider "aws" {
  region = "ap-southeast-1"
  default_tags {
    tags = {
      Project     = "qnsc-kb"
      Environment = "develop"
      ManagedBy   = "opentofu"
    }
  }
}

// Reads TF_VAR_cloudflare_api_token. Pages and DNS resources are count-gated on the
// account id, so this stack applies before Cloudflare exists.
provider "cloudflare" {
  api_token = var.cloudflare_api_token != "" ? var.cloudflare_api_token : null
}

module "stack" {
  source = "../../modules/stack"

  product  = "qnsc-kb"
  env      = "develop"
  env_slug = "develop"
  region   = "ap-southeast-1"

  app_domain = "kb-dev.qnsc.vn"
  api_domain = "kb-api-dev.qnsc.vn"
  web_record = "kb-dev"
  api_record = "kb-api-dev"

  shared_state_key  = "qnsc-kb/shared/terraform.tfstate"
  runtime_state_key = "platform/runtime-dev/terraform.tfstate"
  storage_state_key = "platform/storage-dev/terraform.tfstate"

  // Develop tracks the newest build; production pins the release tag it was cut from.
  image_tag = "latest"

  // Cost-leaning: short retention, and immediate secret deletion so a
  // destroy+redeploy cycle does not trip "secret scheduled for deletion".
  log_retention_days           = 7
  secrets_recovery_window_days = 0

  // ── Ingress via Cloudflare Tunnel, not an ALB ──────────────────────────────
  // cloudflared runs as a sidecar in the api task and dials OUT, so this environment
  // needs no listener rule, no target group and no public IPv4. Both platform runtime
  // stacks have their ALB disabled anyway, so a tunnel is the only ingress available
  // without turning one back on.
  tunnel_enabled = true
  tunnel_id      = var.tunnel_id

  // ── Sizing ─────────────────────────────────────────────────────────────────
  // The API embeds the QUERY on every search, in-process, so it carries the same
  // ~2 GB model the worker does. 512 CPU / 2048 MB is the smallest Fargate
  // combination that holds it; halving the memory does not make search slow, it makes
  // the task OOM on the first question asked.
  //
  // Spot with a floor of ZERO, and autoscaling off. Develop is exercised by CI deploys
  // and occasional manual checks, so paying for a task around the clock buys nothing.
  // The deploy pipeline scales a zero'd service back to 1 and starts a stopped
  // database, so every merge to main wakes this environment on its own.
  //
  // Autoscaling must stay off while the floor is 0: target tracking scales
  // proportionally, so from zero tasks there is no metric to scale out from, and from
  // one idle task it computes one. It would be inert while billing CloudWatch alarms —
  // and a floor of 1 instead would undo the scale-to-zero within minutes.
  api = {
    cpu                = 512
    memory             = 2048
    min_count          = 0
    max_count          = 2
    enable_autoscaling = false
    use_spot           = true
  }

  // Three containers share this task, and container limits are carved OUT of the total:
  // clamav 1024 (its signature database), beat 256, leaving ~2816 for the Celery worker
  // and its model. 1024 CPU / 4096 MB is the smallest combination that fits all three.
  //
  // max_count is 1 and cannot be raised while beat lives here — two beat containers
  // double every scheduled job. The stack module enforces that with a validation.
  worker = {
    cpu                = 1024
    memory             = 4096
    min_count          = 0
    max_count          = 1
    enable_autoscaling = false
    use_spot           = true
  }

  rds = {
    engine_version           = "16" // matches the pgvector/pgvector:pg16 image used in development
    instance_class           = "db.t4g.micro"
    allocated_storage_gb     = 20
    max_allocated_storage_gb = 100
    multi_az                 = false
    deletion_protection      = false // easy teardown in develop
    monitoring_interval      = 0     // Enhanced Monitoring off — saves CloudWatch cost

    // ZERO, which disables automated backups and point-in-time recovery here.
    // Develop holds nothing worth recovering: the migrator rebuilds the schema from
    // migrations on any deploy, and documents are re-ingestible from their sources.
    //
    // Applying a change from a non-zero retention to 0 DELETES every existing automated
    // snapshot for this instance and is not reversible. Take a manual snapshot first if
    // develop ever holds something real.
    backup_retention_days = 0
  }

  // A cache.t4g.micro node, not serverless: serverless has roughly a $90/mo floor.
  // ElastiCache has no stopped state — only delete — so this is the one component of an
  // idled environment that keeps billing, and it stays up because it is the Celery
  // broker rather than a cache that can be missed.
  cache = {
    enabled   = true
    mode      = "node"
    node_type = "cache.t4g.micro"
  }

  // ClamAV runs here too, not only in production. It is not really optional: the
  // application is pinned to ENVIRONMENT=production in every environment (so the
  // validate_production() guardrails are actually exercised), and that function refuses
  // to boot when malware scanning is off.
  malware_scan_enabled = true

  // ── Off-hours idling ───────────────────────────────────────────────────────
  // Two passes, not one. A single nightly stop does not hold, because the deploy
  // pipeline's `ensure_rds` step wakes this environment whenever a deploy lands — so a
  // merge after the stop leaves everything running until the following night. rally
  // measured exactly that: its develop database published CPU datapoints every hour of
  // every night while a stop schedule fired correctly each evening.
  //
  // Midnight ends the working day; 03:00 catches an environment woken by a late deploy.
  //
  // KNOWN CONSEQUENCE, because it is not obvious: scaling the worker to zero stops
  // Celery beat with it, so nothing scheduled runs overnight — no outbox replay, no
  // cloud-connector polling. In develop that is the intended trade. Beat resumes at the
  // wake, and the outbox is a queue rather than a stream, so pending rows are replayed
  // then rather than lost. Production must not take this setting for that reason.
  idle_schedule = "cron(0 0,3 * * ? *)"

  // 08:00 local, every day. Deploys already wake this environment, but that covers the
  // days it is CHANGED rather than the days it is USED — someone opening it on a
  // morning nobody merged would find it stopped, and RDS takes minutes to reach
  // `available`, which reads as an outage rather than something to wait out.
  //
  // 08:00 rather than 09:00 because the database needs those minutes and the API tasks
  // then have to pass a health check, so the environment is serving before the working
  // day rather than during its first minutes.
  wake_schedule = "cron(0 8 * * ? *)"

  // Must match the weights baked into the image. Fixes EMBEDDING_DIMENSION at 1024,
  // which is the pgvector column width and the HNSW index built by migration
  // 20260802_03 — changing it later means a migration and a full re-embed.
  embedding_model   = "BAAI/bge-m3"
  embedding_version = "bge-m3-v1"

  cloudflare_account_id = var.cloudflare_account_id
  microsoft_client_id   = var.microsoft_client_id
  google_client_id      = var.google_client_id
  allowed_email_domains = var.allowed_email_domains
}
