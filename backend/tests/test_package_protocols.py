"""A package's bundled service must name a protocol its server can speak.

Run:  python3 backend/tests/test_package_protocols.py

Nothing checked this before: an Xray node paired with L2TP saved without a
word and failed only when a customer bought the package and provisioning
ran - after they had paid, and far from the form that caused it.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.routers import packages as packages_router

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
models.Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()

mt = models.Node(name="mikro-1", type=models.NodeType.mikrotik)
xr = models.Node(name="xray-1", type=models.NodeType.xray)
pkg = models.Package(name="p", price=1)
db.add_all([mt, xr, pkg])
db.commit()
for row in (mt, xr, pkg):
    db.refresh(row)


def sync(pairs):
    """(ok, detail). pairs = [(node, protocol), ...]"""
    specs = [
        schemas.PackageConnectionSpec(node_id=n.id, protocol=p, flow="")
        for n, p in pairs
    ]
    try:
        packages_router._sync_connections(db, pkg, specs)
        db.commit()
        return True, None
    except HTTPException as exc:
        db.rollback()
        return False, str(exc.detail)


print("--- what a MikroTik can carry ---")
for proto in ("wireguard", "openvpn", "l2tp", "ikev2", "sstp"):
    check(f"mikrotik + {proto}", sync([(mt, proto)]), (True, None))

ok, detail = sync([(mt, "xray")])
check("mikrotik + xray is refused", ok, False)
check("...and the message names the server and the protocol",
      "mikro-1" in detail and "xray" in detail, True)

print("\n--- what an Xray node can carry ---")
check("xray + xray", sync([(xr, "xray")]), (True, None))
for proto in ("wireguard", "openvpn", "l2tp", "ikev2", "sstp"):
    ok, _ = sync([(xr, proto)])
    check(f"xray + {proto} is refused", ok, False)

print("\n--- the whole submission is judged, not just the first row ---")
ok, detail = sync([(mt, "wireguard"), (xr, "l2tp")])
check("one bad pair rejects the batch", ok, False)
check("...naming the offending server", "xray-1" in detail, True)
# And crucially, the good row must NOT have been written either.
check("nothing was saved from a rejected batch",
      db.query(models.PackageConnection).filter_by(package_id=pkg.id).count(), 1)

print("\n--- a valid batch replaces the set ---")
check("two valid pairs save", sync([(mt, "openvpn"), (xr, "xray")]), (True, None))
rows = db.query(models.PackageConnection).filter_by(package_id=pkg.id).all()
check("both were written", len(rows), 2)
check("and the previous set is gone",
      sorted(r.protocol for r in rows), ["openvpn", "xray"])

print("\n--- a server that does not exist ---")
specs = [schemas.PackageConnectionSpec(node_id=99999, protocol="wireguard", flow="")]
try:
    packages_router._sync_connections(db, pkg, specs)
    ok, detail = True, None
except HTTPException as exc:
    db.rollback()
    ok, detail = False, str(exc.detail)
check("is refused rather than silently dropped", ok, False)
check("the existing set survived",
      db.query(models.PackageConnection).filter_by(package_id=pkg.id).count(), 2)

print("\n--- the panel and the bot agree on the split ---")
from app.telegram_bot import keyboards

for node_type, expected in (("xray", {"xray"}),
                            ("mikrotik", {"wireguard", "openvpn", "l2tp", "ikev2", "sstp"})):
    bot_side = {"xray"} if node_type == "xray" else {"wireguard", "openvpn", "l2tp", "ikev2", "sstp"}
    panel_side = (packages_router.XRAY_PROTOCOLS if node_type == "xray"
                  else packages_router.MIKROTIK_PROTOCOLS)
    check(f"{node_type}: same set on both sides", panel_side, bot_side)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
