"""Deterministic context compression before prompt assembly."""


def compress_context(text: str, max_characters: int = 4000) -> str:
    text = (text or "").strip()
    if len(text) <= max_characters:
        return text
    clipped = text[:max_characters]
    last_break = max(clipped.rfind("\n"), clipped.rfind(". "))
    end = last_break + 1 if last_break > max_characters // 2 else max_characters
    return clipped[:end].rstrip() + " …"
