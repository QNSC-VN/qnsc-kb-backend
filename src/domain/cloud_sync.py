"""Incremental cloud connector synchronization and version handoff."""
from __future__ import annotations

import hashlib
import json
import asyncio
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.config import settings
from src.core.secrets import encrypt_secret
from src.domain.connector_adapters import ConnectorProviderError, NormalizedChange, adapter_for
from src.domain.events import event_bus
from src.domain.source_extraction import extract_source_markdown, extract_source_pages
from src.domain.source_storage import save_source
from src.models.article import Article, DocumentSource
from src.models.user import AccessGroup
from src.models.connectors import DocumentVersion, ExternalAclPrincipal, ExternalDocument, ExternalGroupMapping, PermissionSnapshot, SourceScope, SyncCursor, SyncError
from src.models.governance import PendingDraft
from src.models.ops import Connector, ConnectorJob


def _acl_hash(permissions: list[dict[str, str]]) -> str:
    return hashlib.sha256(json.dumps(sorted(permissions, key=lambda item: (item.get("principal_type", ""), item.get("principal_id", ""))), sort_keys=True).encode("utf-8")).hexdigest()


async def _upsert_document(db: AsyncSession, connector: Connector, scope: SourceScope, change: NormalizedChange) -> ExternalDocument:
    document = (await db.execute(select(ExternalDocument).where(ExternalDocument.connector_id == connector.id, ExternalDocument.corpus_id == change.corpus_id, ExternalDocument.external_id == change.external_id))).scalar_one_or_none()
    if document is None:
        document = ExternalDocument(connector_id=connector.id, scope_id=scope.id, corpus_id=change.corpus_id, external_id=change.external_id, name=change.name)
        db.add(document)
        await db.flush()
    document.scope_id = scope.id
    document.name = change.name
    document.parent_external_id = change.parent_external_id
    document.mime_type = change.mime_type
    document.web_url = change.web_url
    document.revision = change.revision
    document.metadata_json = change.metadata
    document.state = change.state
    return document


async def _save_permissions(db: AsyncSession, connector: Connector, document: ExternalDocument, permissions: list[dict[str, str]]) -> bool:
    acl_hash = _acl_hash(permissions)
    if document.acl_hash == acl_hash:
        return False
    await db.execute(PermissionSnapshot.__table__.update().where(PermissionSnapshot.external_document_id == document.id).values(active=False))
    snapshot = PermissionSnapshot(external_document_id=document.id, acl_hash=acl_hash, permissions_json=permissions, active=True)
    db.add(snapshot)
    await db.flush()
    for item in permissions:
        db.add(ExternalAclPrincipal(permission_snapshot_id=snapshot.id, principal_type=item.get("principal_type", "user"), principal_id=item.get("principal_id", ""), role=item.get("role", "reader")))
    document.acl_hash = acl_hash
    mappings = (await db.execute(select(ExternalGroupMapping).where(ExternalGroupMapping.connector_id == connector.id, ExternalGroupMapping.active.is_(True), ExternalGroupMapping.external_group_id.in_([item.get("principal_id", "") for item in permissions if item.get("principal_type") == "group"])))).scalars().all()
    document.metadata_json = {**(document.metadata_json or {}), "mapped_access_group_ids": [str(item.access_group_id) for item in mappings], "unmapped_group_ids": [item.get("principal_id") for item in permissions if item.get("principal_type") == "group" and item.get("principal_id") not in {mapping.external_group_id for mapping in mappings}]}
    return True


async def _apply_mapped_groups(db: AsyncSession, connector: Connector, document: ExternalDocument) -> None:
    if not document.article_id:
        return
    article = await db.get(Article, document.article_id)
    if not article:
        return
    mapped_ids = (document.metadata_json or {}).get("mapped_access_group_ids", [])
    if mapped_ids:
        article.access_groups = list((await db.execute(select(AccessGroup).where(AccessGroup.id.in_(mapped_ids)))).scalars().all())
    else:
        # Fail closed for a restricted cloud document whose provider group is
        # not mapped locally.  Public documents remain public by policy.
        if article.sensitivity != "public":
            article.access_groups = []
    await db.flush()


async def _ingest_content(db: AsyncSession, connector: Connector, document: ExternalDocument, change: NormalizedChange, job: ConnectorJob) -> None:
    adapter = adapter_for(connector)
    data = await adapter.download(change)
    content_hash = hashlib.sha256(data).hexdigest()
    if document.content_hash == content_hash and document.revision == change.revision:
        return
    pages = await asyncio.to_thread(extract_source_pages, change.name, data)
    text = await asyncio.to_thread(extract_source_markdown, change.name, data, pages)
    storage_key = await asyncio.to_thread(save_source, content_hash, change.name, data, connector.company_domain)
    document.content_hash = content_hash
    document.revision = change.revision
    document.state = "active"
    version = (await db.execute(select(DocumentVersion).where(DocumentVersion.external_document_id == document.id, DocumentVersion.revision == (change.revision or content_hash)))).scalar_one_or_none()
    if version is None:
        version = DocumentVersion(external_document_id=document.id, revision=change.revision or content_hash, content_hash=content_hash, storage_key=storage_key, parser_version="source-extraction-v1", chunker_version="parent-child-v1", status="ready")
        db.add(version)
    if document.article_id:
        article = (await db.execute(
            select(Article).where(Article.id == document.article_id).options(selectinload(Article.access_groups))
        )).scalar_one_or_none()
        if article and article.lifecycle_status == "active":
            # Connector content is external input and must pass the same
            # independent approval path as a manually submitted revision.
            existing = (await db.execute(select(PendingDraft).where(
                PendingDraft.external_document_id == document.id,
                PendingDraft.status == "pending",
            ))).scalar_one_or_none()
            metadata = {
                "domain": article.domain,
                "type": article.type,
                "sensitivity": article.sensitivity,
                "language": article.language,
                "access_group_ids": [str(group.id) for group in article.access_groups],
                "submission_kind": "connector_update",
                "suggested_update_article_id": str(article.id),
            }
            if existing is None:
                db.add(PendingDraft(
                    title=change.name.rsplit(".", 1)[0][:255],
                    company_domain=connector.company_domain,
                    dept=article.dept,
                    source_ref=f"{connector.system}://{change.corpus_id}/{change.external_id}",
                    source_hash=content_hash,
                    summary=text,
                    restructured_body_md=text,
                    restructure_status="not_requested",
                    storage_key=storage_key,
                    original_filename=change.name,
                    mime_type=change.mime_type,
                    page_texts=pages,
                    status="pending",
                    created_by=connector.created_by,
                    external_document_id=document.id,
                    update_target_article_id=article.id,
                    content_metadata=metadata,
                ))
            else:
                existing.title = change.name.rsplit(".", 1)[0][:255]
                existing.source_hash = content_hash
                existing.summary = text
                existing.restructured_body_md = text
                existing.storage_key = storage_key
                existing.page_texts = pages
                existing.original_filename = change.name
                existing.content_metadata = metadata
            return
    existing = (await db.execute(select(PendingDraft).where(PendingDraft.external_document_id == document.id, PendingDraft.status == "pending"))).scalar_one_or_none()
    if existing is None:
        db.add(PendingDraft(title=change.name.rsplit(".", 1)[0][:255], company_domain=connector.company_domain, source_ref=f"{connector.system}://{change.corpus_id}/{change.external_id}", source_hash=content_hash, summary=text, storage_key=storage_key, original_filename=change.name, mime_type=change.mime_type, page_texts=pages, status="pending", created_by=connector.created_by, external_document_id=document.id))
    else:
        existing.source_hash = content_hash
        existing.summary = text
        existing.storage_key = storage_key
        existing.page_texts = pages
        existing.original_filename = change.name


async def sync_cloud_connector(db: AsyncSession, connector: Connector, job: ConnectorJob) -> None:
    adapter = adapter_for(connector)
    if connector.oauth_expires_at and connector.oauth_expires_at <= datetime.utcnow():
        tokens = await adapter.refresh_token()
        connector.oauth_access_token = encrypt_secret(tokens.get("access_token"))
        connector.oauth_refresh_token = encrypt_secret(tokens.get("refresh_token")) or connector.oauth_refresh_token
        connector.oauth_expires_at = datetime.utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)))
        await db.commit()
    scopes = (await db.execute(select(SourceScope).where(SourceScope.connector_id == connector.id, SourceScope.selected.is_(True)))).scalars().all()
    if not scopes:
        raise ConnectorProviderError("No connector scopes are selected", retryable=False, code="no_scopes")
    job.status = "running"
    job.attempts += 1
    await db.commit()
    try:
        for scope in scopes:
            cursor_row = (await db.execute(select(SyncCursor).where(SyncCursor.connector_id == connector.id, SyncCursor.scope_id == scope.id))).scalar_one_or_none()
            cursor = cursor_row.cursor_value if cursor_row else None
            changes, next_cursor = await adapter.incremental_changes({"external_scope_id": scope.external_scope_id, "config": scope.config_json or {}}, cursor)
            for change in changes:
                previous = (await db.execute(select(ExternalDocument).where(ExternalDocument.connector_id == connector.id, ExternalDocument.corpus_id == change.corpus_id, ExternalDocument.external_id == change.external_id))).scalar_one_or_none()
                previous_revision = previous.revision if previous else None
                document = await _upsert_document(db, connector, scope, change)
                permissions = [] if change.state == "deleted" else await adapter.permissions(change)
                acl_changed = await _save_permissions(db, connector, document, permissions) if change.state != "deleted" else False
                if change.state == "deleted":
                    document.state = "deleted"
                    if document.article_id:
                        article = await db.get(Article, document.article_id)
                        if article:
                            article.lifecycle_status = "inactive"
                            await db.commit()
                            await event_bus.publish("ArticleDeleted", {"article_id": str(article.id)})
                elif change.content_changed and (previous is None or previous_revision != change.revision or not document.content_hash):
                    await _ingest_content(db, connector, document, change, job)
                if acl_changed and document.article_id:
                    await _apply_mapped_groups(db, connector, document)
                    await db.commit()
                    await event_bus.publish("PermissionChanged", {"article_id": str(document.article_id)})
            if cursor_row is None:
                cursor_row = SyncCursor(connector_id=connector.id, scope_id=scope.id, cursor_type="delta" if connector.system == "sharepoint" else "changes")
                db.add(cursor_row)
            cursor_row.cursor_value = next_cursor or cursor_row.cursor_value
            cursor_row.last_success_at = datetime.utcnow()
            await db.commit()
        connector.last_sync = datetime.utcnow()
        connector.status = "active"
        connector.last_error = None
        job.status = "completed"
        job.completed_at = datetime.utcnow()
        await db.commit()
    except Exception as exc:
        connector.status = "error"
        connector.last_error = str(exc)[:2000]
        job.status = "failed"
        job.last_error = str(exc)[:2000]
        db.add(SyncError(
            connector_id=connector.id,
            job_id=job.id,
            stage="sync",
            error_code=getattr(exc, "code", None),
            message=str(exc)[:4000],
            retryable=bool(getattr(exc, "retryable", True)),
            attempts=job.attempts,
        ))
        await db.commit()
        raise
