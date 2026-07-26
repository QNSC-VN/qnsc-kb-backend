"""Private local source storage used by the document review viewer."""
from __future__ import annotations

import re
from pathlib import Path

from src.core.config import settings


def _safe_name(filename: str) -> str:
    name = Path(filename).name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "source.bin"


def save_source(source_hash: str, filename: str, data: bytes) -> str:
    root = Path(settings.SOURCE_STORAGE_PATH).resolve()
    root.mkdir(parents=True, exist_ok=True)
    key = f"{source_hash}_{_safe_name(filename)}"
    target = (root / key).resolve()
    if root not in target.parents:
        raise ValueError("Invalid source storage path")
    target.write_bytes(data)
    return key


def source_path(storage_key: str) -> Path:
    root = Path(settings.SOURCE_STORAGE_PATH).resolve()
    target = (root / Path(storage_key).name).resolve()
    if root not in target.parents:
        raise FileNotFoundError("Invalid source storage key")
    return target
