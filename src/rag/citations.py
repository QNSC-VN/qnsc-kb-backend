"""Citation parsing compatible with DocNexus and legacy QNSC answers."""
import re


def extract_citation_indices(answer: str) -> list[int]:
    matches = re.findall(r"\[Source ID:\s*(\d+)\]|\[(\d+)\]", answer or "")
    return sorted({int(first or second) for first, second in matches})


def extract_citation_ids(answer: str) -> list[str]:
    """Extract backend-issued citation IDs, accepting legacy numeric markers."""
    matches = re.findall(r"\[Source ID:\s*(C?\d+)\]|\[(C?\d+)\]", answer or "", re.IGNORECASE)
    ids: set[str] = set()
    for first, second in matches:
        value = (first or second).upper()
        ids.add(value if value.startswith("C") else f"C{value}")
    return sorted(ids, key=lambda value: int(value[1:]))
