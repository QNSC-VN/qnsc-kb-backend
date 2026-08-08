"""Small deterministic reranker used when a cross-encoder is unavailable."""
from __future__ import annotations

import re
from typing import Sequence


# These words add little retrieval signal. Keeping them out of lexical
# coverage prevents a query such as "What is CTS?" from ranking generic text
# containing "what/is" above the passage containing the important term CTS.
STOPWORDS = {
    "a", "an", "and", "are", "be", "by", "can", "do", "for", "from", "how",
    "i", "in", "is", "it", "of", "on", "or", "the", "to", "what", "when",
    "where", "which", "who", "why", "with", "you", "your",
    "là", "và", "có", "cho", "của", "để", "gì", "nào", "như", "về", "tôi",
}

REFERENCE_MARKERS = (
    "references", "reference", "helpful documents", "sources", "bibliography",
    "tài liệu tham khảo", "nguồn tham khảo", "http://", "https://", "www.",
)

DEFINITION_QUERY_MARKERS = (
    "what is", "what are", "define", "definition", "meaning", "explain",
    "là gì", "định nghĩa", "có nghĩa là", "giải thích", "khái niệm",
)

DEFINITION_PATTERNS = (
    r"\b(?:is|are|means|refers to|defined as|describes)\b",
    r"\b(?:là|được gọi là|có nghĩa là|dùng để chỉ|được định nghĩa là)\b",
)


def normalize_query(query: str) -> str:
    """Remove low-signal question words before keyword/vector retrieval."""
    tokens = [
        token for token in re.findall(r"[\w'-]+", (query or "").lower())
        if len(token) > 1 and token not in STOPWORDS
    ]
    # An all-stopword input has no retrieval signal. Returning the original
    # query here caused generic words such as "what is" to retrieve arbitrary
    # documents through vector similarity.
    return " ".join(tokens)


def is_definition_query(query: str) -> bool:
    normalized = " ".join((query or "").lower().split())
    return any(marker in normalized for marker in DEFINITION_QUERY_MARKERS)


def score_retrieval_text(query: str, text: str, title: str = "", section: str = "") -> float:
    """Score how well a passage answers the query, not just contains its terms."""
    normalized_query = normalize_query(query)
    terms = set(re.findall(r"[\w'-]+", normalized_query.lower()))
    passage = " ".join(str(value or "") for value in (text, title, section)).lower()
    normalized_text = " ".join(re.findall(r"[\w'-]+", passage))
    matched = sum(1 for term in terms if re.search(rf"(?<![\w'-]){re.escape(term)}(?![\w'-])", normalized_text))
    score = matched / max(len(terms), 1)
    if len(normalized_query) > 2 and " ".join(re.findall(r"[\w'-]+", normalized_query.lower())) in normalized_text:
        score += 0.35

    if is_definition_query(query):
        if any(re.search(pattern, passage, re.IGNORECASE) for pattern in DEFINITION_PATTERNS):
            score += 0.75
        if any(marker in passage for marker in REFERENCE_MARKERS):
            score -= 1.0
        # A passage dominated by URLs is reference material, even when it
        # repeats the subject name many times.
        if len(re.findall(r"https?://|www\.", passage)) >= 2:
            score -= 0.5
    elif any(marker in passage for marker in REFERENCE_MARKERS):
        score -= 0.25
    return score


def rerank_chunks(query: str, chunks: Sequence[object], limit: int = 5) -> list[object]:
    scored: list[tuple[float, int, object]] = []
    for position, chunk in enumerate(chunks):
        article = getattr(chunk, "article", None)
        parent = getattr(chunk, "parent_chunk", None)
        # The child passage is the precise retrieval unit. Parent text is only
        # a fallback because it often contains repeated slides/references.
        text = getattr(chunk, "chunk_text", "") or getattr(parent, "text", "")
        score = score_retrieval_text(
            query,
            text,
            getattr(article, "title", ""),
            getattr(parent, "section_ref", "") if parent else "",
        )
        scored.append((score, -position, chunk))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored[:limit]]
