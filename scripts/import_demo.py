"""Extract the static demo's article catalogue into importable QNSC seed JSON.

Usage: python scripts/import_demo.py C:\\Users\\admin\\Downloads\\qnsc-knowledge-base.html
The output is deliberately JSON so the normal seed/import command can validate and
persist it without executing arbitrary JavaScript from the demo file.
"""
import argparse
import ast
import json
import re
from pathlib import Path


def _objects(source: str) -> list[str]:
    start = source.index("const ARTICLES")
    start = source.index("[", start)
    result, depth, quote, escaped, begin = [], 0, None, False, None
    for index in range(start + 1, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            if depth == 0:
                begin = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and begin is not None:
                result.append(source[begin:index + 1])
                begin = None
        elif char == "]" and depth == 0:
            break
    return result


def _value(block: str, name: str) -> str | None:
    match = re.search(rf"\b{name}\s*:\s*(['\"])(.*?)\1", block, re.S)
    return match.group(2).strip() if match else None


def _array(block: str, name: str) -> list[str]:
    match = re.search(rf"\b{name}\s*:\s*\[([^\]]*)\]", block, re.S)
    return re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)) if match else []


def extract(html: str) -> list[dict]:
    records = []
    for block in _objects(html):
        article_id = _value(block, "id")
        if not article_id:
            continue
        body = "\n\n".join(re.findall(r"(['\"])(.*?)\1", block[block.find("body:"):], re.S) and [item[1] for item in re.findall(r"(['\"])(.*?)\1", block[block.find("body:"):], re.S)][:80])
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
        records.append({
            "external_id": article_id, "dept": _value(block, "dept"), "domain": _value(block, "domain"),
            "type": _value(block, "type"), "title": _value(block, "title"), "tags": _array(block, "tags"),
            "status": _value(block, "status") or "published", "sensitivity": _value(block, "sensitivity") or "internal",
            "owner": _value(block, "owner"), "reviewers": _array(block, "reviewers"), "related": _array(block, "related"),
            "created": _value(block, "created"), "last_reviewed": _value(block, "lastReviewed"),
            "next_review": _value(block, "nextReview"), "summary": _value(block, "summary"), "body_text": body,
        })
    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("html", type=Path)
    parser.add_argument("--output", type=Path, default=Path("seed/demo_articles.json"))
    args = parser.parse_args()
    payload = {"source": str(args.html), "articles": extract(args.html.read_text(encoding="utf-8"))}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Extracted {len(payload['articles'])} demo articles to {args.output}")


if __name__ == "__main__":
    main()
