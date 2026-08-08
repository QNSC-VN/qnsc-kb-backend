// qnsc-kb · production
//
// Values only. The whole stack lives in ../../modules/stack, so production and develop
// cannot drift structurally — only the numbers below differ.
//
// ─────────────────────────────────────────────────────────────────────────────
// THIS ENVIRONMENT IS PRE-LAUNCH IDLE, ON PURPOSE.
//
// It has no users. Both services sit at zero tasks, the cache does not exist, and the
// database is stopped between weekly maintenance windows. That is a deliberate,
// documented posture with an end date — not a half-finished environment — and it is
// what keeps production at roughly $4/mo instead of ~$127 while nobody is using it.
//
// Everything durable is ALREADY on: 30-day backups, deletion protection, a 30-day
// secret recovery window, 90-day logs. Those cost little and are the settings that are
// painful to add after data exists.
//
// What that means for a deploy landing here today: the pipeline builds, promotes,
// migrates and rolls the services, and the services are at desiredCount 0, so no task
// starts. The deploy reports success because it did succeed — there is simply nothing
// serving. Do not read a green prod deploy as "production is up" until go-live.
//
// ── GO-LIVE CHECKLIST ───────────────────────────────────────────────────────
// Flip these together, in one change. Several are coupled and a partial flip leaves a
// worse state than either end:
//
//   1. cache = { enabled = true }              ~10 min, issues a NEW endpoint
//   2. api    = { min_count = 1, enable_autoscaling = true }
//   3. worker = { min_count = 1 }              leave max_count at 1 — beat is a singleton
//   4. remove idle_schedule entirely
//   5. rds.instance_class → db.t4g.small at minimum; re-measure against the real corpus
//      before settling, because the HNSW index is what grows
//
// (1) and (2) are coupled by the stack module's `cache_required_for_jobs` check: the
// cache is the Celery broker, so tasks running without it do no background work at all.
// Enabling the floors without the cache is the state to avoid.
//
// ── BEFORE THE FIRST PRODUCTION DEPLOY: START THE DATABASE BY HAND ──────────
// The shared deploy reusable's `ensure_rds` step — which starts a stopped instance and
// scales zeroed services back up — is gated on `ENVIRONMENT == 'develop'`. It does NOT
// run for production, by design: a deploy that can start a production database is also
// a deploy that hides an accidental stop.
//
// So while `idle_schedule` above leaves this database stopped, a production deploy
// reaches `Run database migrations` and FAILS against an instance that is not running.
// That failure is loud, but its cause is two repositories away from its symptom.
//
//   aws rds start-db-instance --db-instance-identifier qnsc-kb-prod --region ap-southeast-1
//
// takes several minutes to reach `available`. Do it before cutting a release, or remove
// the idle schedule first. The develop deploy role holds `rds:StartDBInstance` for
// exactly this reason; the production one deliberately does not.
// ─────────────────────────────────────────────────────────────────────────────
terraform {
  required_version = ">= 1.9"
  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 5.0" }
    cloudflare = { source = "cloudflare/cloudflare", version = "~> 4.0" }
  }

  backend "s3" {
    bucket         = "qnsc-tofu-state"
    key            = "qnsc-kb/prod/terraform.tfstate"
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
      Environment = "production"
      ManagedBy   = "opentofu"
    }
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token != "" ? var.cloudflare_api_token : null
}

module "stack" {
  source = "../../modules/stack"

  product  = "qnsc-kb"
  env      = "production"
  env_slug = "prod"
  region   = "ap-southeast-1"

  app_domain = "kb.qnsc.vn"
  api_domain = "kb-api.qnsc.vn"
  web_record = "kb"
  api_record = "kb-api"

  shared_state_key  = "qnsc-kb/shared/terraform.tfstate"
  runtime_state_key = "platform/runtime-prod/terraform.tfstate"
  storage_state_key = "platform/storage-prod/terraform.tfstate"

  // Pinned to the release tag that triggered the apply (TF_VAR_image_tag in
  // infra-apply.yml), never "latest". Without the pin an infra apply would quietly
  // reset the task definition to whatever :latest points at — which is a develop build.
  image_tag = var.image_tag

  // 90 days of logs (an audit floor rather than a debugging preference), and a real
  // secret recovery window: unlike develop, a secret deleted here by mistake is not
  // trivially recreated, because its VALUE was never in Terraform.
  log_retention_days           = 90
  secrets_recovery_window_days = 30

  tunnel_enabled = true
  tunnel_id      = var.tunnel_id

  // ── Sizing ─────────────────────────────────────────────────────────────────
  // Larger than develop because production serves real queries, but the FLOORS are 0
  // until go-live — see the checklist above. The numbers below are what one task costs
  // when the floor is raised, not what is running today.
  //
  // The api stays OFF Spot even after go-live: a Spot interruption is a dropped request
  // and a broken response mid-answer. The worker takes Spot deliberately — Celery
  // redelivers an interrupted task, so an interruption costs time rather than work.
  api = {
    cpu                = 1024
    memory             = 4096
    min_count          = 0
    max_count          = 6
    enable_autoscaling = false
    use_spot           = false
    cpu_target_pct     = 60
    memory_target_pct  = 70
  }

  // max_count stays 1 while Celery beat rides in this task — two replicas would double
  // every scheduled job. Splitting beat into its own service is the prerequisite for
  // scaling the worker horizontally, and the stack module's validation enforces it.
  worker = {
    cpu                = 2048
    memory             = 8192
    min_count          = 0
    max_count          = 1
    enable_autoscaling = false
    use_spot           = true
  }

  rds = {
    engine_version = "16"

    // t4g.micro TODAY because the database is empty and stopped. This is the single
    // most likely thing on this page to be wrong at go-live: migration 20260802_03
    // builds an HNSW index, whose construction cost scales with the corpus, and 1 GB of
    // RAM will not hold one for a real document set. Measure against real content and
    // raise it — an under-sized instance shows up as ingestion timeouts, not as an
    // obvious out-of-memory error.
    instance_class           = "db.t4g.micro"
    allocated_storage_gb     = 30
    max_allocated_storage_gb = 500

    // Single-AZ, and that is a decision rather than an oversight: Multi-AZ doubles the
    // instance rate. The exposure is an outage measured in hours during an AZ failure,
    // NOT data loss — provided backup_retention_days stays at 30, because
    // point-in-time recovery is what bounds the loss window to minutes.
    //
    // These two settings are therefore COUPLED: do not lower retention while single-AZ.
    //
    // Not a one-way door — RDS converts single-AZ to Multi-AZ in place: one flag, one
    // apply, a brief failover, no data migration and no endpoint change. Revisit when
    // the product carries an availability commitment.
    multi_az              = false
    backup_retention_days = 30

    deletion_protection = true
    monitoring_interval = 0 // Enhanced Monitoring off until there is load worth profiling
  }

  // NO cache node while idle. ElastiCache has no stopped state — only delete — so it is
  // the one component that would keep billing in an otherwise idle environment.
  //
  // Coupled to the zero floors above by the stack module's `cache_required_for_jobs`
  // check: this cache is the Celery BROKER, so a task running without it performs no
  // ingestion, no connector sync and no outbox replay. Re-enable it in the same change
  // that raises the floors, never after.
  cache = {
    enabled = false
  }

  // Weekly, not nightly. The environment is already at zero tasks with a stopped
  // database, so this is a backstop rather than the main saving: it catches an
  // environment left running by a manual check or a deploy that woke the database.
  //
  // REMOVE THIS AT GO-LIVE. A schedule that stops production every Sunday is exactly
  // the kind of thing that survives a launch by accident.
  idle_schedule = "cron(0 1 ? * SUN *)"

  // No wake_schedule, deliberately: nothing here should start on a timer. Before
  // go-live there is nothing to wake for; after go-live the floors keep it up
  // continuously and a timed start would mask a real outage by papering over it.

  malware_scan_enabled = true

  // Must match the weights baked into the image, and must match develop: a different
  // model writes vectors of a different width that the other environment cannot
  // compare against. Fixed at migration time by the pgvector column and the HNSW index.
  embedding_model   = "BAAI/bge-m3"
  embedding_version = "bge-m3-v1"

  cloudflare_account_id = var.cloudflare_account_id
  microsoft_client_id   = var.microsoft_client_id
  google_client_id      = var.google_client_id
  allowed_email_domains = var.allowed_email_domains
}
