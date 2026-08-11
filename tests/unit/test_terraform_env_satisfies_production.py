"""Terraform must supply every setting validate_production() insists on.

THIS IS THE TEST THAT WAS MISSING. Three separate outages in one day, all the same shape:
a code change added a production requirement, Terraform did not know about it, and the
API refused to boot — with the failure appearing only when a task started, minutes after
a green CI run and a successful deploy.

  MICROSOFT_LOGIN_REDIRECT_URI   added by the SSO change; never wired
  BOOTSTRAP_ADMIN_ENABLED        new bootstrap needed either a real password or this off
  EMBEDDING_MODEL                pinned to a hosted model the image does not contain

Nothing caught them because CI never constructs the settings a deployed task receives. It
tests a fresh database and a Settings built from defaults, and both of those are the
cases that work.

So: read the environment the stack module actually renders, build Settings from it, and
run the production guardrails against it. A new requirement that Terraform does not
satisfy now fails here, in the PR that introduces it, naming the setting.

The values are synthesised — this asserts PRESENCE and shape, not correctness of any
particular URL. What it cannot check is a value only AWS knows (a database endpoint, a
secret's contents); those are covered by the deploy pipeline's own preflight, which
refuses to register a task definition while an injected secret is still empty.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core.config import Settings

STACK = Path(__file__).resolve().parents[2] / "infra" / "modules" / "stack" / "main.tf"

# A valid v4 UUID: MICROSOFT_TENANT_ID is regex-checked, so "x" would fail for a reason
# that has nothing to do with whether Terraform sets it.
TENANT = "00000000-0000-4000-8000-000000000000"
ORIGIN = "https://kb.example.com"


def _terraform_env_names() -> set[str]:
    """Every container env var and injected secret the stack module renders.

    Parsed rather than duplicated: a list maintained by hand here would drift from the
    module and quietly stop testing anything.
    """
    source = STACK.read_text(encoding="utf-8")
    env = set(re.findall(r'\{\s*name\s*=\s*"([A-Z0-9_]+)"\s*,\s*value\s*=', source))
    secrets = set(re.findall(r'\{\s*name\s*=\s*"([A-Z0-9_]+)"\s*,\s*secret_arn\s*=', source))
    # Also the one-off migrator task, which uses map syntax rather than a list of objects.
    plain = set(re.findall(r"^\s{4}([A-Z][A-Z0-9_]{3,})\s*=\s", source, re.M))
    return env | secrets | plain


def _synthesise(name: str, literal: str | None) -> str:
    """A plausible value, so a failure means MISSING rather than malformed."""
    if literal in {"true", "false"}:
        return literal
    # Type comes from Settings, not from guessing: several values are Terraform
    # expressions rather than literals (tostring(var.x), module.rds.port), so the module
    # text cannot say whether a field is an int or a bool.
    annotation = str(Settings.model_fields[name].annotation) if name in Settings.model_fields else ""
    if "bool" in annotation:
        return "true"
    if "int" in annotation:
        return "5432" if "PORT" in name else "1"
    if "float" in annotation:
        return "1.0"
    if name == "MICROSOFT_TENANT_ID":
        return TENANT
    if name.endswith(("_URI", "_URL")):
        if "R2" in name or "S3" in name:
            return "https://accountid.r2.cloudflarestorage.com"
        if name in {"CORS_ORIGINS", "FRONTEND_URL"}:
            return ORIGIN
        return f"{ORIGIN}/api/v1/callback"
    if name in {"CORS_ORIGINS", "FRONTEND_URL"}:
        return ORIGIN
    if any(marker in name for marker in ("KEY", "SECRET", "PASSWORD", "TOKEN")):
        return "s" * 64
    if literal is not None:
        return literal
    return "configured"


def _literals() -> dict[str, str]:
    """Literal values the module hard-codes, e.g. ENABLE_RLS = "true"."""
    source = STACK.read_text(encoding="utf-8")
    pairs = re.findall(r'\{\s*name\s*=\s*"([A-Z0-9_]+)"\s*,\s*value\s*=\s*"([^"${]*)"\s*\}', source)
    return dict(pairs)


def _deployed_settings() -> Settings:
    names = _terraform_env_names()
    literals = _literals()
    values = {name: _synthesise(name, literals.get(name)) for name in names}
    # ENVIRONMENT is rendered as a literal "production" by the module; be explicit, since
    # the whole point is to exercise the production branch.
    values["ENVIRONMENT"] = "production"
    known = set(Settings.model_fields)
    return Settings(_env_file=None, **{k: v for k, v in values.items() if k in known})


def test_the_stack_module_renders_something_to_check():
    """A parser that silently matches nothing would make this suite pass vacuously."""
    names = _terraform_env_names()
    assert len(names) > 20, f"only parsed {len(names)} env names from {STACK.name}"
    for expected in ("ENABLE_RLS", "SECRET_KEY", "EMBEDDING_MODEL", "MICROSOFT_CLIENT_ID"):
        assert expected in names, f"{expected} not parsed — the regex has drifted"


def test_terraform_env_passes_the_production_guardrails():
    """The regression test for three separate outages.

    If this fails with "X is required in production", Terraform is missing X: add it to
    infra/modules/stack/main.tf rather than relaxing the guardrail.
    """
    settings = _deployed_settings()
    settings.validate_production()


@pytest.mark.parametrize(
    "dropped",
    ["MICROSOFT_LOGIN_REDIRECT_URI", "BOOTSTRAP_ADMIN_ENABLED", "MALWARE_SCANNER_HOST"],
)
def test_the_guard_actually_fails_when_a_setting_goes_missing(dropped):
    """Proves the test above has teeth — each of these took develop down for real."""
    names = _terraform_env_names() - {dropped}
    literals = _literals()
    values = {name: _synthesise(name, literals.get(name)) for name in names}
    values["ENVIRONMENT"] = "production"
    known = set(Settings.model_fields)
    settings = Settings(_env_file=None, **{k: v for k, v in values.items() if k in known})

    with pytest.raises(RuntimeError):
        settings.validate_production()
