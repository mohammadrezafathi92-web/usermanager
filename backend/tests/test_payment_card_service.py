"""Receipt Void phase P1 - every writer of a payment-card pool lives in
services/payment_cards.py, and moving them there changed nothing.

Design: docs/receipt-void-design-2026-10-02.md, sections 3.4, 15.2 and the
P1 row of section 21 ("live behaviour unchanged"). Covers RV-64 (AST gate).
Run:  python3 backend/tests/test_payment_card_service.py
"""
from __future__ import annotations

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.routers import panel_settings as router
from app.services import payment_cards as cards

failures: list[str] = []
APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
SERVICE = os.path.join(APP_DIR, "services", "payment_cards.py")
POOL_FIELDS = {
    # (last_used_at is not listed: ApiKey has a column of the same name; the
    # card one is only written by resolve_active_card inside the service.)
    "accumulated_amount",
    "active_payment_card_id", "payment_card_mode", "payment_card_switch_threshold",
    "own_active_payment_card_id", "own_payment_card_mode", "own_payment_card_switch_threshold",
}


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
models.Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()
commits = []
event.listen(db, "after_commit", lambda session: commits.append(1))
root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True)
reseller = models.AdminUser(username="res", hashed_password="x", is_superadmin=False)
db.add_all([root, reseller, models.PanelSettings(id=1)])
db.commit()


def settings():
    db.expire_all()
    return db.get(models.PanelSettings, 1)


def card_payload(number):
    return schemas.PaymentCardCreate(card_number=number, card_holder="H")


print("--- RV-64: no card-pool writer outside the service ---")
offenders = []
for folder, _dirs, files in os.walk(APP_DIR):
    for name in files:
        path = os.path.join(folder, name)
        if not name.endswith(".py") or os.path.samefile(path, SERVICE):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr in POOL_FIELDS:
                    offenders.append((os.path.relpath(path, APP_DIR), node.lineno, f"assigns .{target.attr}"))
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "setattr" and len(node.args) >= 2 \
                        and isinstance(node.args[1], ast.Constant) and node.args[1].value in POOL_FIELDS:
                    offenders.append((os.path.relpath(path, APP_DIR), node.lineno, f"setattr {node.args[1].value}"))
                if isinstance(func, ast.Attribute) and func.attr == "PaymentCard":
                    offenders.append((os.path.relpath(path, APP_DIR), node.lineno, "constructs PaymentCard"))
check("no assignment to a pool field, no setattr of one, no PaymentCard(...) outside services/payment_cards.py",
      offenders, [])
router_source = open(os.path.join(APP_DIR, "routers", "panel_settings.py"), encoding="utf-8").read()
check("the settings router no longer deletes a card row itself", "db.delete(card)" in router_source, False)

print("--- global pool, through the real endpoint functions ---")
first = router.create_payment_card(card_payload("1111"), db=db)
check("first card of an empty pool becomes the active one", settings().active_payment_card_id, first.id)
second = router.create_payment_card(card_payload("2222"), db=db)
check("a second card does not move the pointer", settings().active_payment_card_id, first.id)
router.activate_payment_card(second.id, db=db)
check("manual activation moves the pointer", settings().active_payment_card_id, second.id)
updated = router.update_payment_card(first.id, schemas.PaymentCardUpdate(card_holder="New", sort_order=5), db=db)
check("update writes only the given fields", (updated.card_holder, updated.sort_order, updated.card_number), ("New", 5, "1111"))
router.delete_payment_card(second.id, db=db, _confirm=None)
check("deleting the ACTIVE card moves the pointer to what is left", settings().active_payment_card_id, first.id)
third = router.create_payment_card(card_payload("3333"), db=db)
router.delete_payment_card(third.id, db=db, _confirm=None)
check("deleting a non-active card leaves the pointer alone", settings().active_payment_card_id, first.id)
router.delete_payment_card(first.id, db=db, _confirm=None)
check("deleting the last card clears the pointer", (settings().active_payment_card_id, cards.list_cards(db, None)), (None, []))

print("--- global pool settings no longer go through the generic setattr loop ---")
router.update_settings(schemas.PanelSettingsUpdate(payment_card_mode="threshold", payment_card_switch_threshold=5000,
                                                   payment_instructions="pay here"), db=db, admin=root)
row = settings()
check("mode, threshold and an unrelated field are all stored as before",
      (row.payment_card_mode, row.payment_card_switch_threshold, row.payment_instructions), ("threshold", 5000, "pay here"))
recorded = []
real_set = cards.set_pool_settings
cards.set_pool_settings = lambda db_, owner, **kw: recorded.append((owner, kw))
try:
    router.update_settings(schemas.PanelSettingsUpdate(payment_card_mode="rotate", payment_instructions="changed"),
                           db=db, admin=root)
finally:
    cards.set_pool_settings = real_set
row = settings()
check("with the service call stubbed out, the pool field is NOT written (the loop no longer touches it) "
      "while the unrelated field still is",
      (recorded, row.payment_card_mode, row.payment_instructions), ([(None, {"mode": "rotate"})], "threshold", "changed"))

print("--- a reseller's own pool ---")
mine = router.create_my_payment_card(card_payload("9999"), admin=reseller, db=db)
db.refresh(reseller)
check("first own card becomes the reseller's active card", reseller.own_active_payment_card_id, mine.id)
check("...and the global pointer is untouched", settings().active_payment_card_id, None)
other = router.create_my_payment_card(card_payload("8888"), admin=reseller, db=db)
router.activate_my_payment_card(other.id, admin=reseller, db=db)
db.refresh(reseller)
check("own activation", reseller.own_active_payment_card_id, other.id)
router.update_my_payment(schemas.OwnPaymentSettingsUpdate(payment_card_mode="", payment_card_switch_threshold=700),
                                   db=db, admin=reseller)
db.refresh(reseller)
check("own settings: an empty mode still falls back to 'manual', threshold stored",
      (reseller.own_payment_card_mode, reseller.own_payment_card_switch_threshold), ("manual", 700))
router.delete_my_payment_card(other.id, admin=reseller, db=db, _confirm=None)
db.refresh(reseller)
check("deleting the reseller's active card falls back to their remaining one", reseller.own_active_payment_card_id, mine.id)

print("--- advance_after_payment: core + wrapper ---")
a = router.create_payment_card(card_payload("A"), db=db)
b = router.create_payment_card(card_payload("B"), db=db)
cards.set_pool_settings(db, None, mode="threshold", switch_threshold=1000, active_card_id=a.id)
db.commit()
commits.clear()
cards.advance_after_payment(db, a.id, 400)
db.refresh(a)
check("below the threshold: accumulated, pointer unchanged, exactly one commit",
      (a.accumulated_amount, settings().active_payment_card_id, len(commits)), (400, a.id, 1))
commits.clear()
cards.advance_after_payment(db, a.id, 600)
db.refresh(a)
check("reaching the threshold: pointer moves to the next card and the counter resets, one commit",
      (a.accumulated_amount, settings().active_payment_card_id, len(commits)), (0, b.id, 1))
commits.clear()
cards.advance_after_payment(db, a.id, 0)
cards.advance_after_payment(db, 999999, 500)
check("amount <= 0 and an unknown card are silent no-ops WITHOUT a commit (as before)", len(commits), 0)
commits.clear()
changed = cards.advance_after_payment_core(db, b.id, 250)
db.flush()
check("the core writes without committing", (changed, len(commits)), (True, 0))
db.rollback()
db.refresh(b)
check("...so the caller's rollback undoes it", b.accumulated_amount or 0, 0)
check("the core reports the no-ops", (cards.advance_after_payment_core(db, b.id, 0), cards.advance_after_payment_core(db, 999999, 5)),
      (False, False))
core_source = open(SERVICE, encoding="utf-8").read()
core_body = core_source[core_source.index("def advance_after_payment_core"):core_source.index("def advance_after_payment(")]
check("the core contains no commit/rollback", ("commit(" in core_body, "rollback(" in core_body), (False, False))
service_tail = core_source[core_source.index("_UNSET = object()"):]
check("none of the other service writers commits either (the endpoint does)", "commit(" in service_tail, False)

db.close()
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
