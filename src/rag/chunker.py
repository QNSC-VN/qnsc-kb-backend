"""Sentence-aware parent/child chunking used by the QNSC indexer."""
from __future__ import annotations

import re


def _split_units(text: str) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        units.extend([part.strip() for part in re.split(r"(?<=[.!?])\s+", paragraph) if part.strip()])
    return units


def sliding_chunks(text: str, size: int = 1500, overlap: int = 200) -> list[str]:
    clean = text.strip()
    if not clean:
        return []
    chunks: list[str] = []
    current = ""
    for unit in _split_units(clean):
        candidate = f"{current} {unit}".strip()
        if current and len(candidate) > size:
            chunks.append(current)
            tail = current[-overlap:]
            boundary = tail.find(" ")
            current = f"{tail[boundary + 1:] if boundary >= 0 else tail} {unit}".strip()
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def create_parent_child_chunks(text: str) -> list[dict[str, object]]:
    return [
        {
            "parent_index": index,
            "parent_text": parent,
            "children": sliding_chunks(parent, size=500, overlap=100),
        }
        for index, parent in enumerate(sliding_chunks(text, size=1800, overlap=250))
    ]
