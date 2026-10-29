"""Package validity starts on first observed successful use, exactly once."""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services.first_use import activate_connection_expiry, activate_expiry, package_expiry


failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
models.Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, expire_on_commit=False)
now = dt.datetime(2026, 10, 29, 12, 0, 0)


print("--- package expiry policy ---")
fixed = models.Package(name="fixed", duration_days=30, starts_on_first_connection=False)
deferred = models.Package(name="deferred", duration_days=30, starts_on_first_connection=True)
unlimited = models.Package(name="unlimited", duration_days=0, starts_on_first_connection=True)
check("ordinary package keeps purchase-time expiry",
      package_expiry(fixed, now), (now + dt.timedelta(days=30), None))
check("first-connection package stores pending duration",
      package_expiry(deferred, now), (None, 30))
check("unlimited duration has no deferred countdown",
      package_expiry(unlimited, now), (None, None))


print("\n--- activation follows the owning service ---")
with Session() as db:
    node = models.Node(name="n1", type=models.NodeType.mikrotik, mt_host="127.0.0.1")
    user = models.User(username="first-use", expire_days_after_first_use=14)
    package = models.Package(name="purchase", duration_days=30, starts_on_first_connection=True)
    db.add_all([node, user, package])
    db.flush()
    purchase = models.Purchase(
        user_id=user.id,
        package_id=package.id,
        status=models.UserStatus.active,
        expire_at=None,
        expire_days_after_first_use=30,
    )
    db.add(purchase)
    db.flush()
    purchase_connection = models.Connection(
        user_id=user.id,
        node_id=node.id,
        type=models.ConnectionType.wireguard,
        purchase_id=purchase.id,
        wg_peer_name="first-use-purchase",
    )
    legacy_connection = models.Connection(
        user_id=user.id,
        node_id=node.id,
        type=models.ConnectionType.wireguard,
        purchase_id=None,
        wg_peer_name="first-use-legacy",
    )
    db.add_all([purchase_connection, legacy_connection])
    db.commit()

    check("purchase connection activates only its purchase",
          activate_connection_expiry(db, purchase_connection, now), True)
    db.commit()
    db.refresh(purchase)
    db.refresh(user)
    expected_purchase_expiry = now + dt.timedelta(days=30)
    check("purchase gets the first-use expiry", purchase.expire_at, expected_purchase_expiry)
    check("purchase pending days are cleared", purchase.expire_days_after_first_use, None)
    check("purchase activation leaves legacy user countdown pending", user.expire_days_after_first_use, 14)

    later = now + dt.timedelta(days=2)
    check("repeated purchase use cannot extend expiry",
          activate_connection_expiry(db, purchase_connection, later), False)
    db.commit()
    db.refresh(purchase)
    check("repeated use preserves original expiry", purchase.expire_at, expected_purchase_expiry)

    check("legacy connection activates the user allotment",
          activate_connection_expiry(db, legacy_connection, now), True)
    db.commit()
    db.refresh(user)
    check("legacy user gets its first-use expiry", user.expire_at, now + dt.timedelta(days=14))
    check("legacy pending days are cleared", user.expire_days_after_first_use, None)


print("\n" + "=" * 72)
print(f"{len(failures)} FAILED" if failures else "ALL CHECKS PASSED")
if failures:
    raise SystemExit(1)
