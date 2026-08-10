import pytest

from src.core.security import get_password_hash, verify_password


def test_password_hash_round_trip_uses_bcrypt_backend():
    hashed = get_password_hash("Correct horse battery staple!42")

    assert hashed.startswith("$2")
    assert verify_password("Correct horse battery staple!42", hashed)
    assert not verify_password("wrong password", hashed)


def test_password_hash_rejects_bcrypt_unsupported_length():
    with pytest.raises(ValueError, match="72"):
        get_password_hash("x" * 73)
