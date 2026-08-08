// Inputs an ENVIRONMENT chooses. Anything derived from these lives in locals in
// main.tf, so develop and prod cannot drift in how a value is assembled — only in
// what they feed in.

variable "product" {
  type    = string
  default = "qnsc-kb"
}

variable "env" {
  type        = string
  description = "Full environment name (develop | production). Used in tags and DEPLOYMENT_ENV."
}

variable "env_slug" {
  type        = string
  description = "Short environment name used in resource names (develop | prod)."
}

variable "region" {
  type    = string
  default = "ap-southeast-1"
}

// ── Remote state keys ────────────────────────────────────────────────────────
variable "shared_state_key" {
  type        = string
  description = "State key of this product's _shared stack (ECR + OIDC + re-exported platform outputs)."
}

variable "runtime_state_key" {
  type        = string
  description = "State key of the platform runtime stack (shared VPC/subnets/SGs). Must be applied first."
}

variable "storage_state_key" {
  type        = string
  description = "State key of the platform storage stack (Cloudflare R2 buckets)."
}

// ── Naming and ingress ───────────────────────────────────────────────────────
variable "app_domain" {
  type        = string
  description = "Public hostname of the SPA, e.g. kb-dev.qnsc.vn."
}

variable "api_domain" {
  type        = string
  description = "Public hostname of the API, e.g. kb-api-dev.qnsc.vn."
}

variable "web_record" {
  type        = string
  description = "DNS record name for the SPA (host part of app_domain)."
}

variable "api_record" {
  type        = string
  description = "DNS record name for the API (host part of api_domain)."
}

variable "image_tag" {
  type        = string
  description = "Container image tag. develop tracks \"latest\"; production pins the release tag."
}

// ── Ingress via Cloudflare Tunnel ────────────────────────────────────────────
variable "tunnel_enabled" {
  type    = bool
  default = true

  description = <<-EOT
    Serve the API through a cloudflared sidecar that dials OUT to Cloudflare, instead
    of an ALB target group.

    Default true, unlike rally's, because there is no shared ALB to fall back on:
    `enable_alb` is false on both platform runtime stacks (since 2026-08-02, after
    rally's own tunnel cutover — an idle load balancer with no target groups is
    $25.70/mo for nothing). Setting this false therefore requires turning that ALB
    back on first.
  EOT
}

variable "tunnel_id" {
  type        = string
  default     = ""
  description = <<-EOT
    Cloudflare Tunnel UUID. Its CNAME target is <tunnel_id>.cfargotunnel.com.

    A tunnel and its connector token are ONE Cloudflare object created out of band —
    Terraform does not mint them. Put the token in the `tunnel-token` secret.
  EOT
}

// ── Sizing ───────────────────────────────────────────────────────────────────
variable "api" {
  type = object({
    cpu                = number
    memory             = number
    min_count          = number
    max_count          = number
    enable_autoscaling = optional(bool, false)
    use_spot           = optional(bool, false)
    cpu_target_pct     = optional(number, 65)
    memory_target_pct  = optional(number, 75)
  })

  description = <<-EOT
    API task sizing. Memory is the load-bearing number here, not CPU: the API answers
    search queries by embedding the query text, and src/lib/embeddings.py loads
    SentenceTransformer(EMBEDDING_MODEL) in-process. bge-m3 needs roughly 2 GB
    resident once loaded, so an under-sized task does not run slowly — it is OOM-killed
    on the first query, long after the deploy reported success.
  EOT

  validation {
    condition     = var.api.enable_autoscaling == false || var.api.min_count >= 1
    error_message = "Autoscaling with min_count = 0 cannot self-heal: nothing publishes a metric at zero tasks, so whatever scaled it down is permanent."
  }
}

variable "worker" {
  type = object({
    cpu                = number
    memory             = number
    min_count          = number
    max_count          = number
    enable_autoscaling = optional(bool, false)
    use_spot           = optional(bool, true)
    cpu_target_pct     = optional(number, 65)
    memory_target_pct  = optional(number, 75)
  })

  description = <<-EOT
    Worker task sizing. This task carries the most: the Celery worker (embedding +
    PaddleOCR extraction), the Celery beat scheduler, and the ClamAV daemon — whose
    signature database alone is ~2 GB resident.

    Container `memory` limits are carved out of the TASK total, never added to it, so
    this figure must cover all three.
  EOT

  validation {
    condition     = var.worker.enable_autoscaling == false || var.worker.min_count >= 1
    error_message = "Autoscaling with min_count = 0 cannot self-heal: nothing publishes a metric at zero tasks, so whatever scaled it down is permanent."
  }

  validation {
    condition     = var.worker.max_count <= 1
    error_message = "Celery beat runs as a container inside the worker task, and beat is a singleton — two replicas double every scheduled job. Split beat into its own service before raising max_count."
  }
}

variable "rds" {
  type = object({
    instance_class           = string
    allocated_storage_gb     = number
    max_allocated_storage_gb = number
    multi_az                 = bool
    deletion_protection      = bool
    backup_retention_days    = number
    monitoring_interval      = optional(number, 0)
    engine_version           = optional(string, "16")
  })

  description = <<-EOT
    Postgres settings. engine_version defaults to "16" rather than the rds module's
    own default, to match the pgvector/pgvector:pg16 image this application is
    developed and tested against.

    Sizing note: migration 20260802_03 builds an HNSW index over the embedding column.
    HNSW construction is memory-hungry and scales with corpus size, so an instance
    class chosen against an empty database will not stay right.
  EOT
}

variable "cache" {
  type = object({
    enabled   = optional(bool, true)
    mode      = optional(string, "node")
    node_type = optional(string, "cache.t4g.micro")
  })
  default = {}

  description = <<-EOT
    Valkey cache. It is the Celery BROKER and result backend, not merely a cache, so
    disabling it stops all background work — ingestion, connector polling, the outbox
    relay — rather than degrading it.

    "node" mode, not "serverless": serverless has roughly a $90/mo floor against
    ~$12/mo for a cache.t4g.micro node.
  EOT
}

// ── Secrets ──────────────────────────────────────────────────────────────────
variable "secrets_recovery_window_days" {
  type        = number
  default     = 0
  description = "0 in develop so a destroy+redeploy cycle does not hit \"secret scheduled for deletion\". Production keeps a real window."
}

variable "secrets_bundle_name" {
  type        = string
  default     = "app"
  description = "Bundle all app secrets into one Secrets Manager container read per key. Secrets Manager bills per SECRET regardless of size."
}

variable "secrets_use_bundle" {
  type    = bool
  default = true
}

// ── Application configuration ────────────────────────────────────────────────
variable "app_db_role" {
  type        = string
  default     = "qnsc_app"
  description = <<-EOT
    The least-privilege role the api and worker connect as. NOT a credential — a role
    name — so it travels as plain env; its password is the `app-db-password` secret.

    Created by scripts/bootstrap_db_role.py from the migrator's entrypoint, because
    migration 20260802_05 GRANTs to this role but never creates it.
  EOT
}

variable "malware_scan_enabled" {
  type    = bool
  default = true

  description = <<-EOT
    Run ClamAV as a sidecar in the worker task and scan uploads.

    Not freely optional: validate_production() in src/core/config.py REFUSES to boot
    when ENVIRONMENT is production and this is false. Turning it off therefore also
    means dropping out of production mode, which turns off every other guardrail in
    that function (API docs stay published, self-registration stays open).
  EOT
}

variable "embedding_model" {
  type        = string
  default     = "BAAI/bge-m3"
  description = <<-EOT
    Must match the model baked into the image (the Dockerfile's EMBEDDING_MODEL build
    arg). A mismatch is not an error: the task downloads the other model on first use,
    which is the multi-minute cold start the bake exists to prevent.

    It also determines EMBEDDING_DIMENSION, which fixes the pgvector column width and
    the HNSW index at migration time. Changing it later requires a migration and a full
    re-embed of every chunk.
  EOT
}

variable "embedding_version" {
  type    = string
  default = "bge-m3-v1"
}

variable "gemini_model" {
  type    = string
  default = "gemini-2.5-flash-lite"
}

variable "microsoft_tenant_id" {
  type        = string
  default     = "common"
  description = "Entra tenant for the Microsoft connector's OAuth flow (connectors only — this application has no SSO login path yet)."
}

variable "microsoft_client_id" {
  type        = string
  default     = ""
  description = "Public identifier, not a secret. Empty leaves the Microsoft connector dormant."
}

variable "google_client_id" {
  type        = string
  default     = ""
  description = "Public identifier, not a secret. Empty leaves the Google connector dormant."
}

variable "allowed_email_domains" {
  type        = list(string)
  default     = []
  description = "Comma-joined into ALLOWED_EMAIL_DOMAINS. Empty accepts any domain."
}

// ── Platform / misc ──────────────────────────────────────────────────────────
variable "cloudflare_account_id" {
  type        = string
  default     = ""
  description = "Cloudflare account that owns the Pages project. Empty skips the SPA module entirely."
}

variable "log_retention_days" {
  type    = number
  default = 7
}

variable "container_insights" {
  type        = string
  default     = "disabled"
  description = "Stated, never inherited: the ecs-cluster module defaults to \"enhanced\", whose per-task metrics bill as custom CloudWatch metrics."
}
