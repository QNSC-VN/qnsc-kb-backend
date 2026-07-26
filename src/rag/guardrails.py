"""Reusable lightweight prompt-injection and output safety checks."""
from __future__ import annotations

import re

_INPUT_BLOCKLIST = (
    "ignore previous instructions", "ignore system prompt", "system instructions",
    "override restrictions", "bypass system", "reveal system prompt",
)
_SENSITIVE_OUTPUT = (r"password\s*=\s*", r"api_key\s*=\s*", r"secret_key\s*=\s*", r"db_password")


def is_safe_question(question: str) -> bool:
    lowered = question.lower()
    return not any(phrase in lowered for phrase in _INPUT_BLOCKLIST)


def is_safe_answer(answer: str) -> bool:
    return not any(re.search(pattern, answer, re.IGNORECASE) for pattern in _SENSITIVE_OUTPUT)
