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


def normalize_query(query: str) -> str:
    """Remove low-signal question words before keyword/vector retrieval."""
    tokens = [
        token for token in re.findall(r"[\w'-]+", (query or "").lower())
        if len(token) > 1 and token not in STOPWORDS
    ]
    return " ".join(tokens) or (query or "").strip()


def rerank_chunks(query: str, chunks: Sequence[object], limit: int = 5) -> list[object]:
    normalized_query = normalize_query(query)
    terms = set(re.findall(r"[\w'-]+", normalized_query))
    scored: list[tuple[float, int, object]] = []
    for position, chunk in enumerate(chunks):
        article = getattr(chunk, "article", None)
        parent = getattr(chunk, "parent_chunk", None)
        text = " ".join(
            str(value or "") for value in (
                getattr(chunk, "chunk_text", ""),
                getattr(parent, "text", ""),
                getattr(article, "title", ""),
            )
        ).lower()
        matched = sum(1 for term in terms if term in text)
        coverage = matched / max(len(terms), 1)
        # Exact phrase and exact token matches are strong evidence that the
        # passage answers the current question. Phrase matching is checked
        # against normalized whitespace so punctuation/newlines do not break
        # it. The position remains a tie-breaker for otherwise equal results.
        normalized_text = " ".join(re.findall(r"[\w'-]+", text))
        phrase_bonus = 0.35 if len(normalized_query) > 2 and normalized_query in normalized_text else 0.0
        exact_term_bonus = sum(
            0.08 for term in terms
            if re.search(rf"(?<![\w'-]){re.escape(term)}(?![\w'-])", normalized_text)
        )
        # Preserve vector/RRF ordering as a tie-breaker while rewarding exact
        # lexical support for the user's wording.
        scored.append((coverage + phrase_bonus + exact_term_bonus, -position, chunk))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored[:limit]]
