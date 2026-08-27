"""Issue and inspect licences. Runs on YOUR machine, never on a customer's.

    # once, ever - keep the private key safe and OFF every customer server
    python3 backend/scripts/license_tool.py keygen

    # on the customer's server, to read its fingerprint
    python3 backend/scripts/license_tool.py fingerprint

    # then, on your machine
    python3 backend/scripts/license_tool.py issue \
        --key netcip_license_private.key \
        --customer "فروشگاه رضا" \
        --fingerprint 3f5c... \
        --days 365

    # sanity-check a key you just issued
    python3 backend/scripts/license_tool.py inspect --token NETCIP1.xxx.yyy

The private key is the whole security of this scheme. If it leaks, anyone
can mint licences and nothing else here matters. Keep one copy, encrypted,
off the internet - not in this repository and not on a panel server.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from app.services import licensing


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def cmd_keygen(args) -> int:
    if os.path.exists(args.out) and not args.force:
        print(f"{args.out} already exists. Refusing to overwrite it - "
              f"a lost signing key invalidates every licence you have issued.\n"
              f"Pass --force only if you are certain.")
        return 1
    private = Ed25519PrivateKey.generate()
    raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(args.out, "wb") as fh:
        fh.write(raw)
    os.chmod(args.out, 0o600)

    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pub_b64 = _b64e(public)

    print(f"private key written to {args.out}  (chmod 600)")
    print()
    print("Put the PUBLIC key into the panel build. Either set it as an")
    print("environment variable in docker-compose.yml:")
    print()
    print(f"    USERMANAGER_LICENSE_PUBKEY={pub_b64}")
    print()
    print("or paste it into SIGNING_PUBLIC_KEY_B64 in")
    print("backend/app/services/licensing.py before compiling.")
    print()
    print("Back the private key up now. There is no way to re-derive it, and")
    print("without it you cannot issue or renew a single licence again.")
    return 0


def _load_private(path: str) -> Ed25519PrivateKey:
    with open(path, "rb") as fh:
        raw = fh.read()
    return Ed25519PrivateKey.from_private_bytes(raw)


def cmd_issue(args) -> int:
    private = _load_private(args.key)

    expires_at = None
    if args.days:
        expires_at = dt.datetime.utcnow() + dt.timedelta(days=args.days)
    elif args.until:
        expires_at = dt.datetime.fromisoformat(args.until)

    payload = licensing.LicensePayload(
        license_id=args.id or f"lic_{uuid.uuid4().hex[:12]}",
        customer=args.customer,
        fingerprint=args.fingerprint or "",
        issued_at=dt.datetime.utcnow(),
        expires_at=expires_at,
        max_customers=args.max_customers,
        features=args.feature or [],
        note=args.note or "",
    )
    body = json.dumps(payload.to_dict(), ensure_ascii=False,
                      separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = private.sign(body)
    token = f"{licensing.TOKEN_PREFIX}.{_b64e(body)}.{_b64e(signature)}"

    if not args.fingerprint:
        print("WARNING: no --fingerprint given. This licence will work on ANY")
        print("         server. Only do that for a demo or your own testing.")
        print()
    print(f"licence id : {payload.license_id}")
    print(f"customer   : {payload.customer}")
    print(f"machine    : {payload.fingerprint or '(any)'}")
    print(f"expires    : {expires_at.date() if expires_at else '(never)'}")
    print()
    print(token)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        print()
        print(f"also written to {args.out}")
    return 0


def cmd_fingerprint(args) -> int:
    print(licensing.hardware_fingerprint())
    return 0


def cmd_inspect(args) -> int:
    token = args.token
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            token = fh.read()
    try:
        payload, _body, _sig = licensing.parse_token(token)
    except licensing.LicenseError as exc:
        print(f"unreadable: {exc}")
        return 1
    print(json.dumps(payload.to_dict(), ensure_ascii=False, indent=2))

    if args.pubkey:
        # Verify against a public key given here, so you can confirm a
        # licence really was signed by the key you think it was.
        licensing.SIGNING_PUBLIC_KEY_B64 = args.pubkey
        status = licensing.verify(token, fingerprint=payload.fingerprint or None)
        print()
        print(f"signature  : {'valid' if status.reason != licensing.REASON_BAD_SIGNATURE else 'INVALID'}")
        print(f"verdict    : {status.reason}  {status.message}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("keygen", help="create the signing key pair (once)")
    p.add_argument("--out", default="netcip_license_private.key")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_keygen)

    p = sub.add_parser("issue", help="sign a licence for one customer")
    p.add_argument("--key", required=True, help="path to the private key")
    p.add_argument("--customer", required=True)
    p.add_argument("--fingerprint", help="from `license_tool.py fingerprint` on their server")
    p.add_argument("--days", type=int, help="valid for this many days")
    p.add_argument("--until", help="valid until this date (YYYY-MM-DD)")
    p.add_argument("--max-customers", type=int, dest="max_customers")
    p.add_argument("--feature", action="append")
    p.add_argument("--note", default="")
    p.add_argument("--id", help="override the generated licence id")
    p.add_argument("--out", help="also write the token to this file")
    p.set_defaults(func=cmd_issue)

    p = sub.add_parser("fingerprint", help="print THIS machine's fingerprint")
    p.set_defaults(func=cmd_fingerprint)

    p = sub.add_parser("inspect", help="decode a licence (and optionally check its signature)")
    p.add_argument("--token")
    p.add_argument("--file")
    p.add_argument("--pubkey", help="base64 public key, to verify the signature")
    p.set_defaults(func=cmd_inspect)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
