"""Parsing and rendering for grounded and general-knowledge answer sections."""
from __future__ import annotations

import re

GROUNDED_SENTINEL = "<<<GROUNDED>>>"
EXTENDED_SENTINEL = "<<<EXTENDED>>>"
GROUNDED_HEADING = "## Answer from the Knowledge Base"
EXTENDED_HEADING = "## Additional context (general knowledge — not from the Knowledge Base, not cited)"

_SENTINEL_LINE_RE = re.compile(r"^\s*<<<(GROUNDED|EXTENDED)>>>\s*$", re.IGNORECASE)
_CITATION_MARKER_RE = re.compile(r"\[(?:Source ID:\s*)?C?\d+\]", re.IGNORECASE)


def _sentinel_lines(text: str) -> list[tuple[str, int, int]]:
    lines = text.splitlines(keepends=True)
    found: list[tuple[str, int, int]] = []
    offset = 0
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            match = _SENTINEL_LINE_RE.match(line.rstrip("\r\n"))
            if match:
                found.append((match.group(1).upper(), offset, offset + len(line)))
        offset += len(line)
    return found


def _strip_sentinel_lines(text: str) -> str:
    kept: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            kept.append(line)
        elif in_fence or not _SENTINEL_LINE_RE.match(line):
            kept.append(line)
    return "\n".join(kept)


def normalize_answer_markdown(answer: str) -> str:
    """Remove malformed fence/citation fragments without changing content."""
    value = (answer or "").strip()
    citation_markers = r"((?:\[(?:Source ID:\s*)?C?\d+\]\s*)+)"
    value = re.sub(
        rf"(?m)^([ \t]*```)[ \t]+{citation_markers}$",
        r"\1\n\n\2",
        value,
    )
    value = re.sub(r"```[ \t]*\[[ \t]*$", "```", value)
    value = re.sub(r"(?m)^[ \t]*\[[ \t]*$", "", value)
    return value.strip()


def split_answer_sections(raw: str) -> tuple[str, str]:
    """Return normalized grounded and extended sections.

    Unmarked output is deliberately treated as grounded. Sentinel-like text in
    fenced code blocks is content, not a section boundary.
    """
    text = raw or ""
    markers = _sentinel_lines(text)
    grounded_marker = next((item for item in markers if item[0] == "GROUNDED"), None)
    if grounded_marker is None:
        return normalize_answer_markdown(_strip_sentinel_lines(text)), ""

    extended_marker = next(
        (item for item in markers if item[0] == "EXTENDED" and item[1] >= grounded_marker[2]),
        None,
    )
    prefix = text[:grounded_marker[1]]
    if extended_marker:
        grounded_raw = prefix + text[grounded_marker[2]:extended_marker[1]]
        extended_raw = text[extended_marker[2]:]
    else:
        grounded_raw = prefix + text[grounded_marker[2]:]
        extended_raw = ""
    return (
        normalize_answer_markdown(_strip_sentinel_lines(grounded_raw)),
        normalize_answer_markdown(_strip_sentinel_lines(extended_raw)),
    )


def strip_citation_markers(text: str) -> str:
    """Remove source markers from the non-grounded section."""
    cleaned = _CITATION_MARKER_RE.sub("", text or "")
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def render_answer_sections(grounded: str, extended: str, enabled: bool) -> str:
    """Build the copy-safe combined answer returned by the API/UI."""
    grounded = normalize_answer_markdown(grounded)
    extended = normalize_answer_markdown(extended)
    if not enabled:
        return grounded
    rendered = f"{GROUNDED_HEADING}\n\n{grounded}" if grounded else ""
    if extended:
        rendered += f"\n\n---\n\n{EXTENDED_HEADING}\n\n{extended}"
    return rendered.strip()
