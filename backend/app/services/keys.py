"""WireGuard keypair generation without depending on the `wg` binary, plus
misc random-credential helpers used for PPP (OpenVPN/L2TP) secrets."""
import base64
import secrets
import string

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization


def generate_wireguard_keypair() -> tuple[str, str]:
    """Returns (private_key_b64, public_key_b64) compatible with WireGuard."""
    private_key = X25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    return (
        base64.b64encode(private_bytes).decode("ascii"),
        base64.b64encode(public_bytes).decode("ascii"),
    )


def generate_password(length: int = 14) -> str:
    """Random alphanumeric password for PPP (OpenVPN/L2TP) secrets."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)
