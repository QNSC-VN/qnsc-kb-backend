"""Run the offline RAG regression gate.

Each case must contain ``answer``, ``expected_answer``, ``context`` and may
contain ``retrieved_chunk_ids``/``expected_chunk_ids``.  The command exits
non-zero when any configured score falls below its threshold, which makes it
safe to call from CI without requiring a live LLM or database.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.rag.evaluator import answer_correctness, context_recall, lexical_faithfulness


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the offline QNSC RAG regression gate")
    parser.add_argument("--input", type=Path, required=True, help="JSON file containing evaluation cases")
    parser.add_argument("--min-context-recall", type=float, default=0.80)
    parser.add_argument("--min-faithfulness", type=float, default=0.80)
    parser.add_argument("--min-correctness", type=float, default=0.80)
    return parser.parse_args()


def run(cases: list[dict]) -> dict:
    results = []
    for index, case in enumerate(cases, start=1):
        answer = str(case.get("answer", ""))
        expected_answer = str(case.get("expected_answer", ""))
        context = str(case.get("context", ""))
        result = {
            "case": case.get("id", index),
            "context_recall": context_recall(case.get("retrieved_chunk_ids", []), case.get("expected_chunk_ids", [])),
            "faithfulness": lexical_faithfulness(answer, context),
            "answer_correctness": answer_correctness(answer, expected_answer),
        }
        results.append(result)

    if not results:
        raise ValueError("Evaluation input contains no cases")

    return {
        "case_count": len(results),
        "averages": {
            metric: sum(item[metric] for item in results) / len(results)
            for metric in ("context_recall", "faithfulness", "answer_correctness")
        },
        "results": results,
    }


def main() -> int:
    args = parse_args()
    cases = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("Evaluation input must be a JSON array")
    report = run(cases)
    print(json.dumps(report, indent=2, sort_keys=True))

    averages = report["averages"]
    failures = {
        "context_recall": averages["context_recall"] < args.min_context_recall,
        "faithfulness": averages["faithfulness"] < args.min_faithfulness,
        "answer_correctness": averages["answer_correctness"] < args.min_correctness,
    }
    if any(failures.values()):
        print(f"Regression gate failed: {failures}", file=sys.stderr)
        return 1
    print("Regression gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
