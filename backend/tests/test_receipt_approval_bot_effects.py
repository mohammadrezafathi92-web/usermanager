"""Receipt Void phase P3 (seventh batch) - the bot's create_user endpoint
records what an approval really did, in shadow, without ever affecting the
sale itself (design 5.2, 5.6).
Run:  python3 backend/tests/test_receipt_approval_bot_effects.py
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models, models_receipt_void as rv, schemas
from app.routers import bot as bot_router
from app.services import bot_auth, receipt_void_schema, user_ops
from app.services import receipt_approval_intent as ri
from app.services import receipt_approval_registration as reg
from app.services import receipt_approval_runtime as runtime
from app.telegram_bot.handlers import admin_pending

failures: list[str] = []
F, SH = rv.receipt_approval_effects, rv.receipt_approval_shadow_events


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
models.Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
receipt_void_schema.bootstrap(engine, Session)
db = Session()
root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)
node = models.Node(name="n1", type=models.NodeType.mikrotik)
db.add_all([root, node, models.PanelSettings(id=1)])
db.flush()
package = models.Package(name="P", quota_gb=10, duration_days=30, price=1000)
db.add(package)
db.flush()
slots = [models.PackageConnection(package_id=package.id, node_id=node.id, protocol=models.ConnectionType.wireguard) for _ in range(2)]
db.add_all(slots)
db.commit()
internal = bot_auth.BotPrincipal.internal(None)


def fake_provision(db_, user, node_, protocol, flow="", *args, **kwargs):
    """Stands in for the remote call: only the Connection row."""
    batch = kwargs.get("purchase_batch") or next((a for a in args if isinstance(a, str) and len(a) == 32), None)
    connection = models.Connection(user_id=user.id, node_id=node_.id, type=models.ConnectionType(protocol),
                                   wg_private_key="SECRET-KEY", purchase_batch=batch)
    db_.add(connection)
    db_.flush()
    return connection


user_ops.provision_connection = fake_provision
specs = [schemas.BotCreateConnectionSpec(node_id=node.id, protocol="wireguard", flow="") for _ in range(2)]


def request(username, telegram_id, approval_uuid=None, **extra):
    fields = dict(username=username, quota_gb=10, expire_days=30, telegram_id=telegram_id, connections=specs,
                  package_name="P", package_id=package.id, paid_amount=900, payment_method="card",
                  approval_uuid=approval_uuid)
    fields.update(extra)
    return schemas.BotCreateUserRequest(**fields)


def effects(uuid):
    return sorted(f"{r.effect_type}:{r.effect_key}" for r in db.execute(select(F).where(F.c.approval_uuid == uuid)))


def shadow_codes():
    return [r.error_code for r in db.execute(select(SH).order_by(SH.c.id))]


print("--- no approval uuid: exactly the old behaviour ---")
bot_router.create_user(request("legacy", 1), db=db, principal=internal)
check("user, purchase and sale exist; nothing recorded; the ledger row has no approval",
      (db.query(models.User).filter_by(username="legacy").count(), len(db.execute(select(F)).all()), shadow_codes(),
       db.query(models.LedgerEntry).filter_by(username_snapshot="legacy").one().approval_uuid), (1, 0, [], None))

print("--- with a registered approval ---")
runtime.set_requested_mode(db, registration_mode="shadow")
db.commit()
intent = ri.ApprovalIntent(pending_source_instance_id="inst", pending_local_id=1, kind="new", target_username="newbie",
                           amount=900, package_id=package.id, list_price=1000, claimed_telegram_id=999)
answer = reg.begin(db, internal, intent, approval_mode="manual", approved_by_telegram_id=1000)
uuid_ = answer["approval_uuid"]
bot_router.create_user(request("newbie", 999, uuid_), db=db, principal=internal)
manifest = sorted(f"{m['effect_type']}:{m['effect_key']}" for m in reg.manifest_of(db, uuid_))
check("every expected effect was recorded - user, purchase, both connection slots, sale", (effects(uuid_), shadow_codes()),
      (manifest, []))
check("two identical connections landed on two different slots",
      len({r.resource_id for r in db.execute(select(F).where(F.c.approval_uuid == uuid_, F.c.effect_type == "connection_created"))}), 2)
check("the sale's ledger row now points at the approval",
      db.query(models.LedgerEntry).filter_by(username_snapshot="newbie").one().approval_uuid, uuid_)
stored = " ".join(str(v) for r in db.execute(select(F)).mappings() for v in r.values())
check("no credential in the effects", "SECRET-KEY" in stored, False)

print("--- a sale that differs from what was registered still goes through ---")
intent2 = ri.ApprovalIntent(pending_source_instance_id="inst", pending_local_id=2, kind="new", target_username="other",
                            amount=900, package_id=package.id, claimed_telegram_id=555)
uuid2 = reg.begin(db, internal, intent2, approval_mode="manual", approved_by_telegram_id=1000)["approval_uuid"]
bot_router.create_user(request("other", 555, uuid2, paid_amount=123, connections=specs[:1] + [
    schemas.BotCreateConnectionSpec(node_id=node.id, protocol="l2tp", flow="")]), db=db, principal=internal)
check("the customer was created and sold to regardless",
      (db.query(models.User).filter_by(username="other").count(), db.query(models.LedgerEntry).filter_by(username_snapshot="other").one().amount),
      (1, 123))
check("...and the differences are shadow events: an unexpected connection, a different amount",
      sorted(shadow_codes()), ["effect_manifest_mismatch", "effect_not_in_manifest"])
check("what did match is recorded", effects(uuid2),
      sorted(["user_created:user", "purchase_created:purchase", f"connection_created:conn:package_connection:{slots[0].id}"]))

print("--- an approval uuid the panel never issued ---")
before = len(shadow_codes())
bot_router.create_user(request("ghost", 777, "00000000-0000-0000-0000-000000000000"), db=db, principal=internal)
check("the sale works, the ledger row is NOT tagged with an unknown uuid, effects are refused",
      (db.query(models.User).filter_by(username="ghost").count(),
       db.query(models.LedgerEntry).filter_by(username_snapshot="ghost").one().approval_uuid,
       set(shadow_codes()[before:])), (1, None, {"effect_not_in_manifest"}))

print("--- an existing customer buys another package ---")
intent3 = ri.ApprovalIntent(pending_source_instance_id="inst", pending_local_id=3, kind="new", target_username="newbie",
                            amount=900, package_id=package.id, claimed_telegram_id=999)
uuid3 = reg.begin(db, internal, intent3, approval_mode="manual", approved_by_telegram_id=1000)["approval_uuid"]
before = len(shadow_codes())
try:
    bot_router.purchase_package("newbie", schemas.BotPurchasePackageRequest(
        package_id=package.id, paid_amount=900, payment_method="card", approval_uuid=uuid3), db=db, principal=internal)
    purchase_error = None
except Exception as exc:
    purchase_error = repr(exc)
check("purchase_package: purchase, both slots and the sale are recorded",
      (purchase_error, effects(uuid3), shadow_codes()[before:]),
      (None, sorted(f"{m['effect_type']}:{m['effect_key']}" for m in reg.manifest_of(db, uuid3)), []))

print("--- renewal ---")
intent4 = ri.ApprovalIntent(pending_source_instance_id="inst", pending_local_id=4, kind="renew", target_username="newbie",
                            amount=800, package_id=package.id, claimed_telegram_id=999)
uuid4 = reg.begin(db, internal, intent4, approval_mode="manual", approved_by_telegram_id=1000)["approval_uuid"]
bot_router.renew("newbie", schemas.BotRenewRequest(add_gb=10, add_days=30, package_id=package.id, paid_amount=800,
                                                    payment_method="card", approval_uuid=uuid4), db=db, principal=internal)
check("the renewal's sale is recorded and tagged (the renewal effect itself comes with user_ops evidence)",
      (effects(uuid4), db.query(models.LedgerEntry).filter_by(kind="sale_renew").one().approval_uuid), (["ledger_sale:sale"], uuid4))

print("--- top-up and card payment ---")
from app.services import payment_cards
card = payment_cards.create_card(db, None, {"card_number": "6037-9999", "card_holder": "H"})     # new pool: event_logged
db.commit()
intent5 = ri.ApprovalIntent(pending_source_instance_id="inst", pending_local_id=5, kind="topup", target_username="newbie",
                            amount=5000, payment_card_id=card.id, claimed_telegram_id=999)
uuid5 = reg.begin(db, internal, intent5, approval_mode="manual", approved_by_telegram_id=1000)["approval_uuid"]
balance = db.query(models.User).filter_by(username="newbie").one().balance or 0
bot_router.add_balance("newbie", schemas.BotAddBalanceRequest(amount=5000, payment_card_id=card.id, approval_uuid=uuid5),
                       db=db, principal=internal)
bot_router.record_payment_card_use(card.id, schemas.BotRecordCardPaymentRequest(amount=5000, approval_uuid=uuid5),
                                   db=db, principal=internal)
db.expire_all()
check("top-up: wallet credit, ledger_topup and the card payment - the whole manifest",
      (effects(uuid5), db.query(models.User).filter_by(username="newbie").one().balance - balance),
      (sorted(f"{m['effect_type']}:{m['effect_key']}" for m in reg.manifest_of(db, uuid5)), 5000))
PE = rv.payment_card_pool_events
check("the pool event carries the approval and is the voidable kind",
      [(e.event_kind, e.approval_uuid == uuid5, e.amount) for e in db.execute(select(PE))], [("payment_recorded", True, 5000)])
bot_router.record_payment_card_use(card.id, schemas.BotRecordCardPaymentRequest(amount=5000, approval_uuid=uuid5),
                                   db=db, principal=internal)
from app.services import payment_card_events
check("a repeated record-payment for the same approval is an ordinary payment - the pool is NOT knocked back to legacy",
      ([e.event_kind for e in db.execute(select(PE).order_by(PE.c.id))],
       payment_card_events.lock_pool(db, None, create_as=None), db.get(models.PaymentCard, card.id).accumulated_amount),
      (["payment_recorded", "payment_recorded_uncorrelated"], "event_logged", 10000))
db.rollback()
bot_router.add_balance("newbie", schemas.BotAddBalanceRequest(amount=-100), db=db, principal=internal)
bot_router.record_payment_card_use(card.id, schemas.BotRecordCardPaymentRequest(amount=1), db=db, principal=internal)
check("a wallet debit and a card payment without an approval behave as before",
      (db.query(models.User).filter_by(username="newbie").one().balance - balance, db.get(models.PaymentCard, card.id).accumulated_amount),
      (4900, 10001))

print("--- the bot passes the id along ---")
check("...also to add_balance and record_card_payment",
      (inspect.getsource(admin_pending.perform_approval).count('session.get("approval_uuid")'),), (3,))
source = inspect.getsource(admin_pending.perform_approval)
check("perform_approval puts the registered approval uuid into the sale details",
      'sale_info["approval_uuid"] = session["approval_uuid"]' in source, True)
check("all three sale request schemas accept it",
      ["approval_uuid" in m.model_fields for m in (schemas.BotCreateUserRequest, schemas.BotPurchasePackageRequest,
                                                   schemas.BotRenewRequest)], [True, True, True])

db.close()
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
