from src.rag.chunker import create_parent_child_chunks


def test_chunker_preserves_table_structure_and_heading_metadata():
    text = """## Retention policy

| Category | Duration |
| --- | --- |
| Standard | 30 days |
| Exception | 90 days |
"""

    parents = create_parent_child_chunks(text)

    assert parents
    assert parents[0]["chunk_type"] == "table"
    assert parents[0]["heading"] == "Retention policy"
    assert any("Standard" in child for child in parents[0]["children"])
    assert any("Exception" in child for child in parents[0]["children"])


def test_chunker_keeps_list_items_together_when_they_fit():
    text = """## Required steps

1. Validate the request.
2. Record the approval decision.
3. Publish the approved article.
"""

    parents = create_parent_child_chunks(text)

    assert parents
    assert parents[0]["chunk_type"] == "list"
    assert "Validate the request." in parents[0]["parent_text"]
    assert "Publish the approved article." in parents[0]["parent_text"]
