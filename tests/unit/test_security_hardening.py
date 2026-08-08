import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi import Response
from starlette.requests import Request
from jose import jwt

from src.core.config import Settings
from src.core.security import create_access_token
from src.lib import embeddings
from src.api.routers.auth import _reject_cross_site_auth_request


def test_production_rejects_default_secret_and_auto_schema():
    settings = Settings(
        ENVIRONMENT="production",
        SECRET_KEY="super-secret-key-change-in-production",
        AUTO_CREATE_SCHEMA=False,
        ALLOW_SELF_REGISTRATION=False,
        CORS_ORIGINS="https://kb.example.com",
    )
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        settings.validate_production()


def test_production_rejects_a_local_frontend_url():
    settings = Settings(
        ENVIRONMENT="production",
        SECRET_KEY="a" * 32,
        DATA_ENCRYPTION_KEY="d" * 32,
        AUTO_CREATE_SCHEMA=False,
        ALLOW_SELF_REGISTRATION=False,
        ENABLE_API_DOCS=False,
        MALWARE_SCAN_ENABLED=True,
        MALWARE_SCANNER_HOST="clamav",
        CORS_ORIGINS="https://kb.example.com",
        FRONTEND_URL="http://localhost:5173",
    )
    with pytest.raises(RuntimeError, match="FRONTEND_URL"):
        settings.validate_production()


def test_production_requires_a_separate_data_encryption_key():
    settings = Settings(
        ENVIRONMENT="production",
        SECRET_KEY="a" * 32,
        DATA_ENCRYPTION_KEY=None,
        AUTO_CREATE_SCHEMA=False,
        ALLOW_SELF_REGISTRATION=False,
        ENABLE_API_DOCS=False,
        MALWARE_SCAN_ENABLED=True,
        MALWARE_SCANNER_HOST="clamav",
        FRONTEND_URL="https://kb.example.com",
        CORS_ORIGINS="https://kb.example.com",
        MICROSOFT_REDIRECT_URI=None,
        GOOGLE_REDIRECT_URI=None,
    )
    with pytest.raises(RuntimeError, match="DATA_ENCRYPTION_KEY"):
        settings.validate_production()


def test_production_rejects_an_insecure_credentialed_cors_origin():
    settings = Settings(
        ENVIRONMENT="production",
        SECRET_KEY="a" * 32,
        DATA_ENCRYPTION_KEY="d" * 32,
        AUTO_CREATE_SCHEMA=False,
        ALLOW_SELF_REGISTRATION=False,
        ENABLE_API_DOCS=False,
        MALWARE_SCAN_ENABLED=True,
        MALWARE_SCANNER_HOST="clamav",
        FRONTEND_URL="https://kb.example.com",
        CORS_ORIGINS="https://kb.example.com,http://not-safe.example.com",
    )
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        settings.validate_production()


def test_production_requires_r2_credentials():
    settings = Settings(
        ENVIRONMENT="production",
        SECRET_KEY="a" * 32,
        DATA_ENCRYPTION_KEY="d" * 32,
        AUTO_CREATE_SCHEMA=False,
        ALLOW_SELF_REGISTRATION=False,
        ENABLE_API_DOCS=False,
        MALWARE_SCAN_ENABLED=True,
        MALWARE_SCANNER_HOST="clamav",
        FRONTEND_URL="https://kb.example.com",
        CORS_ORIGINS="https://kb.example.com",
        MICROSOFT_REDIRECT_URI=None,
        GOOGLE_REDIRECT_URI=None,
        SOURCE_STORAGE_BACKEND="r2",
        SOURCE_STORAGE_BUCKET="private-kb",
        R2_ACCOUNT_ID="account-id",
        R2_ACCESS_KEY_ID="",
        R2_SECRET_ACCESS_KEY="",
    )
    with pytest.raises(RuntimeError, match="R2_ACCESS_KEY_ID"):
        settings.validate_production()


def test_production_rejects_insecure_connector_callback_urls():
    settings = Settings(
        ENVIRONMENT="production",
        SECRET_KEY="a" * 32,
        DATA_ENCRYPTION_KEY="d" * 32,
        AUTO_CREATE_SCHEMA=False,
        ALLOW_SELF_REGISTRATION=False,
        ENABLE_API_DOCS=False,
        MALWARE_SCAN_ENABLED=True,
        MALWARE_SCANNER_HOST="clamav",
        FRONTEND_URL="https://kb.example.com",
        CORS_ORIGINS="https://kb.example.com",
        MICROSOFT_REDIRECT_URI=None,
        GOOGLE_REDIRECT_URI=None,
        CONNECTOR_WEBHOOK_BASE_URL="http://localhost:8000",
    )
    with pytest.raises(RuntimeError, match="CONNECTOR_WEBHOOK_BASE_URL"):
        settings.validate_production()


def test_ceo_includes_company_review_permission():
    from src.domain.rbac import AuthorizationService
    from src.models.user import User

    ceo = User(role="CEO", company_domain="acme.test")
    assert AuthorizationService.has_permission(ceo, "article.review", requested_scope="company")


def test_only_the_global_admin_role_gets_the_database_wide_bypass():
    from src.domain.rbac import AuthorizationService
    from src.models.rbac import Role
    from src.models.user import User

    cross_company_reader = User(role="Staff", company_domain="acme.test")
    cross_company_reader.roles.append(Role(name="Reader", company_domain="acme.test", active=True))
    assert not AuthorizationService.is_global_administrator(cross_company_reader)

    admin = User(role="Admin", company_domain="acme.test")
    admin.roles.append(Role(name="Admin", company_domain=None, active=True))
    assert AuthorizationService.is_global_administrator(admin)


def test_global_article_read_does_not_grant_group_directory_access():
    from src.domain.rbac import AuthorizationService
    from src.models.rbac import Permission, Role, RolePermission
    from src.models.user import User

    user = User(role="Staff", company_domain="acme.test")
    permission = Permission(key="article.read", name="Read")
    role = Role(name="Global reader", company_domain="acme.test", active=True)
    role.permissions.append(RolePermission(permission=permission, scope="global"))
    user.roles.append(role)
    assert AuthorizationService.has_permission(user, "article.read", requested_scope="global")
    assert not AuthorizationService.can_view_all_access_groups(user)


def test_explicit_global_user_management_enables_only_identity_routing():
    from src.domain.rbac import AuthorizationService
    from src.models.rbac import Permission, Role, RolePermission
    from src.models.user import User

    user = User(role="Staff", company_domain="acme.test")
    permission = Permission(key="user.manage", name="Manage users")
    role = Role(name="Global user manager", company_domain="acme.test", active=True)
    role.permissions.append(RolePermission(permission=permission, scope="global"))
    user.roles.append(role)
    assert AuthorizationService.has_global_identity_management(user)
    assert not AuthorizationService.is_global_administrator(user)


def test_explicit_global_connector_management_enables_only_connector_routing():
    from src.domain.rbac import AuthorizationService
    from src.models.rbac import Permission, Role, RolePermission
    from src.models.user import User

    user = User(role="Staff", company_domain="acme.test")
    permission = Permission(key="connector.manage", name="Manage connectors")
    role = Role(name="Global connector manager", company_domain="acme.test", active=True)
    role.permissions.append(RolePermission(permission=permission, scope="global"))
    user.roles.append(role)
    assert AuthorizationService.has_global_connector_management(user)
    assert not AuthorizationService.has_global_identity_management(user)
    assert not AuthorizationService.is_global_administrator(user)


def test_explicit_global_governance_access_enables_only_governance_routing():
    from src.domain.rbac import AuthorizationService
    from src.models.rbac import Permission, Role, RolePermission
    from src.models.user import User

    user = User(role="Staff", company_domain="acme.test")
    permission = Permission(key="governance.read", name="Read governance")
    role = Role(name="Global governance reader", company_domain="acme.test", active=True)
    role.permissions.append(RolePermission(permission=permission, scope="global"))
    user.roles.append(role)
    assert AuthorizationService.has_global_governance_access(user)
    assert not AuthorizationService.has_global_identity_management(user)
    assert not AuthorizationService.has_global_connector_management(user)


def test_global_article_review_enables_article_routing_without_identity_access():
    from src.domain.rbac import AuthorizationService
    from src.models.rbac import Permission, Role, RolePermission
    from src.models.user import User
    user = User(role="Staff", company_domain="acme.test")
    role = Role(name="Global reviewer", company_domain="acme.test", active=True)
    role.permissions.append(RolePermission(permission=Permission(key="article.review", name="Review"), scope="global"))
    user.roles.append(role)
    assert AuthorizationService.has_global_article_access(user)
    assert not AuthorizationService.has_global_identity_management(user)


def test_access_token_carries_auth_version():
    token = create_access_token("user@acme.test", auth_version=4)
    assert jwt.get_unverified_claims(token)["av"] == 4


def test_embedding_failure_is_not_converted_to_zero_vector(monkeypatch):
    monkeypatch.setattr(embeddings.BGEModelSingleton, "get_model", classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("model unavailable"))))
    with pytest.raises(RuntimeError, match="Embedding generation failed"):
        embeddings.get_bge_embedding("test")


def test_cookie_auth_endpoints_reject_unapproved_browser_origins():
    request = Request({"type": "http", "headers": [(b"origin", b"https://attacker.example")]})
    with pytest.raises(HTTPException, match="Cross-site"):
        _reject_cross_site_auth_request(request)
    _reject_cross_site_auth_request(Request({"type": "http", "headers": []}))


def test_refresh_subject_rejects_access_tokens_and_extracts_a_valid_refresh_token():
    from src.api.routers.auth import _refresh_claims_from_token, _refresh_subject_from_token
    from src.core.security import create_refresh_token

    token = create_refresh_token("User@Acme.Test")
    assert _refresh_subject_from_token(token) == "user@acme.test"
    assert _refresh_claims_from_token(token)["av"] == 0
    with pytest.raises(HTTPException, match="Refresh token"):
        _refresh_subject_from_token(create_access_token("user@acme.test"))


def test_logout_sets_tenant_context_before_revoking_a_refresh_session():
    from src.api.routers.auth import RefreshRequest, logout
    from src.core.security import create_refresh_token

    session = SimpleNamespace(revoked_at=None)

    class Result:
        def scalar_one_or_none(self):
            return session

    class Db:
        def __init__(self):
            self.calls = []
            self.committed = False

        async def execute(self, statement, *_args, **_kwargs):
            self.calls.append(str(statement))
            return Result()

        async def commit(self):
            self.committed = True

    db = Db()
    token = create_refresh_token("user@acme.test")
    request = Request({"type": "http", "headers": []})
    asyncio.run(logout(request, Response(), RefreshRequest(refresh_token=token), None, db))
    assert "set_config('app.company_domain'" in db.calls[0]
    assert session.revoked_at is not None
    assert db.committed
