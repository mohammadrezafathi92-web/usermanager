"""Removes one customer's RECENT sales completely - the services, their
connections on the nodes, and every accounting trace - as if they had never
happened. Written for test purchases made on a live panel.

This is NOT the receipt-void feature (docs/receipt-void-design-2026-10-02.md):
it deletes rows instead of reversing them, it is run by hand on the server,
and it has no step-by-step recovery. Use it for test data only.

What it does for the customer, limited to the last N days:
  - every service (Purchase) created in that time: its connections are
    removed from their nodes and deleted, then the service itself. A
    service whose connection could not be removed from its node is KEPT
    and reported.
  - every accounting row of that customer from that time (sales,
    renewals, wallet top-ups) is deleted.
  - wallet top-ups of that time are taken back out of the wallet balance
    (never below zero).
  - discount codes used in that time become unused again.
  - what those payments added to each bank card's running total is taken
    back (never below zero).
  - with --delete-user: the account itself is deleted too.

Not handled (reported if it applies): a renewal of an OLDER service, a
reseller's credit that was charged for these sales, loyalty counters.

Without --execute nothing is changed: it only prints what it would do.
With --execute a full database backup is taken first.

Run:
  docker compose exec backend python -m app.scripts.void_test_sales USERNAME --days 3
  docker compose exec backend python -m app.scripts.void_test_sales USERNAME --days 3 --execute --confirm USERNAME
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from .. import models
from ..database import SessionLocal
from ..services import backup, payment_cards, user_ops

LEDGER_KINDS = ("sale_new", "sale_renew", "wallet_topup")


def collect(db, user: models.User, cutoff: dt.datetime) -> dict:
    purchases = (db.query(models.Purchase)
                 .filter(models.Purchase.user_id == user.id, models.Purchase.created_at >= cutoff)
                 .order_by(models.Purchase.id).all())
    purchase_ids = {p.id for p in purchases}
    ledger = (db.query(models.LedgerEntry)
              .filter(models.LedgerEntry.user_id == user.id, models.LedgerEntry.created_at >= cutoff,
                      models.LedgerEntry.kind.in_(LEDGER_KINDS))
              .order_by(models.LedgerEntry.id).all())
    redemptions = (db.query(models.DiscountCodeRedemption)
                   .filter(models.DiscountCodeRedemption.user_id == user.id,
                           models.DiscountCodeRedemption.created_at >= cutoff).all())
    card_totals: dict[int, int] = {}
    for row in ledger:
        if row.payment_card_id is not None:
            card_totals[row.payment_card_id] = card_totals.get(row.payment_card_id, 0) + int(row.amount or 0)
    return {
        "purchases": purchases, "ledger": ledger, "redemptions": redemptions, "card_totals": card_totals,
        "topup_total": sum(int(r.amount or 0) for r in ledger if r.kind == "wallet_topup"),
        "old_renewals": [r for r in ledger if r.kind == "sale_renew" and r.purchase_id not in purchase_ids],
    }


def show(user: models.User, plan: dict, days: int, delete_user: bool) -> None:
    balance = int(user.balance or 0)
    print(f"customer {user.username} (id {user.id}), last {days} day(s)")
    print(f"  services to delete: {len(plan['purchases'])}")
    for purchase in plan["purchases"]:
        print(f"    service #{purchase.id}  {purchase.package_name_snapshot or '-'}  "
              f"created {purchase.created_at:%m-%d %H:%M}  connections: {len(purchase.connections)}")
    print(f"  accounting rows to delete: {len(plan['ledger'])}")
    for row in plan["ledger"]:
        print(f"    #{row.id}  {row.kind:<12} {int(row.amount or 0):>12,}  {row.created_at:%m-%d %H:%M}")
    print(f"  total removed from the books: {sum(int(r.amount or 0) for r in plan['ledger']):,}")
    print(f"  wallet: balance {balance:,}, top-ups to take back {plan['topup_total']:,} "
          f"-> {max(0, balance - plan['topup_total']):,}")
    print(f"  discount uses to undo: {len(plan['redemptions'])}")
    for card_id, amount in sorted(plan["card_totals"].items()):
        print(f"  bank card #{card_id}: take {amount:,} back out of its running total")
    if plan["old_renewals"]:
        print(f"  WARNING: {len(plan['old_renewals'])} renewal(s) of an older service - the accounting row is "
              f"deleted, but the quota/days added to that service are NOT taken back")
    if user.owner_admin_id is not None:
        print("  WARNING: this customer belongs to a reseller - credit the reseller was charged is NOT refunded")
    if int(getattr(user, "purchase_count", 0) or 0):
        print(f"  note: the loyalty purchase counter is {user.purchase_count} and is left as it is")
    print(f"  account itself: {'DELETED' if delete_user else 'kept'}")


def execute(db, user: models.User, plan: dict, delete_user: bool) -> int:
    kept = 0
    for purchase in plan["purchases"]:
        failed = []
        for connection in list(purchase.connections):
            try:
                user_ops.delete_connection(db, connection)
            except Exception as exc:  # noqa: BLE001 - one unreachable node must not stop the rest
                db.rollback()
                failed.append(f"{connection.type.value}: {type(exc).__name__}")
        if failed:
            kept += 1
            print(f"  KEPT service #{purchase.id}: could not remove from its node ({', '.join(failed)})")
            continue
        db.delete(purchase)
        db.commit()
        print(f"  deleted service #{purchase.id}")
    for redemption in plan["redemptions"]:
        code = db.get(models.DiscountCode, redemption.code_id)
        if code is not None and (code.used_count or 0) > 0:
            code.used_count -= 1
        db.delete(redemption)
    for card_id, amount in plan["card_totals"].items():
        card = db.get(models.PaymentCard, card_id)
        if card is not None:
            payment_cards.take_back(db, card, amount)
    user.balance = max(0, int(user.balance or 0) - plan["topup_total"])
    for row in plan["ledger"]:
        db.delete(row)
    db.commit()
    print(f"  deleted {len(plan['ledger'])} accounting row(s), undid {len(plan['redemptions'])} discount use(s), "
          f"wallet balance now {int(user.balance or 0):,}")
    if delete_user:
        if kept:
            print("  account NOT deleted: a service above could not be removed from its node")
        else:
            user_ops.delete_user_cascade(db, user)
            print("  account deleted")
    return 1 if kept else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="void_test_sales")
    parser.add_argument("username")
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--delete-user", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv[1:])
    if args.days < 1 or args.days > 30:
        print("--days must be between 1 and 30")
        return 2
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.username == args.username).first()
        if user is None:
            print(f"no customer named {args.username}")
            return 1
        plan = collect(db, user, dt.datetime.utcnow() - dt.timedelta(days=args.days))
        show(user, plan, args.days, args.delete_user)
        if not args.execute:
            print("\nNOTHING was changed. To really do it, add:  --execute --confirm", user.username)
            return 0
        if args.confirm != user.username:
            print("\nNOTHING was changed: --confirm must repeat the username exactly")
            return 2
        path = backup.create_backup()
        print(f"\nbackup taken first: {path.name}")
        return execute(db, user, plan, args.delete_user)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
