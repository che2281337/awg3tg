"""Генерация ключей WireGuard (X25519) без вызова `awg genkey`."""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def generate_keypair() -> tuple[str, str]:
    """Возвращает (private_key, public_key) в base64, как `awg genkey | awg pubkey`."""
    private = X25519PrivateKey.generate()
    priv_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    pub_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(priv_raw).decode(), base64.b64encode(pub_raw).decode()


def public_from_private(private_key: str) -> str:
    private = X25519PrivateKey.from_private_bytes(base64.b64decode(private_key))
    pub_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(pub_raw).decode()
