"""Make the hash for the vendor recovery password ("رمز مادر").

    python3 backend/scripts/hash_recovery.py 'the-password-you-choose'

Put the printed hash into MASTER_RECOVERY_PASSWORD_HASH in each panel's
.env (the same hash across all your installs is fine - it is your own
secret). The plaintext password never leaves your head; only the hash is
stored, and even reading a customer's .env does not reveal it.

Leave MASTER_RECOVERY_PASSWORD_HASH empty to keep the recovery login OFF -
there is then no vendor login on that panel at all.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.security import hash_password  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print(__doc__)
        return 1
    print(hash_password(sys.argv[1]))
    print()
    print("Add to each panel's backend/.env (or docker-compose environment):")
    print(f"  MASTER_RECOVERY_PASSWORD_HASH={hash_password(sys.argv[1])}")
    print(f"  MASTER_RECOVERY_USERNAME=__vendor__   (or your own choice)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
