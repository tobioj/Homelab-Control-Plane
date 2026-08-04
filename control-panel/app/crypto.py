"""Encrypt-at-rest for per-project secrets (brief §5: "the control-plane
app's own database/storage of per-project secrets... must be encrypted at
rest, not a flat readable file"). Fernet (symmetric, authenticated) keyed by
ENCRYPTION_KEY in .env — deliberately not reusing SECRET_KEY, which signs
session cookies and has a different threat model."""

from cryptography.fernet import Fernet

from app.config import settings

_fernet = Fernet(settings.ENCRYPTION_KEY.encode())


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()
