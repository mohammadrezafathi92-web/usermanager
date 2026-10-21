"""License signing for the console's "صدور لایسنس" page - added 2026-09-07
so the operator can issue a license from the browser instead of running
backend/scripts/license_tool.py by hand.

Deliberately duplicates (does not import) backend/app/services/licensing.py's
token format. This service ships with NONE of the panel's code on purpose
(see store.py's own docstring - separate DB, separate login, separate
container, so a compromised panel never reaches this data). The two are
kept in sync by matching the wire format exactly - TOKEN_PREFIX, the JSON
field names, and Ed25519-signing the sorted-key JSON body - rather than by
sharing an import. backend/scripts/license_tool.py's cmd_issue does the
same thing from the vendor's own machine; this is the same recipe, run
from here instead.

The private key itself lives on this SAME server (the vendor's own - never
a customer's), at LICENSE_PRIVATE_KEY_PATH, on the license-server's own
persisted /data volume (next to license.db) so it survives restarts. It is
never bundled into the image and never leaves this container's volume.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import uuid
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

TOKEN_PREFIX = "NETCIP1"

PRIVATE_KEY_PATH = os.environ.get("LICENSE_PRIVATE_KEY_PATH", "/data/private.key")


class SigningKeyMissing(Exception):
    pass


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def has_signing_key() -> bool:
    return os.path.exists(PRIVATE_KEY_PATH)


def _load_private_key() -> Ed25519PrivateKey:
    try:
        with open(PRIVATE_KEY_PATH, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise SigningKeyMissing(
            f"کلید خصوصی امضا روی این سرور پیدا نشد ({PRIVATE_KEY_PATH})"
        ) from exc
    try:
        return Ed25519PrivateKey.from_private_bytes(raw)
    except Exception as exc:  # noqa: BLE001 - any bad key file reads the same to the operator
        raise SigningKeyMissing(f"فایل کلید خصوصی خوانده نشد: {exc}") from exc


def issue(
    *,
    customer: str,
    fingerprint: str = "",
    days: Optional[int] = None,
    max_customers: Optional[int] = None,
    note: str = "",
) -> tuple[str, str]:
    """Signs and returns (token, license_id). Raises SigningKeyMissing if
    the private key is not present on this server.

    fingerprint="" produces a licence valid on ANY machine - only for a
    demo or the vendor's own testing, same warning license_tool.py's CLI
    gives.
    """
    token, payload = issue_payload(customer=customer, fingerprint=fingerprint, days=days,
                                   max_customers=max_customers, note=note)
    return token, payload["id"]


def issue_payload(
    *,
    customer: str,
    fingerprint: str = "",
    days: Optional[int] = None,
    valid_days: Optional[int] = None,
    until: Optional[dt.datetime] = None,
    max_customers: Optional[int] = None,
    note: str = "",
    now: Optional[dt.datetime] = None,
) -> tuple[str, dict]:
    """The three clocks license_tool.py offers, one at a time:

      days        - exp = now + days (the term starts at issue);
      valid_days  - "vd": the term starts when the panel first sees the key;
      until       - exp = that exact moment (UTC).

    None of them = perpetual. The payload is byte-for-byte the panel's
    LicensePayload.to_dict() layout, so nothing about the token changes.
    """
    if sum(x is not None for x in (days, valid_days, until)) > 1:
        raise ValueError("only one validity basis may be given")
    private = _load_private_key()
    now = now or dt.datetime.utcnow()
    expires_at = now + dt.timedelta(days=days) if days else until
    payload = {
        "v": 1,
        "id": f"lic_{uuid.uuid4().hex[:12]}",
        "customer": customer,
        "fp": fingerprint or "",
        "iat": now.replace(microsecond=0).isoformat(),
        "exp": expires_at.replace(microsecond=0).isoformat() if expires_at else None,
        "max_customers": max_customers,
        "features": [],
        "note": note,
        "vd": valid_days,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = private.sign(body)
    token = f"{TOKEN_PREFIX}.{_b64e(body)}.{_b64e(signature)}"
    return token, payload


class TokenRejected(Exception):
    pass


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def read_token(token: str) -> dict:
    """The payload of a token THIS server's key signed, or TokenRejected.

    Used to record the facts of a licence issued elsewhere (the CLI, or
    before this console kept records), so its validity is known from the
    signed payload itself rather than guessed."""
    token = (token or "").strip().replace("\n", "").replace(" ", "")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise TokenRejected("format")
    try:
        body, signature = _b64d(parts[1]), _b64d(parts[2])
    except Exception as exc:  # noqa: BLE001
        raise TokenRejected("format") from exc
    try:
        _load_private_key().public_key().verify(signature, body)
    except SigningKeyMissing:
        raise
    except InvalidSignature as exc:
        raise TokenRejected("signature") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError
    except ValueError as exc:
        raise TokenRejected("format") from exc
    return payload


def public_key_b64() -> Optional[str]:
    """The verifying half - public by design; shown so the operator can
    compare it with the key compiled into the panel build."""
    try:
        raw = _load_private_key().public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    except SigningKeyMissing:
        return None
    return _b64e(raw)
