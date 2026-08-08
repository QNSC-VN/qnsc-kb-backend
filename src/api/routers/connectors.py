import uuid
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from jose import JWTError, jwt
from typing import Any, Literal
from urllib.parse import urlencode
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from src.api.deps import SessionLocal, get_db, get_current_user, require_permission, set_database_context
from src.models import User
from src.models.user import AccessGroup
from src.models.ops import Connector, ConnectorJob
from src.models.connectors import ExternalGroupMapping, SourceScope, SyncCursor, WebhookSubscription
from src.domain.connectors import sync_local_folder
from src.core.config import settings
from src.domain.rbac import AuthorizationService
from src.domain.connector_adapters import adapter_for, ConnectorProviderError, SharePointAdapter, GoogleDriveAdapter
from src.core.secrets import encrypt_secret

router = APIRouter()

class ConnectorCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    system: Literal["local_folder", "google_drive", "sharepoint"] = "local_folder"
    path: str = Field(default="", max_length=1_000)
    config: dict[str, Any] = Field(default_factory=dict)


class ConnectorUpdate(BaseModel):
    sync_mode: Literal["manual", "daily", "on_update"] | None = None


SYNC_MODES = {"manual", "daily", "on_update"}


class ScopeSelection(BaseModel):
    scope_ids: list[str] = Field(default_factory=list, max_length=500)


class GroupMappingRequest(BaseModel):
    access_group_id: uuid.UUID
    external_group_name: str | None = Field(default=None, max_length=255)


_SENSITIVE_CONFIG_KEYS = {"clientsecret", "accesstoken", "refreshtoken", "token", "apikey", "password", "secret"}
_MAX_CONNECTOR_CONFIG_BYTES = 16_384


def _safe_connector_config(config: dict[str, Any]) -> dict[str, Any]:
    """Reject secrets and oversized data before persisting connector config."""
    try:
        encoded = json.dumps(config, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Connector configuration must contain JSON values only") from exc
    if len(encoded.encode("utf-8")) > _MAX_CONNECTOR_CONFIG_BYTES:
        raise HTTPException(status_code=422, detail="Connector configuration is too large")

    def inspect(value: Any, depth: int = 0) -> None:
        if depth > 8:
            raise HTTPException(status_code=422, detail="Connector configuration is nested too deeply")
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = "".join(character for character in str(key).lower() if character.isalnum())
                if normalized_key in _SENSITIVE_CONFIG_KEYS:
                    raise HTTPException(status_code=422, detail="Connector credentials must be authorized through OAuth, not saved in configuration")
                inspect(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                inspect(item, depth + 1)

    inspect(config)
    return json.loads(encoded)

def _response(connector: Connector) -> dict[str, Any]:
    config = connector.config_json or {}
    return {
        "id": str(connector.id), "name": connector.name, "system": connector.system,
        "status": connector.status, "company_domain": connector.company_domain,
        "last_sync": connector.last_sync, "last_error": connector.last_error,
        "authorized": bool(connector.oauth_refresh_token or connector.oauth_access_token),
        "path": config.get("path"),
        "sync_mode": config.get("sync_mode", "manual" if connector.system == "local_folder" else "daily"),
        "webhook_enabled": bool(config.get("webhook_enabled")),
    }


def _can_complete_oauth(initiator: User | None, connector: Connector) -> bool:
    return bool(
        initiator
        and initiator.active
        and initiator.company_domain == connector.company_domain
        and AuthorizationService.has_permission(initiator, "connector.manage", requested_scope="company")
    )

@router.get("")
async def list_connectors(
    current_user: User = Depends(require_permission("connector.manage")),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(Connector).order_by(Connector.company_domain, Connector.name)
    if not AuthorizationService.has_permission(current_user, "connector.manage", requested_scope="global"):
        stmt = stmt.where(Connector.company_domain == current_user.company_domain)
    return [_response(item) for item in (await db.execute(stmt)).scalars().all()]

@router.post("", status_code=201)
async def create_connector(
    request: ConnectorCreate,
    current_user: User = Depends(require_permission("connector.manage")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if request.system not in {"local_folder", "google_drive", "sharepoint"}:
        raise HTTPException(status_code=422, detail="Unsupported connector provider")
    folder = None
    if request.system == "local_folder":
        from src.domain.connectors import _safe_folder
        folder = _safe_folder(request.path)
    safe_config = _safe_connector_config(request.config)
    sync_mode = safe_config.get("sync_mode", "manual" if request.system == "local_folder" else "daily")
    if sync_mode not in SYNC_MODES:
        raise HTTPException(status_code=422, detail="Sync mode must be manual, daily, or on_update")
    safe_config["sync_mode"] = sync_mode
    if folder:
        safe_config["path"] = str(folder)
    connector = Connector(name=request.name, system=request.system, company_domain=current_user.company_domain, created_by=current_user.id, config_json=safe_config)
    db.add(connector)
    await db.commit()
    await db.refresh(connector)
    return _response(connector)


@router.patch("/{connector_id}")
async def update_connector(
    connector_id: uuid.UUID,
    request: ConnectorUpdate,
    current_user: User = Depends(require_permission("connector.manage")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    connector = await db.get(Connector, connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.company_domain != current_user.company_domain and not AuthorizationService.has_permission(current_user, "connector.manage", requested_scope="global"):
        raise HTTPException(status_code=403, detail="Connector is outside your company")
    if request.sync_mode is not None:
        if request.sync_mode not in SYNC_MODES:
            raise HTTPException(status_code=422, detail="Sync mode must be manual, daily, or on_update")
        connector.config_json = {**(connector.config_json or {}), "sync_mode": request.sync_mode}
    await db.commit()
    await db.refresh(connector)
    return _response(connector)


def _oauth_frontend_redirect(*, connector_id: str | None = None, success: bool = False) -> RedirectResponse:
    params = {"oauth": "success" if success else "error"}
    if connector_id:
        params["connector_id"] = connector_id
    return RedirectResponse(
        url=f"{settings.FRONTEND_URL.rstrip('/')}/admin/connectors?{urlencode(params)}",
        status_code=303,
    )


@router.get("/oauth/callback")
async def oauth_callback_entry(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> Response:
    # Keep the provider redirect URI static (required by Google/Microsoft),
    # while the signed state identifies the connector being authorized.
    if error or not code or not state:
        return _oauth_frontend_redirect()
    try:
        result = await oauth_callback(code=code, state=state, db=db)
    except HTTPException:
        return _oauth_frontend_redirect()
    return _oauth_frontend_redirect(connector_id=result["connector_id"], success=True)


@router.get("/{connector_id}/oauth/start")
async def start_oauth(
    connector_id: uuid.UUID,
    current_user: User = Depends(require_permission("connector.manage")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    connector = (await db.execute(select(Connector).where(Connector.id == connector_id))).scalar_one_or_none()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.company_domain != current_user.company_domain and not AuthorizationService.has_permission(current_user, "connector.manage", requested_scope="global"):
        raise HTTPException(status_code=403, detail="Connector is outside your company")
    if connector.system == "local_folder":
        raise HTTPException(status_code=422, detail="Local folders do not require OAuth")
    if connector.system == "sharepoint" and not all((settings.MICROSOFT_CLIENT_ID, settings.MICROSOFT_CLIENT_SECRET, settings.MICROSOFT_REDIRECT_URI)):
        raise HTTPException(status_code=422, detail="Microsoft connector is not configured. Set MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET, and MICROSOFT_REDIRECT_URI in the API environment.")
    if connector.system == "google_drive" and not all((settings.GOOGLE_CLIENT_ID, settings.GOOGLE_CLIENT_SECRET, settings.GOOGLE_REDIRECT_URI)):
        raise HTTPException(status_code=422, detail="Google Drive connector is not configured. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REDIRECT_URI in the API environment.")
    state = jwt.encode({"type": "connector_oauth", "connector_id": str(connector.id), "user_id": str(current_user.id), "exp": datetime.utcnow() + timedelta(minutes=10)}, settings.SECRET_KEY, algorithm="HS256")
    connector.oauth_state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    connector.oauth_state_expires_at = datetime.utcnow() + timedelta(minutes=10)
    await db.commit()
    adapter = adapter_for(connector)
    if isinstance(adapter, SharePointAdapter):
        url = adapter.oauth_url(state)
    elif isinstance(adapter, GoogleDriveAdapter):
        url = adapter.oauth_url(state)
    else:
        raise HTTPException(status_code=422, detail="OAuth is not configured for this provider")
    return {"authorization_url": url}


@router.get("/{connector_id}/oauth/callback", include_in_schema=False)
async def oauth_callback(
    code: str,
    state: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        claims = jwt.decode(state, settings.SECRET_KEY, algorithms=["HS256"])
        if claims.get("type") != "connector_oauth":
            raise JWTError("invalid connector state")
        connector_id = uuid.UUID(str(claims["connector_id"]))
        initiator_id = uuid.UUID(str(claims["user_id"]))
    except (JWTError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="OAuth state is invalid or expired") from exc
    # Provider callbacks are authenticated by the short-lived signed state,
    # not a browser session.  Use explicit internal context so RLS can safely
    # protect the connector root table.
    await set_database_context(db, None, True)
    connector = (await db.execute(select(Connector).where(Connector.id == connector_id))).scalar_one_or_none()
    if not connector or not connector.oauth_state_hash or connector.oauth_state_hash != hashlib.sha256(state.encode("utf-8")).hexdigest() or not connector.oauth_state_expires_at or connector.oauth_state_expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="OAuth state is invalid or expired")
    initiator = await UserRepository(db).get_by_id(initiator_id)
    if not _can_complete_oauth(initiator, connector):
        connector.oauth_state_hash = None
        connector.oauth_state_expires_at = None
        await db.commit()
        raise HTTPException(status_code=403, detail="The user who started this authorization no longer has connector access")
    try:
        if claims.get("connector_id") != str(connector.id):
            raise JWTError("invalid connector state")
        tokens = await adapter_for(connector).exchange_code(code)
    except (JWTError, ConnectorProviderError) as exc:
        connector.status = "auth_failed"
        connector.last_error = str(exc)
        await db.commit()
        raise HTTPException(status_code=400, detail="Provider authorization failed") from exc
    connector.oauth_access_token = encrypt_secret(tokens.get("access_token"))
    connector.oauth_refresh_token = encrypt_secret(tokens.get("refresh_token")) or connector.oauth_refresh_token
    subject = str(tokens.get("token_type") or "authorized")
    if tokens.get("id_token"):
        try:
            subject = str(jwt.get_unverified_claims(tokens["id_token"]).get("sub") or subject)
        except JWTError:
            pass
    connector.oauth_subject = subject[:255]
    if tokens.get("expires_in"):
        connector.oauth_expires_at = datetime.utcnow() + timedelta(seconds=int(tokens["expires_in"]))
    connector.oauth_state_hash = None
    connector.oauth_state_expires_at = None
    connector.status = "active"
    connector.last_error = None
    await db.commit()
    return {"connector_id": str(connector.id), "status": connector.status, "message": "Connector authorized; select scopes before syncing."}


@router.get("/{connector_id}/scopes")
async def discover_scopes(
    connector_id: uuid.UUID,
    current_user: User = Depends(require_permission("connector.manage")),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    connector = (await db.execute(select(Connector).where(Connector.id == connector_id))).scalar_one_or_none()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.company_domain != current_user.company_domain and not AuthorizationService.has_permission(current_user, "connector.manage", requested_scope="global"):
        raise HTTPException(status_code=403, detail="Connector is outside your company")
    try:
        scopes = await adapter_for(connector).discover_scopes()
    except ConnectorProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return scopes


@router.put("/{connector_id}/scopes")
async def select_scopes(
    connector_id: uuid.UUID,
    request: ScopeSelection,
    current_user: User = Depends(require_permission("connector.manage")),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    connector = (await db.execute(select(Connector).where(Connector.id == connector_id))).scalar_one_or_none()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.company_domain != current_user.company_domain and not AuthorizationService.has_permission(current_user, "connector.manage", requested_scope="global"):
        raise HTTPException(status_code=403, detail="Connector is outside your company")
    selected = set(request.scope_ids)
    existing = (await db.execute(select(SourceScope).where(SourceScope.connector_id == connector.id))).scalars().all()
    for scope in existing:
        scope.selected = scope.external_scope_id in selected
    known = {scope.external_scope_id for scope in existing}
    for item in await adapter_for(connector).discover_scopes():
        if item["external_scope_id"] in selected and item["external_scope_id"] not in known:
            db.add(SourceScope(connector_id=connector.id, external_scope_id=item["external_scope_id"], scope_type=item["scope_type"], display_name=item["display_name"], selected=True, config_json=item.get("config")))
    await db.commit()
    return [{"external_scope_id": scope.external_scope_id, "display_name": scope.display_name, "scope_type": scope.scope_type, "selected": scope.selected} for scope in (await db.execute(select(SourceScope).where(SourceScope.connector_id == connector.id))).scalars().all()]


@router.post("/{connector_id}/webhooks/subscribe")
async def subscribe_webhooks(
    connector_id: uuid.UUID,
    current_user: User = Depends(require_permission("connector.manage")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    connector = await db.get(Connector, connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.company_domain != current_user.company_domain and not AuthorizationService.has_permission(current_user, "connector.manage", requested_scope="global"):
        raise HTTPException(status_code=403, detail="Connector is outside your company")
    if connector.system == "local_folder":
        raise HTTPException(status_code=422, detail="Local folders do not support webhooks")
    if not settings.CONNECTOR_WEBHOOK_BASE_URL:
        raise HTTPException(status_code=422, detail="Configure CONNECTOR_WEBHOOK_BASE_URL before enabling update notifications")
    scopes = (await db.execute(select(SourceScope).where(SourceScope.connector_id == connector.id, SourceScope.selected.is_(True)))).scalars().all()
    if not scopes:
        raise HTTPException(status_code=409, detail="Select at least one folder or drive before enabling update notifications")
    callback = f"{settings.CONNECTOR_WEBHOOK_BASE_URL.rstrip('/')}/api/v1/connectors/webhooks/{connector.system.replace('_', '-')}"
    adapter = adapter_for(connector)
    created = []
    for scope in scopes:
        result = await adapter.create_webhook({"external_scope_id": scope.external_scope_id, "config": scope.config_json or {}}, callback)
        db.add(WebhookSubscription(connector_id=connector.id, scope_id=scope.id, provider_subscription_id=result["subscription_id"], verification_token_hash=hashlib.sha256(result["client_state"].encode("utf-8")).hexdigest(), expires_at=result.get("expires_at"), active=True))
        created.append({"scope_id": str(scope.id), "subscription_id": result["subscription_id"], "expires_at": result.get("expires_at")})
    connector.config_json = {**(connector.config_json or {}), "webhook_enabled": True, "sync_mode": "on_update"}
    await db.commit()
    return {"connector_id": str(connector.id), "subscriptions": created, "sync_mode": "on_update"}


@router.get("/{connector_id}/group-mappings")
async def list_group_mappings(
    connector_id: uuid.UUID,
    current_user: User = Depends(require_permission("connector.manage")),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    connector = await db.get(Connector, connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.company_domain != current_user.company_domain and not AuthorizationService.has_permission(current_user, "connector.manage", requested_scope="global"):
        raise HTTPException(status_code=403, detail="Connector is outside your company")
    mappings = (await db.execute(select(ExternalGroupMapping, AccessGroup.name).join(AccessGroup, AccessGroup.id == ExternalGroupMapping.access_group_id).where(ExternalGroupMapping.connector_id == connector.id))).all()
    return [{"external_group_id": mapping.external_group_id, "external_group_name": mapping.external_group_name, "access_group_id": str(mapping.access_group_id), "access_group_name": name, "active": mapping.active} for mapping, name in mappings]


@router.put("/{connector_id}/group-mappings/{external_group_id}")
async def set_group_mapping(
    connector_id: uuid.UUID,
    request: GroupMappingRequest,
    external_group_id: str = Path(..., min_length=1, max_length=512),
    current_user: User = Depends(require_permission("connector.manage")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    connector = await db.get(Connector, connector_id)
    group = await db.get(AccessGroup, request.access_group_id)
    if not connector or not group:
        raise HTTPException(status_code=404, detail="Connector or access group not found")
    if connector.company_domain != current_user.company_domain and not AuthorizationService.has_permission(current_user, "connector.manage", requested_scope="global"):
        raise HTTPException(status_code=403, detail="Connector is outside your company")
    if group.company_domain != connector.company_domain:
        raise HTTPException(status_code=422, detail="An access group must belong to the connector's company")
    mapping = (await db.execute(select(ExternalGroupMapping).where(ExternalGroupMapping.connector_id == connector.id, ExternalGroupMapping.external_group_id == external_group_id))).scalar_one_or_none()
    if mapping is None:
        mapping = ExternalGroupMapping(connector_id=connector.id, external_group_id=external_group_id, external_group_name=request.external_group_name, access_group_id=group.id, active=True)
        db.add(mapping)
    else:
        mapping.external_group_name = request.external_group_name or mapping.external_group_name
        mapping.access_group_id = group.id
        mapping.active = True
    await db.commit()
    return {"external_group_id": mapping.external_group_id, "access_group_id": str(mapping.access_group_id), "active": mapping.active}


@router.delete("/{connector_id}/group-mappings/{external_group_id}", status_code=204)
async def delete_group_mapping(
    connector_id: uuid.UUID,
    external_group_id: str = Path(..., min_length=1, max_length=512),
    current_user: User = Depends(require_permission("connector.manage")),
    db: AsyncSession = Depends(get_db),
) -> None:
    connector = await db.get(Connector, connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.company_domain != current_user.company_domain and not AuthorizationService.has_permission(current_user, "connector.manage", requested_scope="global"):
        raise HTTPException(status_code=403, detail="Connector is outside your company")
    mapping = (await db.execute(select(ExternalGroupMapping).where(ExternalGroupMapping.connector_id == connector.id, ExternalGroupMapping.external_group_id == external_group_id))).scalar_one_or_none()
    if mapping:
        mapping.active = False
        await db.commit()

@router.post("/{connector_id}/sync")
async def sync_connector(
    connector_id: uuid.UUID,
    current_user: User = Depends(require_permission("connector.manage")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    connector = (await db.execute(select(Connector).where(Connector.id == connector_id))).scalar_one_or_none()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if not AuthorizationService.has_permission(current_user, "connector.manage", requested_scope="global") and connector.company_domain != current_user.company_domain:
        raise HTTPException(status_code=403, detail="CEOs can only sync their company connectors")
    if connector.system != "local_folder":
        if not connector.oauth_access_token and not connector.oauth_refresh_token:
            raise HTTPException(status_code=409, detail="Authorize the connector before syncing")
        from src.workers.tasks import sync_cloud_connector_task
        job = ConnectorJob(connector_id=connector.id, status="queued", attempts=0)
        db.add(job)
        await db.commit()
        sync_cloud_connector_task.delay(str(connector.id), str(job.id))
        return {"connector_id": str(connector.id), "job_id": str(job.id), "status": "queued", "last_sync": connector.last_sync}
    job = await sync_local_folder(db, connector)
    return {"connector_id": str(connector.id), "job_id": str(job.id), "status": job.status, "last_sync": connector.last_sync}


async def _enqueue_webhook(request: Request, provider: str) -> Response:
    headers = request.headers
    subscription_id = headers.get("x-goog-channel-id") if provider == "google_drive" else None
    client_state = headers.get("x-goog-channel-token") if provider == "google_drive" else None
    if not subscription_id:
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            # Webhook endpoints are public by design. Malformed or unsolicited
            # bodies must be harmless and must not turn into a 500 response.
            return Response(status_code=202)
        if not isinstance(payload, dict):
            return Response(status_code=202)
        values = payload.get("value")
        first_value = values[0] if isinstance(values, list) and values and isinstance(values[0], dict) else {}
        subscription_id = payload.get("subscriptionId") or first_value.get("subscriptionId")
        client_state = client_state or payload.get("clientState") or first_value.get("clientState")
    if subscription_id:
        from src.workers.tasks import sync_cloud_connector_task
        async with SessionLocal() as db_session:
            await set_database_context(db_session, None, True)
            subscription = (await db_session.execute(select(WebhookSubscription).where(WebhookSubscription.provider_subscription_id == subscription_id, WebhookSubscription.active.is_(True)))).scalar_one_or_none()
            expected_token = subscription.verification_token_hash if subscription else None
            received_token = hashlib.sha256(client_state.encode("utf-8")).hexdigest() if client_state else None
            if subscription and expected_token and received_token and hmac.compare_digest(expected_token, received_token):
                connector_job = ConnectorJob(connector_id=subscription.connector_id, status="queued", attempts=0)
                db_session.add(connector_job)
                await db_session.commit()
                sync_cloud_connector_task.delay(str(subscription.connector_id), str(connector_job.id))
    return Response(status_code=202)


@router.post("/webhooks/sharepoint")
async def sharepoint_webhook(request: Request, validationToken: str | None = Query(default=None)) -> Response:
    if validationToken:
        return Response(content=validationToken, media_type="text/plain")
    return await _enqueue_webhook(request, "sharepoint")


@router.post("/webhooks/google-drive")
async def google_drive_webhook(request: Request) -> Response:
    return await _enqueue_webhook(request, "google_drive")
