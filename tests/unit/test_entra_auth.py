from urllib.parse import parse_qs, urlparse
import pytest

from src.core.config import settings
from src.domain import entra_auth


def test_entra_authorization_url_contains_only_non_secret_parameters(monkeypatch):
    monkeypatch.setattr(settings, "MICROSOFT_CLIENT_ID", "client-id")
    monkeypatch.setattr(settings, "MICROSOFT_CLIENT_SECRET", "secret-value")
    monkeypatch.setattr(settings, "MICROSOFT_TENANT_ID", "tenant-id")
    monkeypatch.setattr(settings, "MICROSOFT_LOGIN_REDIRECT_URI", "https://kb.example/auth/entra/callback")

    url = entra_auth.authorization_url("signed-state", "nonce-value")
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["client-id"]
    assert query["state"] == ["signed-state"]
    assert query["nonce"] == ["nonce-value"]
    assert "secret-value" not in url
    assert entra_auth.configured()


def test_entra_token_metadata_rejects_algorithm_confusion_and_wrong_tenant(monkeypatch):
    monkeypatch.setattr(settings, "MICROSOFT_TENANT_ID", "tenant-guid")
    claims = {"iss": "https://login.microsoftonline.com/tenant-guid/v2.0", "tid": "tenant-guid"}

    with pytest.raises(ValueError, match="algorithm"):
        entra_auth._validate_id_token_metadata({"alg": "HS256"}, claims)

    with pytest.raises(ValueError, match="tenant"):
        entra_auth._validate_id_token_metadata(
            {"alg": "RS256"},
            {"iss": "https://login.microsoftonline.com/other-tenant/v2.0", "tid": "other-tenant"},
        )
