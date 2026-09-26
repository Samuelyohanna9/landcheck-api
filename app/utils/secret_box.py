from __future__ import annotations

"""Encryption for stored third-party tokens, and signed short-lived tokens for public links.

Both are derived from one server secret, SOCIAL_SECRET_KEY. When it is not set the features that need
it report "not configured" instead of storing anything in plain text."""

import base64
import hashlib
import hmac
import os
import time

from cryptography.fernet import Fernet, InvalidToken


class SecretNotConfigured(RuntimeError):
    pass


def _secret() -> bytes:
    raw = str(os.getenv("SOCIAL_SECRET_KEY") or "").strip()
    if len(raw) < 16:
        raise SecretNotConfigured("SOCIAL_SECRET_KEY is not set (use a random string of 32+ characters)")
    return raw.encode("utf-8")


def secret_configured() -> bool:
    try:
        _secret()
        return True
    except SecretNotConfigured:
        return False


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"estate-social-token|" + _secret()).digest()))


def encrypt_text(value: str) -> str:
    return _fernet().encrypt(str(value).encode("utf-8")).decode("ascii")


def decrypt_text(value: str) -> str:
    try:
        return _fernet().decrypt(str(value).encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretNotConfigured("Stored token could not be decrypted (SOCIAL_SECRET_KEY changed?)") from exc


def _sign(message: str) -> str:
    return hmac.new(hashlib.sha256(b"estate-social-sign|" + _secret()).digest(), message.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def make_signed_token(kind: str, *parts: str | int, ttl_seconds: int) -> str:
    """kind.part1.part2...expiry.signature - safe to put in a URL."""
    expiry = int(time.time()) + int(ttl_seconds)
    body = ".".join([kind, *[str(part) for part in parts], str(expiry)])
    return f"{body}.{_sign(body)}"


def read_signed_token(token: str, kind: str) -> list[str] | None:
    """Returns the parts when the signature is valid, the kind matches and it has not expired."""
    try:
        body, signature = str(token).rsplit(".", 1)
        if not hmac.compare_digest(_sign(body), signature):
            return None
        pieces = body.split(".")
        if pieces[0] != kind or int(pieces[-1]) < time.time():
            return None
        return pieces[1:-1]
    except (ValueError, SecretNotConfigured):
        return None
