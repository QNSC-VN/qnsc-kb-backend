from src.core.config import settings
from src.domain import source_storage
from src.domain.source_storage import load_source, safe_source_media_type, save_source, source_should_display_inline


def test_local_source_storage_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SOURCE_STORAGE_BACKEND", "local")
    monkeypatch.setattr(settings, "SOURCE_STORAGE_PATH", str(tmp_path))
    key = save_source("abc123", "../../unsafe name.pdf", b"document")
    assert ".." not in key
    assert load_source(key) == b"document"


def test_r2_upload_uses_private_tenant_scoped_object_key(monkeypatch):
    captured = {}

    class Client:
        def put_object(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(settings, "SOURCE_STORAGE_BACKEND", "r2")
    monkeypatch.setattr(settings, "SOURCE_STORAGE_BUCKET", "private-kb")
    monkeypatch.setattr(settings, "SOURCE_STORAGE_PREFIX", "sources")
    monkeypatch.setattr(source_storage, "_s3_client", lambda: Client())

    key = save_source("abc123", "report.pdf", b"document", "ACME.test")

    assert key.startswith("s3://private-kb/sources/acme.test/")
    assert captured["Bucket"] == "private-kb"
    assert captured["ContentType"] == "application/octet-stream"
    assert captured["Metadata"] == {"sha256": "abc123"}
    assert "ACL" not in captured


def test_source_media_type_is_derived_from_a_safe_allow_list():
    assert safe_source_media_type("report.pdf") == "application/pdf"
    assert safe_source_media_type("unsafe.svg") == "application/octet-stream"
    assert source_should_display_inline("report.pdf")
    assert not source_should_display_inline("notes.txt")
