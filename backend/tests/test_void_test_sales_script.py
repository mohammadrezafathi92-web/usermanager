"""scripts/void_test_sales.py removes exactly one customer's RECENT sales -
and nothing else, and nothing at all without an explicit confirmation.

Run:  python3 backend/tests/test_void_test_sales_script.py
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.scripts import void_test_sales as script
from app.services import user_ops

failures: list[str] = []
NOW = dt.datetime.utcnow()
OLD, RECENT = NOW - dt.timedelta(days=10), NOW - dt.timedelta(hours=5)


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
models.Base.metadata.create_all(engine)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)      # same settings as the application
script.SessionLocal = Session
backups: list[str] = []
script.backup.create_backup = lambda: backups.append("taken") or Path("backup_test.db.gz")
removed_from_nodes: list[int] = []
unreachable: set[int] = set()


def fake_deprovision(connection):
    if connection.id in unreachable:
        raise RuntimeError("node unreachable")
    removed_from_nodes.append(connection.id)


user_ops.deprovision_connection = fake_deprovision

db = Session()
node = models.Node(name="n", type=models.NodeType.mikrotik)
package = models.Package(name="P", quota_gb=5, duration_days=30, price=1000)
card = models.PaymentCard(card_number="1", card_holder="h", accumulated_amount=5000)
code = models.DiscountCode(code="OFF", kind="percent", value=10, used_count=3)
tester = models.User(username="tester", telegram_id=1, balance=700)
bystander = models.User(username="bystander", telegram_id=2, balance=50)
db.add_all([node, package, card, code, tester, bystander])
db.flush()
NODE_ID, PACKAGE_ID = node.id, package.id


def purchase(user, when):
    row = models.Purchase(user_id=user.id, package_id=PACKAGE_ID, package_name_snapshot="P", created_at=when)
    db.add(row)
    db.flush()
    connection = models.Connection(user_id=user.id, node_id=NODE_ID, type=models.ConnectionType.wireguard, purchase_id=row.id)
    db.add(connection)
    db.flush()
    return row, connection


def ledger(user, kind, amount, when, purchase_id=None, card_id=None):
    row = models.LedgerEntry(kind=kind, amount=amount, user_id=user.id, purchase_id=purchase_id, payment_card_id=card_id,
                             created_at=when)
    db.add(row)
    return row


old_purchase, old_conn = purchase(tester, OLD)
new_a, conn_a = purchase(tester, RECENT)
new_b, conn_b = purchase(tester, RECENT)
other_purchase, other_conn = purchase(bystander, RECENT)
ledger(tester, "sale_new", 111, OLD, old_purchase.id, card.id)
ledger(tester, "sale_new", 1000, RECENT, new_a.id, card.id)
ledger(tester, "sale_new", 900, RECENT, new_b.id, card.id)
ledger(tester, "sale_renew", 400, RECENT, old_purchase.id)              # renewal of an OLDER service
ledger(tester, "wallet_topup", 500, RECENT, None, card.id)
ledger(bystander, "sale_new", 777, RECENT, other_purchase.id, card.id)
db.add(models.DiscountCodeRedemption(code_id=code.id, user_id=tester.id, username="tester", created_at=RECENT))
db.add(models.DiscountCodeRedemption(code_id=code.id, user_id=tester.id, username="tester", created_at=OLD))
db.commit()
ids = {"old": old_purchase.id, "a": new_a.id, "b": new_b.id, "other": other_purchase.id,
       "conn_a": conn_a.id, "conn_b": conn_b.id, "conn_old": old_conn.id}
db.close()


def run(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code_ = script.main(["x", *args])
    return code_, out.getvalue()


def snapshot():
    session = Session()
    try:
        return {
            "purchases": sorted(p.id for p in session.query(models.Purchase)),
            "ledger": sorted((r.kind, r.amount) for r in session.query(models.LedgerEntry)),
            "balance": session.query(models.User).filter_by(username="tester").one().balance,
            "card": session.get(models.PaymentCard, 1).accumulated_amount,
            "code_used": session.get(models.DiscountCode, 1).used_count,
            "redemptions": session.query(models.DiscountCodeRedemption).count(),
            "connections": session.query(models.Connection).count(),
        }
    finally:
        session.close()


before = snapshot()

print("--- nothing happens without an explicit, matching confirmation ---")
code_, text = run("tester", "--days", "3")
check("dry run: lists the two recent services, four accounting rows, the totals - and changes nothing",
      (code_, "services to delete: 2" in text, "accounting rows to delete: 4" in text, "2,800" in text,
       "NOTHING was changed" in text, snapshot() == before, backups, removed_from_nodes), (0, True, True, True, True, True, [], []))
check("the dry run warns about the renewal of an older service", "renewal(s) of an older service" in text, True)
check("--execute without --confirm, or with another name: still nothing",
      (run("tester", "--execute")[0], run("tester", "--execute", "--confirm", "bystander")[0], snapshot() == before, backups),
      (2, 2, True, []))
check("an unknown customer / a silly window", (run("ghost")[0], run("tester", "--days", "400")[0]), (1, 2))

print("--- execute ---")
code_, text = run("tester", "--days", "3", "--execute", "--confirm", "tester")
after = snapshot()
check("a backup is taken first", (backups, "backup taken first" in text), (["taken"], True))
check("the two recent services and their connections are gone - from the nodes too; the old one is untouched",
      (after["purchases"], sorted(removed_from_nodes), after["connections"]),
      (sorted([ids["old"], ids["other"]]), sorted([ids["conn_a"], ids["conn_b"]]), 2))
check("the customer's recent accounting rows are gone; older rows and another customer's rows stay",
      after["ledger"], [("sale_new", 111), ("sale_new", 777)])
check("wallet: the top-up is taken back (700 - 500)", after["balance"], 200)
check("bank card running total: 5000 - (1000 + 900 + 500)", after["card"], 2600)
check("the recent discount use is undone, the old one stays", (after["code_used"], after["redemptions"]), (2, 1))
check("exit code 0", code_, 0)
check("running it again finds nothing left to do",
      ("services to delete: 0" in run("tester")[1], "accounting rows to delete: 0" in run("tester")[1]), (True, True))

print("--- a node that cannot be reached ---")
db = Session()
tester = db.query(models.User).filter_by(username="tester").one()
stuck, stuck_conn = purchase(tester, RECENT)
ledger(tester, "sale_new", 50, RECENT, stuck.id)
db.commit()
unreachable.add(stuck_conn.id)
stuck_id = stuck.id
db.close()
code_, text = run("tester", "--execute", "--confirm", "tester", "--delete-user")
after = snapshot()
check("the service is KEPT and reported, the exit code says so, and the account is not deleted",
      (code_, f"KEPT service #{stuck_id}" in text, stuck_id in after["purchases"], "account NOT deleted" in text), (1, True, True, True))

print("--- deleting the account as well ---")
unreachable.clear()
code_, text = run("tester", "--execute", "--confirm", "tester", "--delete-user")
session = Session()
check("the account is gone; the other customer is untouched",
      (code_, session.query(models.User).filter_by(username="tester").count(),
       session.query(models.User).filter_by(username="bystander").one().balance,
       session.query(models.Purchase).filter_by(user_id=2).count()), (0, 0, 50, 1))
session.close()

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
