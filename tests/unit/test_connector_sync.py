from src.domain.cloud_sync import _acl_hash
import asyncio

import httpx
import pytest
from fastapi import HTTPException

from src.domain.connector_adapters import ConnectorAdapter, ConnectorProviderError, NormalizedChange
from src.api.routers.connectors import _can_complete_oauth, _safe_connector_config
from src.models.ops import Connector
from src.models.rbac import Permission, Role, RolePermission
from src.models.user import User


def test_acl_hash_is_order_independent():
    left = [{"principal_type": "group", "principal_id": "g2", "role": "reader"}, {"principal_type": "user", "principal_id": "u1", "role": "reader"}]
    right = list(reversed(left))
    assert _acl_hash(left) == _acl_hash(right)


def test_normalized_change_preserves_move_and_content_flags():
    change = NormalizedChange(
        external_id="file-1",
        corpus_id="drive-1",
        name="policy.pdf",
        state="active",
        content_changed=True,
        permissions_changed=False,
        moved=True,
        revision="etag-2",
        mime_type="application/pdf",
        parent_external_id="folder-2",
        web_url="https://provider.example/file-1",
        metadata={"size": 100},
    )
    assert change.external_id == "file-1"
    assert change.moved is True
    assert change.content_changed is True


class _Adapter(ConnectorAdapter):
    allowed_api_hosts = frozenset({"api.example.com"})

    @property
    def access_token(self):
        return "provider-token"


class _MockAdapter(_Adapter):
    def __init__(self, handler):
        super().__init__(connector=None)  # type: ignore[arg-type]
        self.handler = handler

    def _http_client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), follow_redirects=False, trust_env=False)


def test_connector_rejects_provider_controlled_urls_outside_the_official_api():
    adapter = _Adapter(connector=None)  # type: ignore[arg-type]
    with pytest.raises(ConnectorProviderError, match="untrusted API URL"):
        adapter._validate_provider_url("https://metadata.internal/latest")


@pytest.mark.parametrize("url", ["http://downloads.example.com/file", "https://127.0.0.1/file", "https://user:pass@downloads.example.com/file"])
def test_connector_rejects_unsafe_download_redirects(url):
    with pytest.raises(ConnectorProviderError, match="unsafe download redirect"):
        _Adapter._validate_redirect_url(url)


def test_connector_config_rejects_nested_credentials_and_oversized_data():
    with pytest.raises(HTTPException, match="credentials"):
        _safe_connector_config({"sync_mode": "daily", "nested": {"access_token": "do-not-store"}})
    with pytest.raises(HTTPException, match="too large"):
        _safe_connector_config({"note": "x" * 20_000})
    assert _safe_connector_config({"sync_mode": "daily", "folders": ["abc"]}) == {"sync_mode": "daily", "folders": ["abc"]}


def test_oauth_callback_rechecks_the_authorizing_manager():
    connector = Connector(name="Drive", system="google_drive", company_domain="acme.test")
    initiator = User(email="manager@acme.test", name="Manager", password_hash="hash", company_domain="acme.test", role="Staff", active=True)
    permission = Permission(key="connector.manage", name="Connector management")
    role = Role(name="Connector manager", company_domain="acme.test", active=True)
    role.permissions.append(RolePermission(permission=permission, scope="company"))
    initiator.roles.append(role)
    assert _can_complete_oauth(initiator, connector)
    initiator.active = False
    assert not _can_complete_oauth(initiator, connector)


def test_connector_strips_bearer_token_before_provider_download_redirect():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "api.example.com":
            return httpx.Response(302, headers={"location": "https://downloads.example.com/file"}, request=request)
        return httpx.Response(200, content=b"document", headers={"content-type": "application/octet-stream"}, request=request)

    result = asyncio.run(_MockAdapter(handler)._request("GET", "https://api.example.com/download"))
    assert result == b"document"
    assert requests[0].headers["authorization"] == "Bearer provider-token"
    assert "authorization" not in requests[1].headers
