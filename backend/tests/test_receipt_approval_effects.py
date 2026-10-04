"""Receipt Void phase P3 (fifth batch) - record_effect: the only writer of
receipt_approval_effects, checked against the manifest (design 5.2, 5.3).
Run:  python3 backend/tests/test_receipt_approval_effects.py
"""
from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models, models_receipt_void as rv
from app.services import bot_auth, payment_cards, receipt_void_schema
from app.services import receipt_approval_effects as fx
from app.services import receipt_approval_intent as ri
from app.services import receipt_approval_registration as reg

failures: list[str] = []
F, SH, PE = rv.receipt_approval_effects, rv.receipt_approval_shadow_events, rv.payment_card_pool_events
GB = 1024 ** 3
NOW = dt.datetime(2026, 1, 1, 12, 0, 0)


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def rejected(*args):
    try:
        with db.begin_nested():
            fx.record_effect(db, *args)
    except fx.EffectRejected as exc:
        return exc.code
    return None


def effect(uuid, effect_type):
    return dict(db.execute(select(F).where(F.c.approval_uuid == uuid, F.c.effect_type == effect_type)).mappings().first())


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
pc = models.PackageConnection(package_id=package.id, node_id=node.id, protocol=models.ConnectionType.wireguard)
existing = models.User(username="ali", telegram_id=111, total_quota_bytes=100, used_bytes=100, balance=0)
code = models.DiscountCode(code="OFF10", kind="percent", value=10)
db.add_all([pc, existing, code])
card = payment_cards.create_card(db, None, {"card_number": "6037-1111", "card_holder": "H"})
db.commit()
internal = bot_auth.BotPrincipal.internal(None)
counter = [0]


def approval(**fields):
    counter[0] += 1
    intent = ri.ApprovalIntent(pending_source_instance_id="inst", pending_local_id=counter[0], amount=900, **fields)
    result = reg.register(db, internal, intent, approval_mode="manual", approved_by_telegram_id=1000)
    db.commit()
    return result.approval_uuid


print("--- a new user: order, bindings, projections ---")
new_uuid = approval(kind="new", target_username="newbie", package_id=package.id, claimed_telegram_id=999,
                    payment_card_id=card.id, discount_code="OFF10", list_price=1000)
user = models.User(username="newbie", telegram_id=999, total_quota_bytes=10 * GB, package_id=package.id)
db.add(user)
db.flush()
purchase = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=10 * GB, expire_at=NOW + dt.timedelta(days=30))
conn = models.Connection(user_id=user.id, node_id=node.id, type=models.ConnectionType.wireguard, wg_private_key="SECRET-KEY",
                         ppp_password="SECRET-PW")
sale = models.LedgerEntry(kind="sale_new", amount=900, user_id=user.id, payment_card_id=card.id, payment_method="card")
db.add_all([purchase, conn, sale])
db.flush()
slot = f"conn:package_connection:{pc.id}"
check("an effect that depends on the user cannot be written before user_created",
      (rejected(new_uuid, "purchase_created", "purchase", purchase, fx.PurchaseCreatedEvidence(30)),
       rejected(new_uuid, "ledger_sale", "sale", sale)), ("effect_manifest_mismatch", "effect_manifest_mismatch"))
user_ev = fx.UserCreatedEvidence(package_id=package.id, quota_bytes=10 * GB, days=30)
check("user_created", rejected(new_uuid, "user_created", "user", user, user_ev), None)
check("then purchase, connection and sale all match",
      [rejected(new_uuid, "purchase_created", "purchase", purchase, fx.PurchaseCreatedEvidence(30)),
       rejected(new_uuid, "connection_created", slot, conn, fx.ConnectionCreatedEvidence(slot)),
       rejected(new_uuid, "ledger_sale", "sale", sale)], [None, None, None])
row = effect(new_uuid, "ledger_sale")
check("stored: resource, projection with the binding resolved to the real user, delta",
      (row["resource_type"], row["resource_id"], json.loads(row["actual_projection"]), row["delta_value"]),
      ("LedgerEntry", sale.id, {"user": {"type": "User", "id": user.id}, "ledger_kind": "sale_new", "amount": 900,
                                "payment_card_id": card.id}, 900))
everything = " ".join(str(v) for r in db.execute(select(F)).mappings() for v in r.values())
check("no connection credential reaches the effects table", [s for s in ("SECRET-KEY", "SECRET-PW") if s in everything], [])
check("recording the same effect again returns the same row",
      fx.record_effect(db, new_uuid, "ledger_sale", "sale", sale) == row["id"], True)
payment_cards.advance_after_payment_core(db, card.id, 900)
event = dict(db.execute(select(PE).order_by(PE.c.id.desc())).mappings().first())
check("card payment (the pool event row is the resource)",
      (rejected(new_uuid, "card_payment_recorded", "card_payment", event), effect(new_uuid, "card_payment_recorded")["resource_type"]),
      (None, "PaymentCardPoolEvent"))
redemption = models.DiscountCodeRedemption(code_id=code.id, user_id=user.id, username="newbie", package_price=1000, discount_amount=100)
db.add(redemption)
db.flush()
key = f"discount:{code.id}"
check("an optional row goes through the very same check",
      (rejected(new_uuid, "discount_redeemed", key, redemption, fx.DiscountEvidence(3, 5)),
       rejected(new_uuid, "discount_redeemed", key, redemption, fx.DiscountEvidence(3, 4)),
       json.loads(effect(new_uuid, "discount_redeemed")["resource_snapshot"])["counter_after"]),
      ("effect_evidence_invalid", None, 4))
db.commit()

print("--- rejections ---")
other_uuid = approval(kind="new", target_username="ali", package_id=package.id)
p2 = models.Purchase(user_id=existing.id, package_id=package.id, quota_bytes=5 * GB, expire_at=NOW)
wrong_sale = models.LedgerEntry(kind="sale_new", amount=901, user_id=existing.id)
db.add_all([p2, wrong_sale])
db.flush()
check("not in the manifest (an existing user creates no user)", rejected(other_uuid, "user_created", "user", existing, user_ev),
      "effect_not_in_manifest")
check("a value that differs from the manifest (quota; amount)",
      (rejected(other_uuid, "purchase_created", "purchase", p2, fx.PurchaseCreatedEvidence(30)),
       rejected(other_uuid, "ledger_sale", "sale", wrong_sale)), ("effect_manifest_mismatch", "effect_manifest_mismatch"))
check("wrong evidence type / missing evidence / evidence where none belongs",
      (rejected(other_uuid, "purchase_created", "purchase", p2, user_ev), rejected(other_uuid, "purchase_created", "purchase", p2),
       rejected(other_uuid, "ledger_sale", "sale", wrong_sale, user_ev)), ("effect_evidence_invalid",) * 3)
try:
    fx._project("connection_created", slot, conn, fx.ConnectionCreatedEvidence("conn:request_slot:0"))
    slot_result = None
except fx.EffectRejected as exc:
    slot_result = exc.code
check("a slot key that is not the effect key is invalid evidence", slot_result, "effect_evidence_invalid")
check("nothing was written by any rejection", len(db.execute(select(F).where(F.c.approval_uuid == other_uuid)).all()), 0)
@dataclasses.dataclass(frozen=True)
class Leaky:
    password: str = "p"
try:
    fx._snapshot("User", 1, {}, Leaky())
    leak = "accepted"
except fx.EffectRejected as exc:
    leak = exc.code
check("a snapshot key outside the allowlist is REFUSED, not dropped", leak, "effect_evidence_invalid")
check("no allowlisted key looks like a credential",
      [k for k in fx.SNAPSHOT_KEYS if any(w in k for w in ("password", "secret", "private", "psk", "token", "uuid"))], [])
db.rollback()

print("--- renewal evidence: the state table ---")
def renewal(**changes):
    base = dict(renewal_mode="reserved", effective_now=NOW, add_quota_bytes=50, add_days=30, package_id_requested=7,
                q_before=100, u_before=10, e_before=NOW + dt.timedelta(days=5), rq_before=None, rd_before=None,
                rp_before=None, pk_before=3, q_after=100, u_after=10, e_after=NOW + dt.timedelta(days=5), rq_after=50,
                rd_after=30, rp_after=7, pk_after=3)
    base.update(changes)
    return fx.PurchaseRenewedEvidence(**base)


def renewal_ok(evidence):
    try:
        fx._validate_renewal(evidence)
        return True
    except fx.EffectRejected:
        return False


reset = dict(renewal_mode="immediate_reset", u_before=100, q_after=50, u_after=0, e_after=NOW + dt.timedelta(days=30),
             rq_after=None, rd_after=None, rp_after=None, pk_after=7)
check("mode is recomputed from the before values",
      [fx.renewal_mode_of(*args, NOW) for args in ((100, 10, None), (100, 100, None), (0, 0, None),
                                                   (0, 5, NOW + dt.timedelta(days=1)), (100, 10, NOW - dt.timedelta(days=1)))],
      ["reserved", "immediate_reset", "immediate_reset", "reserved", "immediate_reset"])
check("a consistent reserved renewal and a consistent immediate reset", (renewal_ok(renewal()), renewal_ok(renewal(**reset))), (True, True))
check("the design's example: used-up 100, add 50 -> q_after 50 (not 150), usage reset",
      renewal_ok(renewal(**{**reset, "q_after": 150})), False)
check("wrong mode for the before values; a column off its branch; nothing added",
      [renewal_ok(renewal(renewal_mode="immediate_reset")), renewal_ok(renewal(rq_after=49)), renewal_ok(renewal(u_after=0)),
       renewal_ok(renewal(add_quota_bytes=0, add_days=0, rq_after=0, rd_after=0))], [False] * 4)
check("quota only and days only are both valid",
      (renewal_ok(renewal(add_days=0, rd_after=0)), renewal_ok(renewal(add_quota_bytes=0, rq_after=0))), (True, True))

print("--- a renewal effect end to end ---")
existing.used_bytes, existing.total_quota_bytes, existing.expire_at = 0, 10 * GB, None
existing.reserved_quota_bytes, existing.reserved_duration_days, existing.reserved_package_id = 10 * GB, 30, package.id
db.commit()
renew_uuid = approval(kind="renew", target_username="ali", package_id=package.id)
evidence = renewal(add_quota_bytes=10 * GB, add_days=30, package_id_requested=package.id, q_before=10 * GB, u_before=0,
                   e_before=None, pk_before=None, q_after=10 * GB, u_after=0, e_after=None, rq_after=10 * GB, rd_after=30,
                   rp_after=package.id, pk_after=None)
check("recorded against the User (user-level renewal)",
      (rejected(renew_uuid, "purchase_renewed", "renew", existing, evidence), effect(renew_uuid, "purchase_renewed")["resource_type"],
       json.loads(effect(renew_uuid, "purchase_renewed")["resource_snapshot"])["renewal_mode"]), (None, "User", "reserved"))
db.commit()

print("--- topup ---")
top_uuid = approval(kind="topup", target_username="ali")
check("wallet credit: amount is balance_after - balance_before and must equal the manifest",
      (rejected(top_uuid, "wallet_credit_source_created", "credit:receipt_topup", existing, fx.WalletCreditEvidence(100, 999)),
       rejected(top_uuid, "wallet_credit_source_created", "credit:receipt_topup", existing, fx.WalletCreditEvidence(100, 100)),
       rejected(top_uuid, "wallet_credit_source_created", "credit:receipt_topup", existing, fx.WalletCreditEvidence(100, 1000))),
      ("effect_manifest_mismatch", "effect_evidence_invalid", None))
db.commit()

print("--- shadow wrapper ---")
before = len(db.execute(select(SH.c.id)).all())
marker = models.LedgerEntry(kind="sale_renew", amount=1, user_id=existing.id)
db.add(marker)
ok = fx.record_effect_shadow(db, renew_uuid, "ledger_sale", "sale", marker)
db.commit()
events = [tuple(r) for r in db.execute(select(SH.c.stage, SH.c.error_code, SH.c.approval_uuid).order_by(SH.c.id))][before:]
check("a mismatch is one shadow event, and the real mutation still commits",
      (ok, events, db.get(models.LedgerEntry, marker.id) is not None), (False, [("effect", "effect_manifest_mismatch", renew_uuid)], True))
check("no approval uuid (legacy path): a silent no-op", fx.record_effect_shadow(db, None, "ledger_sale", "sale", marker), False)
good = models.LedgerEntry(kind="sale_renew", amount=900, user_id=existing.id)
db.add(good)
db.flush()
check("a matching effect is recorded", fx.record_effect_shadow(db, renew_uuid, "ledger_sale", "sale", good), True)
db.commit()

print("--- one writer ---")
app_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
writers = []
for folder, _dirs, files in os.walk(app_dir):
    for name in files:
        if name.endswith(".py"):
            source = open(os.path.join(folder, name), encoding="utf-8").read()
            if "receipt_approval_effects" in source and ".insert()" in source and name != "receipt_approval_effects.py":
                tree = ast.parse(source)
                for node_ in ast.walk(tree):
                    if isinstance(node_, ast.Attribute) and node_.attr == "insert" and "effects" in ast.dump(node_.value):
                        writers.append(name)
check("only receipt_approval_effects.py inserts into receipt_approval_effects", writers, [])

db.close()
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
