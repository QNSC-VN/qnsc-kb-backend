from src.core.config import settings
from src.core.secrets import decrypt_secret, encrypt_secret
from src.domain.llm_config import decrypt_api_key, encrypt_api_key


def test_data_encryption_key_rotation_retains_old_ciphertexts(monkeypatch):
    monkeypatch.setattr(settings, "SECRET_KEY", "s" * 32)
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEY", "o" * 32)
    monkeypatch.setattr(settings, "PREVIOUS_DATA_ENCRYPTION_KEYS", "")
    old_ciphertext = encrypt_secret("connector-token")

    monkeypatch.setattr(settings, "DATA_ENCRYPTION_KEY", "n" * 32)
    monkeypatch.setattr(settings, "PREVIOUS_DATA_ENCRYPTION_KEYS", "o" * 32)
    assert decrypt_secret(old_ciphertext) == "connector-token"
    assert decrypt_api_key(encrypt_api_key("llm-key")) == "llm-key"
