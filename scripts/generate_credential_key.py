"""Generate a Fernet key for encrypting stored credentials at rest.

Set the printed value as the CREDENTIAL_ENCRYPTION_KEY environment variable
(maps to google_integration.credential_encryption_key). Changing the key
invalidates every credential encrypted with the old one, so keep it stable.
"""

from family_assistant.services.credential_encryption import generate_key

print(f"CREDENTIAL_ENCRYPTION_KEY={generate_key()}")
