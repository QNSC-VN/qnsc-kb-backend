// PUBLIC identifiers live here, in git, deliberately — not in Actions variables.
//
// The infra-plan job has no `environment:` context (adding one would gate every PR
// behind the production reviewer), so an environment-scoped Actions variable resolves
// to "" during plan. Values that reach apply but not plan make the plan LIE: resources
// gated on them appear as phantom creates or destroys, and a real destroy hidden among
// them is easy to miss. Held here, plan and apply see the same value.
//
// Only the Cloudflare API token — an actual credential — is passed in from CI.

variable "cloudflare_api_token" {
  type      = string
  sensitive = true
  default   = ""

  description = <<-EOT
    Cloudflare API token with Pages + DNS edit scope, from TF_VAR_cloudflare_api_token.
    Empty skips provider auth so the stack can be planned before Cloudflare is set up.
  EOT
}

variable "cloudflare_account_id" {
  type    = string
  default = ""

  description = <<-EOT
    Cloudflare account that owns the Pages project. NOT a secret.

    Empty until the account is wired up, and that is a safe intermediate state: the
    Pages module is count-gated on it, so the AWS half of this stack applies on its own
    and the SPA arrives in a later apply.
  EOT
}

variable "tunnel_id" {
  type    = string
  default = ""

  description = <<-EOT
    UUID of the `qnsc-kb-develop` Cloudflare Tunnel.

    Created OUT OF BAND: a tunnel and its connector token are one Cloudflare object, so
    Terraform cannot mint them. Create the tunnel, put its token in the `tunnel-token`
    secret, and set the UUID here.

    While empty, the `tunnel_has_id` check in the stack module reports a warning on
    every plan and apply — checks are advisory, so read it rather than relying on it to
    stop you. Applying in that state deploys an API with no route to it.
  EOT
}

variable "microsoft_client_id" {
  type        = string
  default     = ""
  description = "Entra application (client) ID for the Microsoft connector. A public identifier. Empty leaves that connector dormant."
}

variable "google_client_id" {
  type        = string
  default     = ""
  description = "Google OAuth client ID for the Google connector. A public identifier. Empty leaves that connector dormant."
}

variable "allowed_email_domains" {
  type        = list(string)
  default     = []
  description = "Restricts which email domains may be registered. Empty accepts any."
}
