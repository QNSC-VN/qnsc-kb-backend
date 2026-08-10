"""Deterministic structure-aware document splitting for review candidates."""
from __future__ import annotations

import re
from typing import Any

from src.rag.chunker import create_parent_child_chunks


def _candidate_title(body: str, index: int, fallback: str) -> tuple[str, str | None]:
    for line in body.splitlines():
        clean = line.strip()
        if re.match(r"^#{1,6}\s+\S", clean):
            return clean.lstrip("#").strip()[:255] or f"{fallback} — part {index}", clean.lstrip("#").strip()[:255]
    return f"{fallback} — part {index}"[:255], None


def split_document_candidates(title: str, text: str) -> list[dict[str, Any]]:
    """Return ordered candidates with source character offsets.

    Headings are preserved by the parent/child chunker.  The 1,800 character
    parent boundary and 500 character child boundary are the recorded MVP
    split rule; candidate bodies remain lossless source slices.
    """
    clean_text = text.strip()
    if not clean_text:
        return []
    parents = create_parent_child_chunks(clean_text)
    candidates: list[dict[str, Any]] = []
    cursor = 0
    for index, parent in enumerate(parents, start=1):
        body = str(parent.get("parent_text") or "").strip()
        if not body:
            continue
        start = clean_text.find(body, cursor)
        if start < 0:
            start = cursor
        end = start + len(body)
        cursor = end
        candidate_title, heading = _candidate_title(body, index, title)
        candidates.append({
            "position": index,
            "title": candidate_title,
            "body_md": body,
            "source_start": start,
            "source_end": end,
            "heading": heading or parent.get("heading"),
        })
    return candidates or [{
        "position": 1,
        "title": title[:255],
        "body_md": clean_text,
        "source_start": 0,
        "source_end": len(clean_text),
        "heading": None,
    }]


def splitter_metrics(documents: list[tuple[str, str]]) -> dict[str, Any]:
    """Produce a reproducible pilot report without an LLM call."""
    rows = []
    manual_correction_count = 0
    article_count = 0
    for name, text in documents:
        candidates = split_document_candidates(name, text)
        article_count += len(candidates)
        candidate_text = "\n".join(str(item["body_md"]) for item in candidates)
        original_headings = [line.strip().lstrip("#").strip() for line in text.splitlines() if re.match(r"^#{1,6}\s+\S", line.strip())]
        preserved = all(heading in candidate_text for heading in original_headings)
        if not preserved:
            manual_correction_count += 1
        rows.append({"document": name, "article_count": len(candidates), "headings_preserved": preserved})
    count = len(documents)
    return {
        "document_count": count,
        "article_count": article_count,
        "manual_correction_count": manual_correction_count,
        "manual_correction_share": (manual_correction_count / count) if count else 0.0,
        "rule": "hybrid heading-aware parent chunks ~1800 characters with ~500-character children; short sections merge when bounded packing preserves order",
        "documents": rows,
    }
