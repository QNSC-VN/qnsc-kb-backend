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

  // ── Sizing ─────────────────────────────────────────────────────────────────
  // 2048 MB, sized from MEASUREMENT on EMBEDDING_RUNTIME=onnx (PR #51), not arithmetic:
  // the task held a steady 1,569 MB (38.3% of the 4096 it was given) across the whole
  // day it ran after the flip — the fp32 ONNX session resident, torch NOT loaded — and
  // a search request adds ~460 ms of one short query through it. 2048 leaves ~30%
  // headroom over that floor.
  //
  // The 4096 before this was the torch floor: the model plus the framework, sized when
  // a 1024 task failed the load and silently fell back to keyword-only search. ONNX is
  // what bought the headroom back — if this ever needs raising again, raise it on a
  // CloudWatch MemoryUtilization graph, not on a hunch, and remember 512 CPU caps a
  // task at 4096 MB.
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

  // Three containers share this task. Two carry hard container limits carved out of the
  // task total — beat 256, and clamav 2048 (its signature database is ~2 GB resident;
  // see the sizing post-mortem in the module) — so 2,304 MB is reserved against the
  // task and the Celery worker itself runs unlimited, bounded only by the task level.
  //
  // 4096, down from 6144 on the same ONNX evidence. Measured idle on the new task:
  // 1,278 MB total (clamav + beat + worker base, model not yet loaded). The embedding
  // session loads lazily on the first chunk and adds ~1.5 GB (the api holds the
  // identical session at 1,569 MB total), so ~2.8 GB steady while embedding, and
  // PaddleOCR is invoked per scanned file on top of that. 4096 leaves ~1.2 GB for the
  // OCR spike. 1024 CPU because 512 caps a task at 4096 MB — this is now the floor,
  // not the ceiling, and the first large scanned-file ingest is the thing to watch: if
  // it dies, raise this on the evidence of the killed task.
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

  // ── Shared develop cache ─────────────────────────────────────────────────────
  // The ONE Valkey node in the runtime layer, not a node of qnsc-kb's own.
  //
  // ElastiCache has no stopped state — only delete — so this was the one component of an
  // idled environment that kept billing all 730 hours of the month, while the schedule
  // above now runs develop 55 hours a week. It could never be turned off either, because
  // it is the Celery broker rather than a cache that can be missed. rally-develop had the
  // same node for the same reason: two at $15.45 each.
  //
  // Saves $15.45/mo across the account. Node created in QNSC-VN/qnsc-infra#69, and rally
  // moved onto it in QNSC-VN/rally#448.
  //
  // DATABASE 1. rally holds 0. This is a Valkey database index, not a key prefix — a
  // prefix has to be honoured by every library touching the connection, while an index is
  // enforced by the server. Cluster mode is disabled on the shared node, so all 16
  // databases exist and SELECT works. Indexes are allocated centrally in the stack
  // variable's description; two products silently sharing one is the collision that
  // allocation prevents, and nothing catches it at plan time.
  //
  // CELERY IS WHY THE EVICTION POLICY MATTERS, and this is the product that carries the
  // risk. Broker keys have no TTL, so evicting one loses a QUEUED TASK rather than missing
  // a cache. The shared node runs the default `volatile-lru`, which evicts only keys that
  // HAVE a TTL — rally's rate-limit counters and denylist entries go first, and Celery's
  // queue is never a candidate. If anyone sets `allkeys-lru` on that node to improve
  // rally's hit rate, this product silently starts dropping background work.
  //
  // `mode` is deliberately NOT set here. It sizes a node this stack no longer creates —
  // the shared node's mode is decided in qnsc-infra's runtime layer — so passing it would
  // read as configuration and change nothing. Same for `node_type`.
  //
  // APPLIED 2026-08-17. qnsc-kb-develop-cache was destroyed and the endpoint changed, so
  // the cutover was a task-definition revision plus a rolling deploy, and in-flight Celery
  // tasks on the old node were lost — acceptable in develop, where the outbox replays and
  // connectors re-poll. Verified afterwards: /health/ready returned
  // {"database":"ok","redis":"ok","job_mode":"celery"} and beat resumed scheduling on the
  // new node. Production, when it exists, keeps its own node.
  cache = {
    enabled  = true
    shared   = true
    db_index = 1
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
  // THREE passes now, and 19:00 is the change: it ends the working day. 22:00 catches an
  // evening deploy, 02:00 a late one. Was `0,3`.
  //
  // Develop was up 08:00-00:00, so five of those sixteen hours were after everyone had
  // stopped. Measured across both develop environments (rally and qnsc-kb), that
  // 19:00-00:00 tail is ~$8.13/mo of RDS and Fargate.
  //
  // THE LATE PASSES ARE NOT OPTIONAL. A deploy at 20:00 wakes develop; with nothing after
  // 19:00 it would stay up until the NEXT working day's stop — 23 hours, worse than the
  // schedule this replaces. Each pass is a no-op when develop is already down
  // (InvalidDBInstanceState, deliberately not retried).
  idle_schedule = "cron(0 2,19,22 * * ? *)"

  // 08:00 local, every day. Deploys already wake this environment, but that covers the
  // days it is CHANGED rather than the days it is USED — someone opening it on a
  // morning nobody merged would find it stopped, and RDS takes minutes to reach
  // `available`, which reads as an outage rather than something to wait out.
  //
  // 08:00 rather than 09:00 because the database needs those minutes and the API tasks
  // then have to pass a health check, so the environment is serving before the working
  // day rather than during its first minutes.
  // NO `wake_schedule`, deliberately. qnsc-kb develop is ON DEMAND: nothing on a timer
  // brings it up, and the idle passes above keep putting it back down.
  //
  // This is the same shape production runs — idle without wake — and the stack module's
  // validation allows it explicitly ("idle without wake is fine — that is production
  // today"). The reverse, wake without idle, is what it forbids.
  //
  // WHAT WAKES IT: a deploy, automatically. The `wake` job in qnsc-ci's backend-deploy
  // reusable runs `ensure-environment-awake` before the build lands — it starts the RDS
  // instance and scales api and worker back up. So working on qnsc-kb costs a few minutes
  // waiting on the first deploy of the day, not a manual step and not a support request.
  // `aws rds start-db-instance` by hand works too if you want it warm before you push.
  //
  // WHY THIS AND NOT `tofu destroy`. Destroying the stack would save the last $3.96 as
  // well, and it is the wrong trade: `secrets_recovery_window_days = 0` above means a
  // destroy deletes all 12 secrets IMMEDIATELY with no recovery window, so every rebuild
  // means re-pasting 12 values by hand and losing the dev database. On-demand is worth
  // having; irrecoverable is not.
  //
  // WHAT IT SAVES, and what it does not. Only this product's own hours: RDS instance time
  // (~$5.97/mo at a weekday schedule) and Fargate (~$5.80). The $2.76 of gp3 storage and
  // $1.20 of secrets bill whether the instance runs or not, so ~$3.96/mo is the floor
  // while the environment exists at all.
  //
  // It saves NOTHING on the shared dev platform, and that is worth stating so nobody
  // expects it to: the shared Valkey node ($15.45) cannot be stopped — ElastiCache has no
  // stopped state — and the shared NAT instance ($3.86) must stay up for rally develop,
  // which still wakes every weekday. Those are properties of the runtime layer, not of
  // this stack.
  //
  // RESTORE IT by putting the line back, if qnsc-kb returns to daily active development:
  //   wake_schedule = "cron(0 8 ? * MON-FRI *)"

  // Hosted. Fixes EMBEDDING_DIMENSION at 768, which is the pgvector column width and the
  // HNSW index built by migration 20260802_03 — changing it later means a migration and
  // re-embedding every chunk, because a query and a chunk embedded by different models
  // are points in unrelated spaces.
  embedding_model   = "BAAI/bge-m3"
  embedding_version = "bge-m3-v1"
  // Parity-gated ONNX flip (cosine 1.000000 vs torch, tests/unit/test_embedding_backends.py).
  // Rollback until the ml group leaves the images: set back to "torch" and redeploy.
  embedding_runtime = "onnx"

  alarm_emails          = var.alarm_emails
  cloudflare_account_id = var.cloudflare_account_id
  microsoft_client_id   = var.microsoft_client_id
  microsoft_tenant_id   = var.microsoft_tenant_id
  google_client_id      = var.google_client_id
  allowed_email_domains = var.allowed_email_domains
  entra_admin_emails    = var.entra_admin_emails
}
