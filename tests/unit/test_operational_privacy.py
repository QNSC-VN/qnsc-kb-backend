from src.core.privacy import REDACTED_OPERATIONAL_CONTENT


def test_operational_telemetry_uses_a_non_content_marker():
    assert REDACTED_OPERATIONAL_CONTENT == "[redacted]"
