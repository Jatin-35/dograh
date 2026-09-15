"""Encryption for Code Editor environment variables.

These values are handed to code the platform did not author, so they get real
encryption at rest rather than the plaintext-JSON treatment `external_credentials`
currently uses.

The key comes from ``CODE_EDITOR_ENCRYPTION_KEY`` — a urlsafe base64 32-byte
Fernet key. Generate one with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

If it is unset, storing a secret **fails** rather than silently falling back to
plaintext. A quiet fallback is how a deployment ends up with unencrypted secrets
that everyone believes are encrypted.

Rotating the key makes existing values undecryptable; they must be re-entered.
That is a deliberate trade — there is no second copy of a secret to recover from.
"""

import os

from cryptography.fernet import Fernet, InvalidToken

ENV_KEY_NAME = "CODE_EDITOR_ENCRYPTION_KEY"


class SecretsNotConfiguredError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            f"{ENV_KEY_NAME} is not configured, so environment variables cannot "
            "be stored. Generate one with: python -c \"from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())\""
        )


class SecretDecryptionError(RuntimeError):
    def __init__(self, key: str) -> None:
        super().__init__(
            f"Could not decrypt {key!r}. This usually means "
            f"{ENV_KEY_NAME} changed since the value was stored; re-enter it."
        )


def _fernet() -> Fernet:
    raw = os.environ.get(ENV_KEY_NAME, "").strip()
    if not raw:
        raise SecretsNotConfiguredError()
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"{ENV_KEY_NAME} is not a valid Fernet key (expected urlsafe "
            "base64-encoded 32 bytes)."
        ) from exc


def is_configured() -> bool:
    """Whether secrets can be stored. Checked by the route so the UI can say so
    up front rather than failing on save."""
    try:
        _fernet()
    except Exception:
        return False
    return True


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(key: str, token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretDecryptionError(key) from exc


def hint(value: str) -> str:
    """A few trailing characters, so the UI can identify a stored secret without
    ever showing it again. Short values reveal nothing at all.

    Scales with length rather than a flat 4: a 40-character API key showing
    only its last 4 characters is barely more identifying than showing none,
    while a 4-character reveal against a 9-character value gives away nearly
    half of it. Neither number is precise about where the line should sit —
    this just widens the reveal a little as there is more value to spare.
    """
    if len(value) >= 14:
        return value[-6:]
    if len(value) >= 8:
        return value[-4:]
    return ""
