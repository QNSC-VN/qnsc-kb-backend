// PUBLIC identifiers live here, in git, for the same reason as in live/develop: the
// infra-plan job has no `environment:` context, so an environment-scoped Actions
// variable resolves to "" during plan and the plan lies about count-gated resources.
//
// Only the Cloudflare API token and the release image tag come from CI.

variable "cloudflare_api_token" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Cloudflare API token with Pages + DNS edit scope, from TF_VAR_cloudflare_api_token."
}

variable "image_tag" {
  type    = string
  default = "latest"

  description = <<-EOT
    Container image tag deployed to production.

    infra-apply.yml passes TF_VAR_image_tag = the release tag that triggered the apply,
    so the value below is only a fallback for a local plan. Production should never
    actually run "latest": that tag follows develop, so an apply that used it would
    quietly move production onto an untested build.
  EOT
}

variable "cloudflare_account_id" {
  type        = string
  default     = ""
  description = "Cloudflare account that owns the Pages project. NOT a secret. Empty count-gates the Pages module, so the AWS half applies on its own."
}

variable "tunnel_id" {
  type    = string
  default = ""

  description = <<-EOT
    UUID of the `qnsc-kb-production` Cloudflare Tunnel — a DIFFERENT tunnel from
    develop's, never the same one. A tunnel maps a hostname to whichever connectors
    hold its token, so sharing one between environments would let a develop task serve
    production traffic.

    Created out of band; put its connector token in the production `tunnel-token`
    secret.
  EOT
}

variable "microsoft_client_id" {
  type        = string
  default     = ""
  description = "Entra application (client) ID for the Microsoft connector. A public identifier."
}

variable "google_client_id" {
  type        = string
  default     = ""
  description = "Google OAuth client ID for the Google connector. A public identifier."
}

variable "allowed_email_domains" {
  type        = list(string)
  default     = []
  description = "Restricts which email domains may be registered. Empty accepts any — narrow this before go-live."
}

variable "alarm_emails" {
  type    = list(string)
  default = []

  description = <<-EOT
    Addresses subscribed to the alarm SNS topic. Each subscription must be confirmed
    from the email itself — Terraform creates it as `pending confirmation` and cannot
    complete it, so an unconfirmed address is silently no alerting at all.
  EOT
}
