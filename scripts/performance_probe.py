"""Measure authenticated search and AI p50/p95 latency on a running stack.

Credentials are read only from ``PERF_TEST_EMAIL`` and
``PERF_TEST_PASSWORD``. The report marks AI measurements provider-backed only
when the authenticated runtime reports an enabled provider with a configured
API key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import time
from collections import Counter
from typing import Any, Awaitable, Callable

import httpx


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def provider_is_configured(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("configured")
        and payload.get("enabled")
        and payload.get("api_key_configured")
    )


def provider_gate_failed(report: dict[str, Any], required: bool) -> bool:
    return required and report.get("provider_backed") is not True


def benchmark_question(question: str, index: int, unique: bool) -> str:
    """Return a cache-miss variant without changing normalized retrieval text."""
    if not unique:
        return question
    separator = question.find(" ")
    if separator < 0:
        return question
    # Search normalization collapses whitespace, while the AI cache hashes the
    # raw, stripped question. This exercises the provider without adding
    # benchmark words that can lower retrieval confidence.
    return f"{question[:separator]}{' ' * (index + 2)}{question[separator + 1:]}"


async def measure(
    request: Callable[[int], Awaitable[httpx.Response]],
    *,
    requests: int,
    concurrency: int,
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    durations: list[float] = []
    statuses: Counter[str] = Counter()

    async def one(index: int) -> None:
        async with semaphore:
            started = time.perf_counter()
            try:
                response = await request(index)
                statuses[str(response.status_code)] += 1
            except httpx.HTTPError:
                statuses["network_error"] += 1
            durations.append((time.perf_counter() - started) * 1000)

    await asyncio.gather(*(one(index) for index in range(requests)))
    failures = sum(count for status, count in statuses.items() if status != "200")
    return {
        "requests": requests,
        "concurrency": max(1, concurrency),
        "failures": failures,
        "status_counts": dict(sorted(statuses.items())),
        "p50_ms": round(percentile(durations, 0.50), 2),
        "p95_ms": round(percentile(durations, 0.95), 2),
        "average_ms": round(statistics.fmean(durations), 2) if durations else 0.0,
    }


async def run(
    *,
    base_url: str,
    email: str,
    password: str,
    search_query: str,
    ai_question: str,
    requests: int,
    concurrency: int,
    warmup: int,
    unique_ai_questions: bool,
) -> dict[str, Any]:
    async with httpx.AsyncClient(
        base_url=base_url, timeout=90.0, trust_env=False
    ) as client:
        login = await client.post(
            "/api/v1/auth/login", data={"username": email, "password": password}
        )
        if login.status_code != 200:
            raise RuntimeError(f"login failed with HTTP {login.status_code}")
        token = str(login.json()["access_token"])
        headers = {"Authorization": f"Bearer {token}"}

        async def search_request(_index: int) -> httpx.Response:
            return await client.get(
                "/api/v1/search",
                headers=headers,
                params={"q": search_query, "limit": 10},
            )

        async def ai_request(index: int) -> httpx.Response:
            question = benchmark_question(ai_question, index, unique_ai_questions)
            return await client.post(
                "/api/v1/ai/ask", headers=headers, json={"question": question}
            )

        for index in range(max(0, warmup)):
            await search_request(index)
            # Keep warmup variants disjoint from measured unique variants so
            # the cold-provider run cannot accidentally include a cache hit.
            await ai_request(index + requests)

        search = await measure(
            search_request, requests=requests, concurrency=concurrency
        )
        ai = await measure(ai_request, requests=requests, concurrency=concurrency)
        provider_config = await client.get("/api/v1/admin/llm/config", headers=headers)
        provider_backed = None
        if provider_config.status_code == 200:
            provider_payload = provider_config.json()
            provider_backed = provider_is_configured(provider_payload)
        return {
            "base_url": base_url,
            "search_query_length": len(search_query),
            "ai_question_length": len(ai_question),
            "warmup_requests_per_endpoint": max(0, warmup),
            "unique_ai_questions": unique_ai_questions,
            "provider_backed": provider_backed,
            "search": search,
            "ai": ai,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", default=os.getenv("PERF_BASE_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument("--email", default=os.getenv("PERF_TEST_EMAIL", ""))
    parser.add_argument(
        "--search-query", default=os.getenv("PERF_SEARCH_QUERY", "procedure")
    )
    parser.add_argument(
        "--ai-question",
        default=os.getenv("PERF_AI_QUESTION", "What is the retention period?"),
    )
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument(
        "--unique-ai-questions",
        action="store_true",
        help="Append a request number to each AI question to measure cold provider latency instead of cache hits",
    )
    parser.add_argument(
        "--require-provider-backed",
        action="store_true",
        help="Fail the probe unless the authenticated runtime confirms an enabled provider and configured API key",
    )
    parser.add_argument("--max-search-p95-ms", type=float, default=1000)
    parser.add_argument("--max-ai-p95-ms", type=float, default=5000)
    args = parser.parse_args()
    password = os.getenv("PERF_TEST_PASSWORD", "")
    if not args.email or not password:
        parser.error(
            "PERF_TEST_EMAIL and PERF_TEST_PASSWORD must be set; no credential is stored in this script"
        )
    if args.requests < 1 or args.concurrency < 1:
        parser.error("--requests and --concurrency must be positive")

    report = asyncio.run(
        run(
            base_url=args.base_url,
            email=args.email,
            password=password,
            search_query=args.search_query,
            ai_question=args.ai_question,
            requests=args.requests,
            concurrency=args.concurrency,
            warmup=args.warmup,
            unique_ai_questions=args.unique_ai_questions,
        )
    )
    print(json.dumps(report, indent=2))
    provider_requirement_failed = provider_gate_failed(
        report, args.require_provider_backed
    )
    return (
        1
        if (
            report["search"]["failures"]
            or report["ai"]["failures"]
            or report["search"]["p95_ms"] > args.max_search_p95_ms
            or report["ai"]["p95_ms"] > args.max_ai_p95_ms
            or provider_requirement_failed
        )
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
