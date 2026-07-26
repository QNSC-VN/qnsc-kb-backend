"""Citation parsing compatible with DocNexus and legacy QNSC answers."""
import re


def extract_citation_indices(answer: str) -> list[int]:
    matches = re.findall(r"\[Source ID:\s*(\d+)\]|\[(\d+)\]", answer or "")
    return sorted({int(first or second) for first, second in matches})
