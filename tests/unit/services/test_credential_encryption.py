"""Unit tests for the Fernet credential encryption helper."""

import pytest

from family_assistant.services.credential_encryption import (
    CredentialDecryptionError,
    CredentialEncryption,
    CredentialEncryptionError,
    generate_key,
)


class TestCredentialEncryption:
    """Tests for CredentialEncryption round-trips and error behavior."""

    def test_round_trip(self) -> None:
        key = generate_key()
        enc = CredentialEncryption(key)

        plaintext = "1//refresh-token-value-abc123"
        ciphertext = enc.encrypt(plaintext)

        assert ciphertext != plaintext
        assert enc.decrypt(ciphertext) == plaintext

    def test_generate_key_classmethod_matches_module_function(self) -> None:
        # Both entry points produce usable Fernet keys.
        for key in (CredentialEncryption.generate_key(), generate_key()):
            enc = CredentialEncryption(key)
            assert enc.decrypt(enc.encrypt("value")) == "value"

    def test_decrypt_with_wrong_key_raises_decryption_error(self) -> None:
        ciphertext = CredentialEncryption(generate_key()).encrypt("secret")
        other = CredentialEncryption(generate_key())

        with pytest.raises(CredentialDecryptionError) as exc_info:
            other.decrypt(ciphertext)

        assert "CREDENTIAL_ENCRYPTION_KEY" in str(exc_info.value)

    def test_decrypt_garbage_raises_decryption_error(self) -> None:
        enc = CredentialEncryption(generate_key())

        with pytest.raises(CredentialDecryptionError):
            enc.decrypt("not-valid-fernet-ciphertext")

    def test_malformed_key_raises_encryption_error(self) -> None:
        with pytest.raises(CredentialEncryptionError):
            CredentialEncryption("this-is-not-a-fernet-key")

    def test_empty_key_raises_encryption_error(self) -> None:
        with pytest.raises(CredentialEncryptionError):
            CredentialEncryption("")
