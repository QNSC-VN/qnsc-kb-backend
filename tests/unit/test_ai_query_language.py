from src.domain.ai_service import RAG_SYSTEM_PROMPT, _query_language


def test_query_language_uses_the_typed_query_not_the_interface_locale():
    assert _query_language("Quy trình phê duyệt là gì?") == "vi"
    assert _query_language("What is the approval process?") == "en"


def test_rag_prompt_requires_the_answer_to_follow_the_latest_query_language():
    assert "latest text inside `<user-question>`" in RAG_SYSTEM_PROMPT
    assert "do not use the interface language" in RAG_SYSTEM_PROMPT
