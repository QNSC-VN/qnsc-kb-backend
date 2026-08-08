"""Source storage with local and S3-compatible backends."""
from __future__ import annotations

import re
import uuid
from typing import Any
from pathlib import Path

from src.core.config import settings

_SAFE_SOURCE_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _safe_name(filename: str) -> str:
    name = Path(filename).name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "source.bin"


def safe_source_media_type(filename: str | None) -> str:
    """Return an allow-listed type for an original source response.

    Multipart ``Content-Type`` is supplied by the uploader. Never reuse it
    for a browser response: an HTML type would execute in the application's
    origin when a reviewer opens the original source.
    """
    return _SAFE_SOURCE_MEDIA_TYPES.get(Path(filename or "").suffix.lower(), "application/octet-stream")


def source_should_display_inline(filename: str | None) -> bool:
    return safe_source_media_type(filename) != "application/octet-stream"


def _is_object_storage() -> bool:
    return settings.SOURCE_STORAGE_BACKEND.lower() in {"s3", "object", "object_storage", "r2", "cloudflare_r2"}


def _safe_namespace(company_domain: str | None) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", (company_domain or "local").strip().lower())
    return value[:120] or "local"


def save_source(source_hash: str, filename: str, data: bytes, company_domain: str | None = None) -> str:
    """Store a source privately; callers expose it only through authorized API routes."""
    if _is_object_storage():
        return _s3_put(source_hash, filename, data, company_domain)
    root = Path(settings.SOURCE_STORAGE_PATH).resolve()
    root.mkdir(parents=True, exist_ok=True)
    key = f"{_safe_namespace(company_domain)}/{uuid.uuid4().hex}/{source_hash}_{_safe_name(filename)}"
    target = (root / key).resolve()
    if root not in target.parents:
        raise ValueError("Invalid source storage path")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return key


def source_path(storage_key: str) -> Path:
    if storage_key.startswith("s3://"):
        raise FileNotFoundError("S3-backed source has no local filesystem path")
    root = Path(settings.SOURCE_STORAGE_PATH).resolve()
    target = (root / storage_key).resolve()
    if root not in target.parents:
        raise FileNotFoundError("Invalid source storage key")
    return target


def delete_source(storage_key: str) -> None:
    """Best-effort deletion for rejected drafts and failed transactions."""
    if storage_key.startswith("s3://"):
        prefix = "s3://"
        bucket, _, key = storage_key[len(prefix):].partition("/")
        expected_prefix = f"{settings.SOURCE_STORAGE_PREFIX.strip('/')}/"
        if bucket != settings.SOURCE_STORAGE_BUCKET or not key.startswith(expected_prefix):
            raise FileNotFoundError("Invalid S3 source key")
        _s3_client().delete_object(Bucket=bucket, Key=key)
        return
    path = source_path(storage_key)
    if path.exists():
        path.unlink()


def _s3_client() -> Any:
    if not settings.SOURCE_STORAGE_BUCKET:
        raise RuntimeError("SOURCE_STORAGE_BUCKET is required for object storage")
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is required for S3-backed source storage") from exc
    endpoint_url = settings.S3_ENDPOINT_URL
    if not endpoint_url and settings.R2_ACCOUNT_ID:
        account_or_endpoint = settings.R2_ACCOUNT_ID.strip().rstrip("/")
        # Accept the dashboard's account ID as well as a full endpoint pasted
        # into the setting.  The latter is common in local .env files.
        endpoint_url = account_or_endpoint if account_or_endpoint.startswith(("https://", "http://")) else f"https://{account_or_endpoint}.r2.cloudflarestorage.com"
    kwargs: dict[str, Any] = {"region_name": settings.AWS_REGION or "auto", "endpoint_url": endpoint_url}
    if settings.R2_ACCESS_KEY_ID and settings.R2_SECRET_ACCESS_KEY:
        kwargs.update(aws_access_key_id=settings.R2_ACCESS_KEY_ID, aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY)
    return boto3.client("s3", **kwargs)


def _s3_key(source_hash: str, filename: str, company_domain: str | None = None) -> str:
    # A tenant prefix and random segment avoid predictable object names.  The
    # database hash is still the authoritative duplicate check.
    return f"{settings.SOURCE_STORAGE_PREFIX.strip('/')}/{_safe_namespace(company_domain)}/{uuid.uuid4().hex}/{source_hash}_{_safe_name(filename)}"


def _s3_put(source_hash: str, filename: str, data: bytes, company_domain: str | None = None) -> str:
    key = _s3_key(source_hash, filename, company_domain)
    _s3_client().put_object(
        Bucket=settings.SOURCE_STORAGE_BUCKET,
        Key=key,
        Body=data,
        ContentType="application/octet-stream",
        Metadata={"sha256": source_hash},
    )
    return f"s3://{settings.SOURCE_STORAGE_BUCKET}/{key}"


def load_source(storage_key: str) -> bytes:
    if storage_key.startswith("s3://"):
        prefix = "s3://"
        bucket, _, key = storage_key[len(prefix):].partition("/")
        expected_prefix = f"{settings.SOURCE_STORAGE_PREFIX.strip('/')}/"
        if not bucket or not key or bucket != settings.SOURCE_STORAGE_BUCKET or not key.startswith(expected_prefix):
            raise FileNotFoundError("Invalid S3 source key")
        response = _s3_client().get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
    return source_path(storage_key).read_bytes()
