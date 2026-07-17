"""Fernet-based encryption for per-user Google refresh tokens at rest.

Refresh tokens are the only long-lived Google credential the app persists, so
they are stored as Fernet ciphertext keyed by ``CREDENTIAL_ENCRYPTION_KEY``.
Access tokens live in memory only and are never encrypted here.

Fernet cannot distinguish a wrong deployment key from corrupt ciphertext, so a
decryption failure is treated as a *configuration* error
(``CredentialDecryptionError``): callers must surface it and must NOT mutate the
connection row, so restoring the correct key restores service with no user
action.
"""

from cryptography.fernet import Fernet, InvalidToken


class CredentialEncryptionError(Exception):
    """Raised when the encryption key is malformed at construction time."""


class CredentialDecryptionError(Exception):
    """Raised when stored ciphertext cannot be decrypted.

    Per the design this is a configuration error (wrong or partially rolled-out
    ``CREDENTIAL_ENCRYPTION_KEY``), never a signal to invalidate the connection.
    """


class CredentialEncryption:
    """Encrypts and decrypts credential strings with a Fernet key."""

    def __init__(self, key: str) -> None:
        """Initialize from a urlsafe-base64 Fernet key string.

        Args:
            key: The Fernet key as produced by ``generate_key`` / ``Fernet.generate_key``.

        Raises:
            CredentialEncryptionError: If the key is not a valid Fernet key.
        """
        try:
            self._fernet = Fernet(key.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise CredentialEncryptionError(
                "CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key "
                "(expected a urlsafe-base64-encoded 32-byte key). "
                "Generate one with scripts/generate_credential_key.py."
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext string, returning urlsafe-base64 ciphertext."""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt ciphertext produced by ``encrypt``.

        Raises:
            CredentialDecryptionError: If the ciphertext cannot be decrypted with
                the configured key. This is a configuration error — check
                ``CREDENTIAL_ENCRYPTION_KEY`` — and callers must not mutate the
                connection row in response.
        """
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise CredentialDecryptionError(
                "Stored credential could not be decrypted — check "
                "CREDENTIAL_ENCRYPTION_KEY. This is a configuration error; the "
                "connection is left untouched so restoring the correct key "
                "restores access."
            ) from exc

    @classmethod
    def generate_key(cls) -> str:
        """Generate a new urlsafe-base64 Fernet key string."""
        return Fernet.generate_key().decode("utf-8")


def generate_key() -> str:
    """Generate a new urlsafe-base64 Fernet key string."""
    return CredentialEncryption.generate_key()
