import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from run_uat import evaluate_response, validate_cases, validate_fixture_coverage


def _cases(count=15):
    return [
        {
            "id": f"q-{index}",
            "question": f"Question {index}?",
            "expected_answer": "The retention period is thirty days.",
            "expected_article_ids": ["article-1"],
        }
        for index in range(count)
    ]


def test_uat_input_requires_exactly_fifteen_questions():
    assert len(validate_cases(_cases())) == 15
    with pytest.raises(ValueError, match="exactly 15"):
        validate_cases(_cases(14))


def test_uat_input_requires_source_ids_for_grounded_questions():
    cases = _cases()
    cases[0]["expected_article_ids"] = []
    with pytest.raises(ValueError, match="expected_article_ids"):
        validate_cases(cases)


def test_uat_input_cannot_silently_skip_an_expectation_fixture():
    cases = _cases()
    cases[0]["fixture_expectations"] = {"restricted": {"expect_refusal": True}}
    with pytest.raises(ValueError, match="not configured"):
        validate_fixture_coverage(
            cases, [{"name": "staff", "email": "staff@acme.test"}]
        )


def test_fixture_refusal_override_clears_base_article_expectations():
    cases = _cases()
    cases[0]["fixture_expectations"] = {"staff": {"expect_refusal": True}}

    normalized = validate_cases(cases)
    result = evaluate_response(
        case=normalized[0],
        fixture_name="staff",
        status_code=200,
        payload={"answer": "Not found in the Knowledge Base.", "citations": []},
        min_answer_score=0.8,
    )

    assert result["expected_article_ids"] == []
    assert result["passed"] is True


def test_uat_evaluation_rejects_unexpected_citation_and_missing_source_url():
    case = validate_cases(_cases())[0]
    result = evaluate_response(
        case=case,
        fixture_name="staff",
        status_code=200,
        payload={
            "answer": "The retention period is thirty days.",
            "citations": [
                {
                    "article_id": "article-1",
                    "source_url": "/api/v1/articles/article-1/source",
                },
                {
                    "article_id": "restricted-article",
                    "source_url": "/api/v1/articles/restricted-article/source",
                },
            ],
        },
        min_answer_score=0.8,
    )
    assert result["unexpected_article_ids"] == ["restricted-article"]
    assert result["passed"] is False


def test_uat_evaluation_requires_all_expected_articles_to_be_cited():
    case = validate_cases(_cases())[0]
    case["expected_article_ids"] = ["article-1", "article-2"]
    result = evaluate_response(
        case=case,
        fixture_name="staff",
        status_code=200,
        payload={
            "answer": "The retention period is thirty days.",
            "citations": [
                {
                    "article_id": "article-1",
                    "source_url": "/api/v1/articles/article-1/source",
                }
            ],
        },
        min_answer_score=0.8,
    )

    assert result["citation_recall"] == 0.5
    assert result["missing_article_ids"] == ["article-2"]
    assert result["passed"] is False


def test_uat_refusal_requires_no_citations():
    cases = _cases()
    cases[0] = {
        "id": "q-refusal",
        "question": "What is not in the knowledge base?",
        "expect_refusal": True,
    }
    case = validate_cases(cases)[0]
    refused = evaluate_response(
        case=case,
        fixture_name="staff",
        status_code=200,
        payload={"answer": "Not found in the Knowledge Base.", "citations": []},
        min_answer_score=0.8,
    )
    leaked = evaluate_response(
        case=case,
        fixture_name="staff",
        status_code=200,
        payload={
            "answer": "Not found in the Knowledge Base.",
            "citations": [{"article_id": "article-1"}],
        },
        min_answer_score=0.8,
    )
    assert refused["passed"] is True
    assert leaked["passed"] is False
