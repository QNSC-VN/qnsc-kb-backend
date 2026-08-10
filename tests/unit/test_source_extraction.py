from src.domain import source_extraction


def test_markitdown_is_bypassed_for_lossless_text_formats(monkeypatch):
    called = False

    def fail_if_called():
        nonlocal called
        called = True
        raise AssertionError("MarkItDown should not handle Markdown input")

    monkeypatch.setattr(source_extraction, "_markitdown", fail_if_called)
    for filename in ("procedure.md", "notes.txt", "rows.csv"):
        assert source_extraction._convert_with_markitdown(filename, b"content") == ""
    assert called is False
