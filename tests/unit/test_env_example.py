import re
from pathlib import Path

from src.core.config import Settings


def test_env_example_declares_every_application_setting():
    env_example = Path(__file__).parents[2] / ".env.example"
    declared_keys = re.findall(
        r"(?m)^([A-Z][A-Z0-9_]*)=", env_example.read_text(encoding="utf-8")
    )
    keys = set(declared_keys)
    assert set(Settings.model_fields) <= keys
    assert len(declared_keys) == len(
        keys
    ), ".env.example must not define duplicate keys"


def test_dockerignore_excludes_secrets_and_local_runtime_state():
    dockerignore = (Path(__file__).parents[2] / ".dockerignore").read_text(
        encoding="utf-8"
    )

    for required_pattern in (
        ".env",
        ".env.*",
        ".git",
        "storage/",
        "connector_sources/",
        "tests/",
    ):
        assert required_pattern in dockerignore
