import uuid
from datetime import datetime
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from src.api.deps import get_db, get_current_user, require_role
from src.models import User
from src.models.ops import Connector, ConnectorJob
from src.domain.connectors import sync_local_folder
from src.core.config import settings

router = APIRouter()

class ConnectorCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    system: str = "local_folder"
    path: str = ""
    config: dict[str, Any] = Field(default_factory=dict)

def _response(connector: Connector) -> dict[str, Any]:
    return {
        "id": str(connector.id), "name": connector.name, "system": connector.system,
        "status": connector.status, "company_domain": connector.company_domain,
        "last_sync": connector.last_sync, "path": (connector.config_json or {}).get("path"),
    }

@router.get("")
async def list_connectors(
    current_user: User = Depends(require_role(["Admin", "CEO"])),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(Connector).order_by(Connector.company_domain, Connector.name)
    if current_user.role == "CEO":
        stmt = stmt.where(Connector.company_domain == current_user.company_domain)
    return [_response(item) for item in (await db.execute(stmt)).scalars().all()]

@router.post("", status_code=201)
async def create_connector(
    request: ConnectorCreate,
    current_user: User = Depends(require_role(["Admin", "CEO"])),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if request.system not in {"local_folder", "google_drive", "sharepoint", "slack"}:
        raise HTTPException(status_code=422, detail="Unsupported connector provider")
    folder = None
    if request.system == "local_folder":
        from src.domain.connectors import _safe_folder
        folder = _safe_folder(request.path)
    if current_user.role == "CEO" and current_user.company_domain not in request.name.lower():
        # Company remains the CEO's verified email domain; the name itself is not trusted for scoping.
        pass
    safe_config = {key: value for key, value in request.config.items() if key not in {"client_secret", "access_token", "refresh_token", "token"}}
    if folder:
        safe_config["path"] = str(folder)
    connector = Connector(name=request.name, system=request.system, company_domain=current_user.company_domain, created_by=current_user.id, config_json=safe_config)
    db.add(connector)
    await db.commit()
    await db.refresh(connector)
    return _response(connector)

@router.post("/{connector_id}/sync")
async def sync_connector(
    connector_id: uuid.UUID,
    current_user: User = Depends(require_role(["Admin", "CEO"])),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    connector = (await db.execute(select(Connector).where(Connector.id == connector_id))).scalar_one_or_none()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if current_user.role == "CEO" and connector.company_domain != current_user.company_domain:
        raise HTTPException(status_code=403, detail="CEOs can only sync their company connectors")
    if connector.system != "local_folder":
        connector.status = "pending_auth"
        await db.commit()
        return {"connector_id": str(connector.id), "job_id": None, "status": connector.status, "last_sync": connector.last_sync,
                "message": "Provider registered. OAuth authorization and provider worker configuration are required before sync."}
    job = await sync_local_folder(db, connector)
    return {"connector_id": str(connector.id), "job_id": str(job.id), "status": job.status, "last_sync": connector.last_sync}
