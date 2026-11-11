"""Shared Pitt model choices and backend-only credential handling."""

import os

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


PITT_MODELS = {
    "@pitt-ai-connect-google-vertex/gemini-4-argon": "Gemini 4 Argon",
    "@pitt-ai-connect-google-vertex/gemini-3.8-flash": "Gemini 3.8 Flash",
}


def validate_pitt_model(model: str) -> str:
    if model not in PITT_MODELS:
        raise ValueError("Choose one of the available Pitt models.")
    return model


def _clean_key(raw: str) -> str:
    key = raw.strip()
    if not key or len(key) > 4096 or any(not 33 <= ord(char) <= 126 for char in key):
        raise ValueError("Enter a valid Pitt API key.")
    return key


def resolve_form_key(api_key: str) -> str:
    key = api_key.strip()
    if not key:
        key = os.environ.get("PITT_API_KEY", "").strip()
        if not key:
            raise ValueError("The server Pitt API key is not configured.")
    return _clean_key(key)


def _cipher() -> Fernet:
    key = settings.pitt_key_encryption_key
    if not key:
        raise ValueError("Pitt API key encryption is not configured on the server.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError):
        raise ValueError("Pitt API key encryption is not configured correctly on the server.") from None


def encrypt_pitt_key(raw: str) -> str:
    key = _clean_key(raw)
    return _cipher().encrypt(key.encode("ascii")).decode("ascii")


def decrypt_pitt_key(ciphertext: str) -> str:
    cipher = _cipher()
    try:
        return _clean_key(cipher.decrypt(ciphertext.encode("ascii")).decode("ascii"))
    except (InvalidToken, ValueError):
        raise ValueError("The saved Pitt API key cannot be decrypted.") from None


def resolve_runtime_key(repository, assignment) -> str:
    from app.feedback_store import StoreError

    credential = repository.get_pitt_credential()
    source = "custom" if credential.encrypted_key is not None else "server"
    if (credential.key_version != assignment.pitt_key_version
            or source != assignment.pitt_key_source):
        raise StoreError("config_changed", 409)
    if credential.encrypted_key is not None:
        return decrypt_pitt_key(credential.encrypted_key)
    return resolve_form_key("")
